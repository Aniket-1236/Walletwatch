"""Instant triggers: subscribe to Solana logs for every tracked wallet over a websocket.

The websocket only says "this wallet just did something, check it NOW". The actual buy detection
still goes through the same verified RPC reader, so a dropped socket only means slower (polling) alerts,
never missing or wrong data."""
import json
import threading
import time

import config
from ws import WSClient, WSError


class SolanaWatcher(threading.Thread):
    def __init__(self, poller, sync_every=3.0):
        super().__init__(daemon=True, name="solana-ws")
        self.poller, self.sync_every = poller, sync_every
        self.error = None

    def run(self):
        p, backoff = self.poller, 1.0
        while not p.stop.is_set():
            url = config.solana_ws_url()
            if config.mock() or not config.use_ws() or not url:
                p.stop.wait(5)
                continue
            try:
                self.session(url)
                backoff = 1.0
            except WSError as e:
                self.error = str(e)[:160]
            except Exception as e:  # noqa: BLE001 - never let the watcher thread die
                self.error = f"{type(e).__name__}: {e}"[:160]
            p.ws_wallets = set()  # socket down: the poller automatically goes back to fast polling
            p.stop.wait(min(30.0, backoff))
            backoff *= 2

    def session(self, url):
        p = self.poller
        c = WSClient(url)
        c.connect()
        self.error = None
        pending, active, next_id = {}, {}, 1      # request id -> wallet id ; subscription id -> wallet id
        last_sync, last_ping, counted = 0.0, time.time(), 0
        try:
            while not p.stop.is_set():
                now = time.time()
                if now - last_sync >= self.sync_every:
                    last_sync = now
                    wanted = {w["id"]: w["address"] for w in p.db.list_wallets() if w["chain"] == "solana"}
                    have = set(pending.values()) | set(active.values())
                    for wid, addr in wanted.items():
                        if wid not in have:
                            c.send_text(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": "logsSubscribe",
                                                    "params": [{"mentions": [addr]}, {"commitment": "confirmed"}]}))
                            pending[next_id] = wid
                            next_id += 1
                    for sub, wid in list(active.items()):
                        if wid not in wanted:
                            c.send_text(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": "logsUnsubscribe",
                                                    "params": [sub]}))
                            next_id += 1
                            del active[sub]
                    p.ws_wallets = set(active.values())
                if now - last_ping >= 25:
                    c.ping()
                    last_ping = now
                raw = c.recv(timeout=1.0)
                if c.bytes_in != counted and getattr(p, "budget", None):
                    p.budget.add_ws_bytes(c.bytes_in - counted)     # websocket data costs credits too
                    counted = c.bytes_in
                if raw is None:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if "id" in msg and msg["id"] in pending:
                    wid = pending.pop(msg["id"])
                    if isinstance(msg.get("result"), int):
                        active[msg["result"]] = wid
                        p.ws_wallets = set(active.values())
                        p.trigger(wid, relist=True)  # catch up on anything missed before this subscription
                elif msg.get("method") == "logsNotification":
                    params = msg.get("params") or {}
                    wid = active.get(params.get("subscription"))
                    value = ((params.get("result") or {}).get("value")) or {}
                    if wid is not None and value.get("err") is None:
                        sig = value.get("signature")
                        # the signature + logs let the app fetch just this one transaction (1 request)
                        p.trigger(wid, hint=(sig, value.get("logs") or []) if sig else None, relist=not sig)
        finally:
            c.close()
