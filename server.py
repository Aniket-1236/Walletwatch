"""WalletWatch: full-stack wallet convergence tracker. Run:  python server.py   (Python 3.9+, no dependencies)."""
import hmac
import json
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import config
import auth as authmod
import budget as budgetmod
import evm
import rpc
from rpc import RateLimited
import live
import market
import mock
import notify
import signals
import solana
from db import DB

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".json": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml"}
WINDOWS = (1, 3, 6, 12, 24, 48)
SESSION_COOKIE = "ww_session"
PUBLIC_FILES = {"manifest.json", "icon-192.png", "icon-512.png", "sw.js", "login.html", "login.js"}
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"), ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
     "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; "
     "form-action 'self'; frame-ancestors 'none'"),
)
LOCKED_HTML = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
               "<title>WalletWatch locked</title><body style='font:16px system-ui;max-width:32rem;margin:12vh auto;padding:0 1rem'>"
               "<h1>WalletWatch is locked</h1><p>No password has been set up yet, so nobody can open this site.</p>"
               "<p>On the server run: <code>python setpassword.py</code></p></body>")


class Hub:
    def __init__(self):
        self.subs, self.lock = [], threading.Lock()

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def publish(self, event, data):
        with self.lock:
            for q in self.subs:
                try:
                    q.put_nowait((event, data))
                except queue.Full:
                    pass


class Poller:
    """Checks every wallet independently and in parallel. The moment a wallet's FIRST-EVER buy of a coin
    is confirmed it is stored, announced, and the multi-wallet count for that coin is recomputed."""

    def __init__(self, db, hub):
        self.db, self.hub = db, hub
        self.status, self.market_by_key = {}, {}
        self.signals, self.updated = [], None
        self.stop = threading.Event()
        self.eval_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.due, self.inflight, self.fails = {}, set(), {}
        self.ws_wallets, self.watcher = set(), None
        self.hints, self.relist = {}, set()                 # live websocket hints per wallet / wallets to list soon
        self.seen, self.next_list, self.last_list = {}, {}, {}
        self._last_mode = "normal"
        helius = bool(config.helius_key()) or "helius-rpc.com" in config.chain_cfg("solana")["rpc"]
        self.budget = budgetmod.Budget(
            db, limit=config.budget_limit(), reserve=config.budget_reserve(), cycle_day=config.cycle_day(),
            enabled=config.budget_enabled() and not config.mock() and (helius or bool(config.budget_hosts())),
            hosts=config.budget_hosts())
        rpc.set_budget(self.budget)
        self.pool = ThreadPoolExecutor(max_workers=config.workers(), thread_name_prefix="wallet")
        self.mpool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="market")
        self.mkt_inflight = set()
        self.latency = deque(maxlen=50)   # seconds between block time and detection

    # settings the user can change from the UI
    def min_wallets(self):
        return int(self.db.get_setting("min_wallets", 2))

    def window_hours(self):
        return int(self.db.get_setting("window_hours", 6))

    # scheduling
    def trigger(self, wallet_id=None, hint=None, relist=False):
        """Check this wallet (or all wallets) right now.
        hint=(signature, logs) comes from the websocket: that one transaction is fetched directly (1 request).
        relist=True asks for a full signature listing (used to catch up after a reconnect)."""
        with self.state_lock:
            if wallet_id is None:
                for w in self.db.list_wallets():
                    self.due[w["id"]] = 0
                    self.next_list[w["id"]] = 0       # a manual "check now" does a full listing
                return
            if relist and time.time() - self.last_list.get(wallet_id, 0) < 120:
                return                                 # listed very recently: don't waste requests
            if hint:
                self.hints.setdefault(wallet_id, []).append(hint)
                del self.hints[wallet_id][:-50]       # never hold more than 50 hints per wallet
            if relist:
                self.relist.add(wallet_id)
            self.due[wallet_id] = 0

    def list_interval(self, w):
        """Seconds between full signature listings of a wallet. With the live websocket this is only a
        safety net; without it it is the real polling speed. Both stretch automatically to fit the
        monthly request budget, so the budget can never run out early."""
        if w["chain"] != "solana":
            return config.poll_seconds()
        live = w["id"] in self.ws_wallets
        base = float(config.reconcile_seconds() if live else config.poll_seconds())
        if self.budget.enabled:
            n = max(1, sum(1 for x in self.db.list_wallets() if x["chain"] == "solana"))
            daily = max(1.0, self.budget.daily_allowance())
            base = max(base, n * 86400.0 / ((0.15 if live else 0.5) * daily))
            mode = self.budget.mode()
            if mode == "lean":
                base *= 2
            elif mode == "paused":
                base = max(base, 900.0)
        return min(base, 6 * 3600.0)

    interval = list_interval

    def tick(self):
        wallets, now = self.db.list_wallets(), time.time()
        ids = {w["id"] for w in wallets}
        with self.state_lock:
            for d in (self.due, self.fails, self.status):
                for k in [k for k in d if k not in ids]:
                    d.pop(k, None)
            for d in (self.hints, self.next_list, self.last_list, self.seen):
                for k in [k for k in d if k not in ids]:
                    d.pop(k, None)
            self.relist &= ids
            ready = [w for w in wallets if w["id"] not in self.inflight and self.due.get(w["id"], 0) <= now]
            for w in ready:
                self.inflight.add(w["id"])
        return [self.pool.submit(self._run_wallet, w) for w in ready]

    def _run_wallet(self, w):
        ok, err = False, None
        try:
            self.check_wallet(w)
            ok = True
        except Exception as e:  # noqa: BLE001 - one bad wallet/RPC must not stop the others
            err = e
            with self.state_lock:
                prev = self.status.get(w["id"], {})
                self.status[w["id"]] = {"ok": False, "checked": prev.get("checked"), "new": 0, "error": str(e)[:200]}
        finally:
            with self.state_lock:
                n = 0 if ok else self.fails.get(w["id"], 0) + 1
                self.fails[w["id"]] = n
                if n:
                    delay = self.list_interval(w) * (2 ** min(n, 4))
                    cap = 30 if isinstance(err, RateLimited) else (900 if isinstance(err, budgetmod.BudgetExceeded) else 120)
                    self.due[w["id"]] = time.time() + min(delay, cap)
                elif w["chain"] == "solana" and not config.mock():
                    self.due[w["id"]] = self.next_list.get(w["id"], time.time() + self.list_interval(w))
                else:
                    self.due[w["id"]] = time.time() + self.list_interval(w)
                if self.hints.get(w["id"]) and not n:
                    self.due[w["id"]] = 0               # a live hint arrived while we were busy
                self.inflight.discard(w["id"])

    def fetch(self, w):
        cursor = self.db.get_cursor(w["id"])
        if config.mock():
            return mock.fetch(w, cursor)
        cfg = config.chain_cfg(w["chain"])
        if not cfg["rpc"]:
            raise RuntimeError(f"No RPC URL for {cfg['label']}. Set the {cfg['env']} environment variable.")
        ignore = config.ignore_tokens()
        known = lambda token: self.db.has_buy(w["id"], token)  # noqa: E731
        if cfg["type"] == "solana":
            return self.fetch_solana(w, cursor, cfg["rpc"], ignore, known)
        lookback = config.env_int(cfg["env"].replace("_RPC_URL", "_LOOKBACK_BLOCKS"), 300)
        return evm.poll_wallet(cfg["key"], cfg["rpc"], w["address"], None if cursor is None else int(cursor),
                               lookback, ignore, known=known)

    def fetch_solana(self, w, cursor, rpc_url, ignore, known):
        wid, now = w["id"], time.time()
        seen = self.seen.setdefault(wid, solana.SeenSigs())
        verify = self.budget.mode() != "lean"              # saving mode skips the extra "first ever?" lookup
        with self.state_lock:
            hints, relist = self.hints.pop(wid, []), wid in self.relist
            self.relist.discard(wid)
        buys, new_cursor, retry = [], cursor, []
        if hints:
            buys, retry = solana.process_hints(rpc_url, w["address"], hints, ignore, known, seen, verify)
        if retry:
            self.next_list[wid] = min(self.next_list.get(wid, now + 3), now + 3)   # list shortly, nothing is lost
        if wid not in self.last_list or relist or now >= self.next_list.get(wid, 0):   # first look, catch-up, or schedule
            more, new_cursor = solana.poll_wallet(rpc_url, w["address"], cursor, config.seed_hours() * 3600, ignore,
                                                  known=known, seen_sigs=seen, verify_first=verify)
            buys += more
            self.last_list[wid] = now
            self.next_list[wid] = now + self.list_interval(w)
            with self.state_lock:
                self.relist.discard(wid)     # this listing already covers any catch-up requested while it ran
        return buys, new_cursor

    def check_wallet(self, w):
        buys, cursor = self.fetch(w)
        now, fresh, new_tx = time.time(), [], 0
        for b in buys:
            if self.db.add_tx_buy(w["id"], w["chain"], b["token"], b.get("symbol"), b.get("amount"), b["tx"], b["ts"], now):
                new_tx += 1                       # every buy transaction, repeats included (All Buys tab)
            first = bool(b.get("first_ever", True))
            if self.db.add_buy(w["id"], w["chain"], b["token"], b.get("symbol"), b.get("amount"), b["tx"], b["ts"],
                               first, now) and first:
                fresh.append(b)
        self.db.set_cursor(w["id"], cursor)   # only after the buys are safely stored
        with self.state_lock:
            self.status[w["id"]] = {"ok": True, "checked": time.time(), "new": len(fresh), "error": None}
        for b in fresh:
            self.on_first_buy(w, b)
        if new_tx:
            self.hub.publish("tx", {"wallet": w["label"], "count": new_tx})
        if fresh:
            self.evaluate()
        return len(fresh)

    def on_first_buy(self, w, b):
        chain, token = w["chain"], b["token"]
        try:
            m = market.get_market(chain, token, timeout=2.5, retries=0)
        except Exception:  # noqa: BLE001
            m = None
        symbol = b.get("symbol") or (m or {}).get("symbol")
        self.db.set_symbol(chain, token, symbol)
        name = symbol or (token[:4] + "..." + token[-4:])
        label = config.CHAIN_DEFS[chain]["label"]
        age = max(0.0, time.time() - b["ts"])
        self.latency.append(age)
        msg = f"{w['label']} bought {name} on {label} for the first time. Address: {token}"
        self.hub.publish("buy", {"title": f"New coin: {name}", "body": msg, "wallet": w["label"],
                                 "token": token, "chain": chain})
        if config.notify_first_buys() and age <= config.notify_max_age():
            self.hub.publish("alert", {"kind": "first_buy", "title": f"{w['label']}: new coin {name}", "body": msg})
            notify.send_async(f"{w['label']} bought new coin {name}", msg)

    def poll_once(self):
        """Check every wallet now and wait for it (used by tests and the demo)."""
        wallets = self.db.list_wallets()
        with self.state_lock:
            todo = [w for w in wallets if w["id"] not in self.inflight]
            for w in todo:
                self.inflight.add(w["id"])
        for f in [self.pool.submit(self._run_wallet, w) for w in todo]:
            f.result()
        self.evaluate()

    # evaluation: count distinct wallets per coin
    def evaluate(self, schedule=True):
        with self.eval_lock:
            self._evaluate(schedule)

    def _schedule_market(self, keys):
        for k in keys:
            if k in self.mkt_inflight or not market.needs_refresh(*k):
                continue
            self.mkt_inflight.add(k)
            self.mpool.submit(self._refresh_market, k)

    def _refresh_market(self, k):
        try:
            info = market.get_market(*k)
            if info and info.get("symbol"):
                self.db.set_symbol(k[0], k[1], info["symbol"])
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.mkt_inflight.discard(k)
        self.evaluate(schedule=False)

    def _evaluate(self, schedule=True):
        now = time.time()
        window = self.window_hours() * 3600
        buys = self.db.buys_since(now - window)
        counts = {}
        for b in buys:
            counts.setdefault((b["chain"], b["token"]), set()).add(b["wallet_id"])
        shown = [(b["chain"], b["token"]) for b in self.db.recent_buys(40)] + \
                [(b["chain"], b["token"]) for b in self.db.latest_tx_buys(60)]
        for k in shown:
            counts.setdefault(k, set())
        top = sorted(counts, key=lambda k: -len(counts[k]))[:40]
        order = list(dict.fromkeys(top + shown))        # every coin on screen gets its name looked up
        self.market_by_key = {k: market.peek(*k) for k in order}   # cache only: never blocks an alert
        if schedule:
            self._schedule_market(order)
        self.signals = signals.compute_signals(buys, self.market_by_key, now, window, self.min_wallets(),
                                               config.ignore_tokens())
        for s in self.signals:
            last = self.db.last_alert(s["chain"], s["token"], now - window)
            if last and last["wallet_count"] >= s["wallet_count"]:
                continue
            name = s["symbol"] or (s["token"][:4] + "..." + s["token"][-4:])
            label = config.CHAIN_DEFS[s["chain"]]["label"]
            who = ", ".join(w["label"] for w in s["wallets"][:6])
            msg = (f"{s['wallet_count']} of your wallets bought {name} on {label} within {self.window_hours()}h "
                   f"({who}). Signal {s['score']}/100 ({s['rating']}).")
            self.db.add_alert(s["chain"], s["token"], name, s["wallet_count"], s["score"], msg, now)
            if now - s["last_ts"] <= config.notify_max_age():  # don't page you about old history on first sync
                self.hub.publish("alert", {"kind": "multi", "title": f"{name}: {s['wallet_count']} wallets", "body": msg})
                notify.send_async(f"{name}: {s['wallet_count']} wallets bought", msg)
        self.updated = now
        self.hub.publish("update", {"updated": now})

    def chart(self):
        """Live graph data: wallets per coin (bars) and how that count grew over time (lines)."""
        now = time.time()
        buys = self.db.buys_since(now - self.window_hours() * 3600)
        groups = {}
        for b in buys:
            groups.setdefault((b["chain"], b["token"]), []).append(b)
        need = max(2, self.min_wallets())   # the graph only shows coins bought by 2+ (or your minimum) wallets
        ranked = sorted((kv for kv in groups.items() if len(kv[1]) >= need),
                        key=lambda kv: (-len(kv[1]), -max(r["ts"] for r in kv[1])))[:10]
        top, series = [], []
        for (chain, token), rows in ranked:
            m = self.market_by_key.get((chain, token)) or {}
            sym = m.get("symbol") or next((r["symbol"] for r in rows if r.get("symbol")), None) or (token[:4] + "..." + token[-4:])
            rows = sorted(rows, key=lambda r: r["ts"])
            top.append({"chain": chain, "token": token, "symbol": sym, "count": len(rows),
                        "wallets": [r["wallet_label"] for r in rows], "last_ts": rows[-1]["ts"]})
            if len(series) < 5:
                series.append({"token": token, "symbol": sym,
                               "points": [[r["ts"], i + 1] for i, r in enumerate(rows)]})
        return {"top": top, "series": series, "window_hours": self.window_hours(), "now": now, "min_wallets": need}

    def connection(self):
        """Plain-language health of the blockchain connection for the Wallets tab."""
        sol = [w for w in self.db.list_wallets() if w["chain"] == "solana"]
        now = time.time()
        with self.state_lock:
            st = {w["id"]: self.status.get(w["id"]) for w in sol}
        ok = sum(1 for v in st.values() if v and v["ok"] and v.get("checked") and now - v["checked"] < 600)
        err = next((v["error"] for v in st.values() if v and not v["ok"] and v.get("error")), None)
        host = config.chain_cfg("solana")["rpc"].split("//")[-1].split("/")[0].split("?")[0]
        return {"host": host, "solana_wallets": len(sol), "ok_wallets": ok, "error": err,
                "key_loaded": bool(config.helius_key()), "ws_enabled": config.use_ws() and not config.mock(),
                "ws_wallets": len(self.ws_wallets), "ws_error": getattr(self.watcher, "error", None)}

    def watch_budget(self):
        if not self.budget.enabled:
            return
        self.budget.flush()
        mode = self.budget.mode()
        if mode != self._last_mode and mode in ("lean", "paused"):
            msg = ("Request budget is running ahead of schedule. Saving mode is on: fewer background checks."
                   if mode == "lean" else
                   "Monthly request budget reserve reached. Checks are paused until the next cycle, so you are never charged.")
            self.hub.publish("alert", {"kind": "budget", "title": "Request budget", "body": msg})
        self._last_mode = mode

    def loop(self):
        last_eval = 0.0
        while not self.stop.is_set():
            try:
                self.tick()
                if time.time() - last_eval >= 15:
                    last_eval = time.time()
                    self.evaluate()
                    self.watch_budget()
            except Exception as e:  # noqa: BLE001
                print("poll loop error:", e, flush=True)
            self.stop.wait(0.5)


class RemotePoller:
    """Stands in for the Poller inside the WEBSITE process when the tracking runs in worker.py
    (PythonAnywhere: a web app cannot keep background threads alive). It only passes requests on."""

    def __init__(self, db):
        self.db, self.status, self.signals, self.market_by_key = db, {}, [], {}
        self.updated, self.ws_wallets, self.watcher, self.latency = None, set(), None, deque()
        self.budget = budgetmod.Budget(db, enabled=False)

    def min_wallets(self):
        return int(self.db.get_setting("min_wallets", 2))

    def window_hours(self):
        return int(self.db.get_setting("window_hours", 6))

    def trigger(self, wallet_id=None, hint=None, relist=False):
        self.db.set_setting("poll_kick", time.time())      # the worker sees this and checks now

    def evaluate(self, schedule=True):
        self.db.set_setting("kick", time.time())           # the worker recalculates signals now

    def chart(self):
        return {"top": [], "series": [], "window_hours": self.window_hours(), "now": time.time(),
                "min_wallets": max(2, self.min_wallets())}

    def connection(self):
        n = sum(1 for w in self.db.list_wallets() if w["chain"] == "solana")
        return {"host": "", "solana_wallets": n, "ok_wallets": 0, "error": None, "key_loaded": bool(config.helius_key()),
                "ws_enabled": False, "ws_wallets": 0, "ws_error": None}


class App:
    SNAPSHOT_KEYS = ("updated", "signals", "chart", "usage", "conn", "live", "alerts", "feed", "all_buys")

    def __init__(self, db_path=None, remote=False):
        self.db = DB(db_path or config.db_path())
        self.hub = Hub()
        self.remote = remote
        self.auth = authmod.Auth(self.db)
        self.poller = RemotePoller(self.db) if remote else Poller(self.db, self.hub)

    def state(self):
        st = self._compute_state()
        if not self.remote:
            return st
        # website process: everything that lives in the worker's memory comes from its latest snapshot
        raw, ts = self.db.get_setting("snapshot"), float(self.db.get_setting("snapshot_ts", 0) or 0)
        try:
            snap = json.loads(raw) if raw else None
        except ValueError:
            snap = None
        if snap:
            for k in self.SNAPSHOT_KEYS:
                if k in snap:
                    st[k] = snap[k]
            stat = {w["id"]: w.get("status") for w in snap.get("wallets", [])}
            for w in st["wallets"]:
                w["status"] = stat.get(w["id"])
        age = time.time() - ts if ts else None
        st["web"] = True
        st["worker"] = {"alive": age is not None and age < 30, "age": age}
        return st

    def _compute_state(self):
        db, p = self.db, self.poller
        counts, wallets = db.buy_counts(), []
        for w in db.list_wallets():
            wallets.append({**w, "buys": counts.get(w["id"], 0), "status": p.status.get(w["id"])})
        feed = []
        for b in db.recent_buys(40):
            m = p.market_by_key.get((b["chain"], b["token"])) or {}
            cfg = config.CHAIN_DEFS[b["chain"]]
            feed.append({"wallet_label": b["wallet_label"], "chain": b["chain"], "token": b["token"],
                         "symbol": b["symbol"] or m.get("symbol"), "name": m.get("name"),
                         "amount": b["amount"], "ts": b["ts"],
                         "delay": max(0, b["detected_at"] - b["ts"]),
                         "tx_url": cfg["tx_url"] + b["tx"] if cfg["tx_url"] else None})
        all_buys = []
        for b in db.latest_tx_buys(60):
            m = p.market_by_key.get((b["chain"], b["token"])) or {}
            cfg = config.CHAIN_DEFS[b["chain"]]
            all_buys.append({"wallet_label": b["wallet_label"], "chain": b["chain"], "token": b["token"],
                             "symbol": b["symbol"] or m.get("symbol"), "name": m.get("name"),
                             "amount": b["amount"], "ts": b["ts"],
                             "delay": max(0, b["detected_at"] - b["ts"]) if b["detected_at"] else None,
                             "is_first": bool(b["is_first"]),
                             "tx_url": cfg["tx_url"] + b["tx"] if cfg["tx_url"] else None})
        return {
            "now": time.time(), "mock": config.mock(), "updated": p.updated, "all_buys": all_buys,
            "settings": {"min_wallets": p.min_wallets(), "window_hours": p.window_hours(),
                         "poll_seconds": config.poll_seconds(), "windows": list(WINDOWS)},
            "chains": [{"key": k, "label": d["label"], "ready": config.mock() or bool(config.chain_cfg(k)["rpc"]),
                        "env": d["env"]} for k, d in config.CHAIN_DEFS.items()],
            "wallets": wallets, "signals": p.signals, "feed": feed,
            "alerts": db.recent_alerts(15), "notify": notify.configured(),
            "chart": p.chart(), "usage": p.budget.snapshot(),
            "conn": p.connection(),
            "live": {"ws_wallets": len(p.ws_wallets), "ws_error": getattr(p.watcher, "error", None),
                     "avg_delay": (sum(p.latency) / len(p.latency)) if p.latency else None,
                     "last_delay": p.latency[-1] if p.latency else None},
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "WalletWatch/1.0"

    def log_message(self, fmt, *args):
        pass

    @property
    def app(self):
        return self.server.app

    # ---------- security helpers
    def end_headers(self):
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        if self._secure():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _secure(self):
        return (getattr(self, "_https", False) or os.getenv("COOKIE_SECURE") == "1"
                or (os.getenv("TRUST_PROXY") == "1" and self.headers.get("X-Forwarded-Proto") == "https"))

    def _ip(self):
        if os.getenv("TRUST_PROXY") == "1":          # only when a proxy you control sits in front (it appends the real IP last)
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()[:64]
        return str(self.client_address[0])[:64]

    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == (self.headers.get("Host") or "")

    def _auth_state(self):
        """'ok' (may use the panel), 'login' (must log in) or 'locked' (no password configured)."""
        a = self.app.auth
        m = a.mode()
        if m == "open":
            return "ok"
        if m == "locked":
            return "locked"
        return "ok" if a.valid(self._cookie()) else "login"

    def _cookie_header(self, token, max_age):
        return (f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"
                + ("; Secure" if self._secure() else ""))

    def _json(self, code, obj, headers=()):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            raise ValueError("Request too large")
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if not isinstance(data, dict):
            raise ValueError("Bad request")
        return data

    def _static(self, name, private=False):
        full = os.path.join(STATIC, name)
        ext = os.path.splitext(name)[1]
        if ext not in TYPES or not os.path.isfile(full):
            return self._json(404, {"error": "Not found"})
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if private or ext in (".html", ".js") else "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    # ---------- login
    def _login(self):
        a = self.app.auth
        if a.mode() != "login":
            return self._json(400, {"error": "No password is set up."})
        try:
            data = self._body()
        except (ValueError, TypeError):
            return self._json(400, {"error": "Bad request"})
        pw, code = data.get("password"), data.get("code")
        if not isinstance(pw, str) or len(pw) > 200 or (code is not None and not isinstance(code, str)):
            return self._json(400, {"error": "Bad request"})
        res = a.login(self._ip(), pw, code)
        if res["ok"]:
            return self._json(200, {"ok": True}, [("Set-Cookie", self._cookie_header(res["token"], res["max_age"]))])
        extra = [("Retry-After", str(res["retry_after"]))] if res.get("retry_after") else []
        return self._json(res["status"], {"error": res["error"]}, extra)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"ok": True})
        a = self.app.auth
        if path == "/api/auth":
            return self._json(200, {"mode": a.mode(), "authed": self._auth_state() == "ok", "totp": a.totp_enabled()})
        name = "index.html" if path == "/" else os.path.basename(path)
        if name in PUBLIC_FILES or path in ("/login", "/login/"):
            return self._static("login.html" if path in ("/login", "/login/") else name)
        st = self._auth_state()
        if st == "locked":
            return self._html(503, LOCKED_HTML)
        if st == "login":
            if path.startswith("/api/"):
                return self._json(401, {"error": "Login required"})
            return self._static("login.html")              # any page: just the login form, no data
        if path == "/api/state":
            data = self.app.state()
            data["auth_enabled"] = a.mode() == "login"
            return self._json(200, data)
        if path == "/api/stream":
            return self._stream()
        return self._static(name)

    def _write_gate(self):
        """Common checks for POST/DELETE. Returns the path, or None after a response was sent."""
        path = urlparse(self.path).path
        if not self._origin_ok():
            self._json(403, {"error": "Blocked: request came from another site."})
            return None
        st = self._auth_state()
        if st == "locked":
            self._html(503, LOCKED_HTML)
            return None
        if st == "login":
            self._json(401, {"error": "Login required"})
            return None
        return path

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            if not self._origin_ok():
                return self._json(403, {"error": "Blocked: request came from another site."})
            return self._login()
        if path == "/api/logout":
            if not self._origin_ok():
                return self._json(403, {"error": "Blocked: request came from another site."})
            self.app.auth.logout(self._cookie())
            return self._json(200, {"ok": True}, [("Set-Cookie", self._cookie_header("", 0))])
        path = self._write_gate()
        if path is None:
            return
        try:
            data = self._body()
            if path == "/api/wallets":
                return self._add_wallet(data)
            if path == "/api/settings":
                return self._settings(data)
            if path == "/api/poll":
                self.app.poller.trigger()
                return self._json(202, {"ok": True})
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        self._json(404, {"error": "Not found"})

    def do_DELETE(self):
        path = self._write_gate()
        if path is None:
            return
        if path.startswith("/api/wallets/"):
            try:
                wid = int(path.rsplit("/", 1)[1])
            except ValueError:
                return self._json(400, {"error": "Bad wallet id"})
            if self.app.db.remove_wallet(wid):
                self.app.poller.status.pop(wid, None)
                threading.Thread(target=self.app.poller.evaluate, daemon=True).start()
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "Wallet not found"})
        self._json(404, {"error": "Not found"})

    def _add_wallet(self, data):
        chain = data.get("chain")
        address = config.normalize_address(chain, data.get("address"))
        label = (data.get("label") or "").strip()[:40] or f"{config.CHAIN_DEFS[chain]['label']} {address[:4]}...{address[-4:]}"
        if len(self.app.db.list_wallets()) >= config.max_wallets():
            raise ValueError(f"Wallet limit reached ({config.max_wallets()}).")
        wid = self.app.db.add_wallet(chain, address, label)
        self.app.poller.trigger()
        self._json(201, {"id": wid, "chain": chain, "address": address, "label": label})

    def _settings(self, data):
        db = self.app.db
        if "min_wallets" in data:
            n = int(data["min_wallets"])
            if not 2 <= n <= 10:
                raise ValueError("Minimum wallets must be between 2 and 10.")
            db.set_setting("min_wallets", n)
        if "window_hours" in data:
            h = int(data["window_hours"])
            if h not in WINDOWS:
                raise ValueError("Window must be one of " + ", ".join(map(str, WINDOWS)) + " hours.")
            db.set_setting("window_hours", h)
        threading.Thread(target=self.app.poller.evaluate, daemon=True).start()
        self._json(200, {"ok": True})

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = self.app.hub.subscribe()
        try:
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=15)
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(q)


def make_server(app, host="0.0.0.0", port=8080):
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.app = app
    return srv


def main():
    app = App()
    threading.Thread(target=app.poller.loop, daemon=True).start()
    if not config.mock():
        def _selftest():
            cfg = config.chain_cfg("solana")
            t = time.time()
            try:
                slot = rpc.rpc_call(cfg["rpc"], "getSlot", [])
                print(f"Connection test OK: Solana slot {slot} ({int((time.time() - t) * 1000)} ms)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"CONNECTION PROBLEM: {e}\n  Run  python check.py  to find out why.", flush=True)
        threading.Thread(target=_selftest, daemon=True).start()
        rpc_host = (config.chain_cfg("solana")["rpc"].split("//")[-1].split("/")[0].split("?")[0])
        print(f"Solana RPC: {rpc_host}" + (" (Helius key loaded, 10 requests/second limit respected)" if config.helius_key() or "helius" in rpc_host else ""), flush=True)
    if not config.mock() and config.use_ws():
        app.poller.watcher = live.SolanaWatcher(app.poller)
        app.poller.watcher.start()
    host, port = os.getenv("HOST", "0.0.0.0"), config.env_int("PORT", 8080)
    srv = make_server(app, host, port)
    mode = "DEMO MODE (fake data)" if config.mock() else "LIVE"
    print(f"WalletWatch {mode} on http://{host}:{port}  (checking every {config.poll_seconds()}s, live websocket for Solana: {"on" if config.use_ws() else "off"})", flush=True)
    mode = app.auth.mode()
    if mode == "login":
        print("Login is ON" + (" with an authenticator code." if app.auth.totp_enabled() else ".")
              + ("" if os.getenv("APP_PASSWORD_HASH") else "  Tip: run  python setpassword.py  to store a hashed password."), flush=True)
    elif mode == "locked":
        print("NO PASSWORD SET: the site stays locked until you run  python setpassword.py", flush=True)
    else:
        print("Warning: running WITHOUT a password (demo/local only).", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
