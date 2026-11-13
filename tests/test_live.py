"""Speed/accuracy tests against a simulated Solana chain (fake JSON-RPC server + fake websocket server)."""
import base64, hashlib, json, os, socket, struct, sys, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config, db as dbmod, live, market, server, solana
from rpc import RpcError
from ws import WSClient, GUID

MINT, MINT2 = "MintAAAA" + "1" * 36, "MintBBBB" + "2" * 36


def wallet(n): return f"Wallet{n}" + "x" * 30


def ata(w, m): return f"ata:{w}:{m}"


def buy_tx(w, mint, sig, pre_raw=0, ts=None):
    pre = [{"mint": mint, "owner": w, "accountIndex": 1, "uiTokenAmount": {"amount": str(pre_raw), "decimals": 6}}] if pre_raw else []
    post = [{"mint": mint, "owner": w, "accountIndex": 1, "uiTokenAmount": {"amount": str(pre_raw + 5_000_000), "decimals": 6}}]
    return {"blockTime": ts or int(time.time()),
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": w}, {"pubkey": ata(w, mint)}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [2_000_000_000, 0], "postBalances": [1_000_000_000, 0],
                     "preTokenBalances": pre, "postTokenBalances": post}}


class FakeChain:
    """In-memory Solana: signature lists per address (newest first) and transactions."""
    def __init__(self):
        self.sigs, self.txs, self.lock, self.calls, self.hide = {}, {}, threading.Lock(), [], set()
        self.detail = []
        self.n = 0

    def add(self, w, mint, pre_raw=0, old_history=False):
        with self.lock:
            self.n += 1
            sig = f"sig{self.n:05d}"
            self.txs[sig] = buy_tx(w, mint, sig, pre_raw)
            for addr in (w, ata(w, mint)):
                self.sigs.setdefault(addr, []).insert(0, {"signature": sig, "err": None, "blockTime": int(time.time())})
            if old_history:
                self.sigs[ata(w, mint)].append({"signature": "older" + sig, "err": None, "blockTime": 1})
            return sig

    def rpc(self, method, params):
        self.calls.append(method)
        self.detail.append((method, params[0] if params else None))
        if method == "getSlot":
            return 12345
        with self.lock:
            if method == "getSignaturesForAddress":
                lst, o = list(self.sigs.get(params[0], [])), params[1]
                if o.get("commitment") != "confirmed":
                    return []          # the real default (finalized) would be ~13s late: tests enforce "confirmed"
                names = [s["signature"] for s in lst]
                if o.get("until") in names:
                    lst = lst[:names.index(o["until"])]
                if o.get("before") in names:
                    lst = lst[names.index(o["before"]) + 1:]
                return lst[:o.get("limit", 1000)]
            if method == "getTransaction":
                return None if params[0] in self.hide else self.txs.get(params[0])
        raise AssertionError(method)


class FakeRpcServer:
    def __init__(self, chain):
        outer = chain

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": outer.rpc(req["method"], req["params"])}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self): self.srv.shutdown(); self.srv.server_close()


def _frame(payload: bytes, opcode=1):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return head + payload


class FakeWsServer:
    """Accepts websocket clients, answers logsSubscribe, and can push a notification for an address."""
    def __init__(self):
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(5)
        self.port, self.conns, self.subs, self.next, self.closed = self.sock.getsockname()[1], [], {}, 100, False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closed:
            try: c, _ = self.sock.accept()
            except OSError: return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(4096)
            if not chunk: return
            data += chunk
        key = [l.split(":")[1].strip() for l in data.decode().split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        self.conns.append(c)
        buf = data.split(b"\r\n\r\n", 1)[1]
        try:
            while True:
                while len(buf) < 2 or len(buf) < self._need(buf):
                    chunk = c.recv(4096)
                    if not chunk: return
                    buf += chunk
                n, hdr = buf[1] & 0x7F, 6
                if n == 126: n, hdr = struct.unpack(">H", buf[2:4])[0], 8
                mask = buf[hdr - 4:hdr]
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[hdr:hdr + n]))
                buf = buf[hdr + n:]
                if (buf and False): pass
                try: msg = json.loads(payload)
                except ValueError: continue
                if msg.get("method") == "logsSubscribe":
                    sub = self.next; self.next += 1
                    self.subs[msg["params"][0]["mentions"][0]] = (sub, c)
                    c.sendall(_frame(json.dumps({"jsonrpc": "2.0", "result": sub, "id": msg["id"]}).encode()))
        except OSError:
            pass

    @staticmethod
    def _need(buf):
        n, hdr = buf[1] & 0x7F, 6
        if n == 126:
            if len(buf) < 4: return 99999
            n, hdr = struct.unpack(">H", buf[2:4])[0], 8
        return hdr + n

    def notify(self, addr, err=None, sig=None, logs=None):
        sub, c = self.subs[addr]
        c.sendall(_frame(json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
            "result": {"context": {"slot": 1}, "value": {"signature": sig or "x", "err": err, "logs": logs or []}},
            "subscription": sub}}).encode()))

    def close(self):
        self.closed = True
        for c in self.conns:
            try: c.close()
            except OSError: pass
        self.sock.close()


def wait_for(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond(): return True
        time.sleep(0.05)
    return False


class SolanaAccuracy(unittest.TestCase):
    def setUp(self):
        self.chain = FakeChain(); self.rpc = FakeRpcServer(self.chain); self.url = f"http://127.0.0.1:{self.rpc.port}"
        self.nd = solana.NULL_RETRY_DELAY; solana.NULL_RETRY_DELAY = 0

    def tearDown(self):
        solana.NULL_RETRY_DELAY = self.nd; self.rpc.close()

    def test_first_ever_vs_previous_history(self):
        w = wallet(1)
        self.chain.add(w, MINT)
        self.chain.add(w, MINT2, old_history=True)          # token account already had history
        buys, cur = solana.poll_wallet(self.url, w, None, 3600)
        got = {b["token"]: b["first_ever"] for b in buys}
        self.assertEqual(got, {MINT: True, MINT2: False})
        self.assertTrue(cur)

    def test_already_holding_is_not_first_and_known_coin_is_still_reported(self):
        w = wallet(2)
        self.chain.add(w, MINT, pre_raw=1_000_000)
        buys, _ = solana.poll_wallet(self.url, w, None, 3600)
        self.assertFalse(buys[0]["first_ever"])
        # a coin this wallet already bought is still returned (for the All Buys tab) but never "first ever",
        # even when its token account has no older history, and without any extra on-chain lookup
        w3 = wallet(3)
        self.chain.add(w3, MINT)
        buys, _ = solana.poll_wallet(self.url, w3, None, 3600, known=lambda m: True)
        self.assertEqual([(b["token"], b["first_ever"]) for b in buys], [(MINT, False)])

    def test_two_buys_of_same_coin_in_one_poll_are_both_reported_only_first_is_new(self):
        w = wallet(6)
        self.chain.add(w, MINT)
        self.chain.add(w, MINT, pre_raw=5_000_000)
        buys, _ = solana.poll_wallet(self.url, w, None, 3600)
        self.assertEqual([b["first_ever"] for b in buys], [True, False])
        self.assertEqual(len({b["tx"] for b in buys}), 2)

    def test_unindexed_transaction_is_retried_not_lost(self):
        w = wallet(3)
        sig = self.chain.add(w, MINT)
        self.chain.hide.add(sig)
        buys, cur = solana.poll_wallet(self.url, w, None, 3600)
        self.assertEqual((buys, cur), ([], None))           # cursor did NOT move past the missing tx
        self.chain.hide.clear()
        buys, cur = solana.poll_wallet(self.url, w, None, 3600)
        self.assertEqual([b["token"] for b in buys], [MINT]); self.assertEqual(cur, sig)

    def test_more_than_one_page_is_fully_read(self):
        w = wallet(4)
        first = self.chain.add(w, MINT)
        _, cur = solana.poll_wallet(self.url, w, None, 3600)
        for i in range(130):
            self.chain.add(w, f"Mint{i:04d}" + "z" * 36)
        buys, cur2 = solana.poll_wallet(self.url, w, cur, 3600)
        self.assertEqual(len(buys), 130)

    def test_requires_confirmed_commitment(self):
        w = wallet(5); self.chain.add(w, MINT)
        self.assertEqual(len(solana.poll_wallet(self.url, w, None, 3600)[0]), 1)  # fake returns [] unless "confirmed"


class WebsocketTests(unittest.TestCase):
    def test_client_handshake_and_messages(self):
        srv = FakeWsServer()
        try:
            c = WSClient(f"ws://127.0.0.1:{srv.port}"); c.connect()
            c.send_text(json.dumps({"id": 7, "method": "logsSubscribe", "params": [{"mentions": ["A"]}, {}]}))
            self.assertEqual(json.loads(c.recv(2))["id"], 7)
            self.assertIsNone(c.recv(0.1))                  # timeout returns None, connection stays usable
            srv.notify("A")
            self.assertEqual(json.loads(c.recv(2))["method"], "logsNotification")
            c.close()
        finally:
            srv.close()


class EndToEnd(unittest.TestCase):
    """Three wallets, real poller + websocket trigger, simulated chain."""
    def setUp(self):
        self.env = {k: os.environ.get(k) for k in ("MOCK", "SOLANA_RPC_URL", "SOLANA_WS_URL", "POLL_SECONDS", "SAFETY_POLL_SECONDS")}
        self.chain = FakeChain(); self.rpc = FakeRpcServer(self.chain); self.ws = FakeWsServer()
        os.environ.pop("MOCK", None)
        os.environ.update(SOLANA_RPC_URL=f"http://127.0.0.1:{self.rpc.port}", SOLANA_WS_URL=f"ws://127.0.0.1:{self.ws.port}",
                          POLL_SECONDS="60", SAFETY_POLL_SECONDS="60")   # slow polling: only the websocket can be fast
        self.orig_market = market.get_market
        market.get_market = lambda chain, token, timeout=10, retries=1: None
        self.app = server.App(":memory:")
        self.p = self.app.poller
        self.ids = [self.app.db.add_wallet("solana", wallet(i), f"W{i}") for i in (1, 2, 3)]
        self.events = []
        q = self.app.hub.subscribe()
        threading.Thread(target=lambda: [self.events.append(q.get()) for _ in iter(int, 1)], daemon=True).start()
        threading.Thread(target=self.p.loop, daemon=True).start()
        self.p.watcher = live.SolanaWatcher(self.p, sync_every=0.2); self.p.watcher.start()
        self.assertTrue(wait_for(lambda: len(self.p.ws_wallets) == 3), "websocket subscriptions did not come up")
        self.assertTrue(wait_for(lambda: all(self.p.status.get(i, {}).get("ok") for i in self.ids)))

    def tearDown(self):
        self.p.stop.set(); self.ws.close(); self.rpc.close(); market.get_market = self.orig_market
        for k, v in self.env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def kinds(self, kind): return [e for e in self.events if e[0] == kind]

    def buy(self, n, mint=MINT, **kw):
        """A buy on the simulated chain plus the websocket push a real RPC would send (signature + logs)."""
        sig = self.chain.add(wallet(n), mint, **kw)
        self.ws.notify(wallet(n), sig=sig, logs=TOKEN_LOGS)
        return sig

    def test_missed_or_unusable_websocket_hint_is_recovered_by_listing(self):
        self.chain.add(wallet(2), MINT)
        self.ws.notify(wallet(2), sig="not-a-real-signature", logs=TOKEN_LOGS)    # push names a signature the RPC can't find
        self.assertTrue(wait_for(lambda: len(self.kinds("buy")) == 1, 10), "buy was lost")

    def test_fast_first_buy_repeat_ignored_and_multi_wallet_count(self):
        t0 = time.time()
        self.buy(1, MINT)
        self.assertTrue(wait_for(lambda: len(self.kinds("buy")) == 1, 4), "first buy not detected via websocket")
        self.assertLess(time.time() - t0, 3.0)                              # with 60s polling this is only possible via push
        self.assertEqual(len(self.app.db.recent_buys()), 1)
        self.assertEqual(self.p.chart()["top"], [])                         # graph needs 2+ wallets on one coin

        self.buy(1, MINT, pre_raw=5_000_000)   # same wallet buys SAME coin again
        self.assertTrue(wait_for(lambda: len(self.app.db.latest_tx_buys()) == 2, 4))   # All Buys tab sees the repeat
        time.sleep(0.5)
        self.assertEqual(len(self.kinds("buy")), 1)                         # no new first-buy row, no new event
        self.assertEqual(len(self.app.db.recent_buys()), 1)                 # Activity (first buys) unchanged
        self.assertEqual(len(self.kinds("tx")), 2)                          # live refresh event for All Buys

        self.buy(2, MINT)          # second wallet, same coin
        self.assertTrue(wait_for(lambda: len(self.p.signals) == 1 and self.p.signals[0]["wallet_count"] == 2))
        multi = [a for a in self.kinds("alert") if a[1].get("kind") == "multi"]
        self.assertEqual(len(multi), 1)

        self.buy(3, MINT)          # third wallet
        self.assertTrue(wait_for(lambda: self.p.signals and self.p.signals[0]["wallet_count"] == 3))
        self.assertTrue(wait_for(lambda: len([a for a in self.kinds("alert") if a[1].get("kind") == "multi"]) == 2))

        chart = self.p.chart()
        self.assertEqual(chart["top"][0]["count"], 3)
        self.assertEqual([pt[1] for pt in chart["series"][0]["points"]], [1, 2, 3])
        state = self.app.state()
        self.assertEqual(len(state["feed"]), 3); self.assertEqual(state["live"]["ws_wallets"], 3)
        self.assertEqual(len(state["all_buys"]), 4)                         # 3 first buys + 1 repeat buy
        self.assertEqual(sum(1 for b in state["all_buys"] if b["is_first"]), 3)
        self.assertTrue(all(b["wallet_label"] and b["delay"] is not None for b in state["all_buys"]))
        self.assertLess(state["live"]["avg_delay"], 5)

    def test_wallet_with_history_does_not_count_as_first_buy(self):
        self.buy(1, MINT2, old_history=True)
        self.assertTrue(wait_for(lambda: self.app.db.has_buy(self.ids[0], MINT2)))
        self.assertEqual(self.app.db.recent_buys(), [])
        self.assertEqual(self.kinds("buy"), [])

    def test_ws_failed_tx_does_not_trigger(self):
        last = [-1]
        def quiet():                       # let the post-subscription catch-up checks finish first
            n, same = len(self.chain.calls), last[0] == len(self.chain.calls)
            last[0] = n
            time.sleep(0.5)
            return same
        self.assertTrue(wait_for(quiet, 10))
        before = len(self.chain.calls)
        self.ws.notify(wallet(1), err={"InstructionError": 1})
        time.sleep(0.8)
        self.assertEqual(len(self.chain.calls), before)


TOKEN_LOGS = ["Program 11111111111111111111111111111111 invoke [1]",
              "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]"]
SOL_ONLY_LOGS = ["Program 11111111111111111111111111111111 invoke [1]", "Program 11111111111111111111111111111111 success"]


class EndToEndBudget(EndToEnd):
    """Counts the real requests per alert (budget: 1 credit per request)."""
    def setUp(self):
        super().setUp()
        import budget, rpc
        self.b = budget.Budget(self.app.db, limit=1000, reserve=0, hosts=("127.0.0.1",))
        self.p.budget = self.b
        rpc.set_budget(self.b)

    def tearDown(self):
        import rpc
        rpc.set_budget(None)
        super().tearDown()

    def requests(self, since):
        return [d for d in self.chain.detail[since:]]

    def test_one_alert_costs_one_or_two_requests_and_no_listing(self):
        w = wallet(1)
        n0, used0 = len(self.chain.detail), self.b.used()
        sig = self.chain.add(w, MINT); self.ws.notify(w, sig=sig, logs=TOKEN_LOGS)
        self.assertTrue(wait_for(lambda: len(self.kinds("buy")) == 1, 4))
        calls = self.requests(n0)
        self.assertEqual(sorted(m for m, _ in calls), ["getSignaturesForAddress", "getTransaction"])
        self.assertEqual([a for m, a in calls if m == "getSignaturesForAddress"], [ata(w, MINT)])  # token account only, never the wallet
        self.assertAlmostEqual(self.b.used() - used0, 2, delta=0.2)   # 2 requests + a few hundredths for websocket data

        n1, used1 = len(self.chain.detail), self.b.used()          # repeat buy of the same coin: ONE request
        sig2 = self.chain.add(w, MINT, pre_raw=5_000_000); self.ws.notify(w, sig=sig2, logs=TOKEN_LOGS)
        self.assertTrue(wait_for(lambda: len(self.app.db.latest_tx_buys()) == 2, 4))
        self.assertEqual([m for m, _ in self.requests(n1)], ["getTransaction"])
        self.assertAlmostEqual(self.b.used() - used1, 1, delta=0.2)

    def test_transactions_without_token_program_cost_nothing(self):
        w = wallet(2)
        n0, used0 = len(self.chain.detail), self.b.used()
        for i in range(5):
            self.ws.notify(w, sig=f"plain-transfer-{i}", logs=SOL_ONLY_LOGS)
        time.sleep(1.0)
        self.assertEqual(self.requests(n0), [])
        self.assertAlmostEqual(self.b.used(), used0, delta=0.5)      # only the tiny websocket data cost

    def test_budget_exhausted_stops_requests_and_says_why(self):
        w = wallet(3)
        self.b.limit = 10
        self.b.add(100)                                             # budget already used up
        n0 = len(self.chain.detail)
        sig = self.chain.add(w, MINT); self.ws.notify(w, sig=sig, logs=TOKEN_LOGS)
        self.assertTrue(wait_for(lambda: "budget" in (self.p.status.get(self.ids[2], {}).get("error") or ""), 10))
        self.assertEqual(self.requests(n0), [])                      # not a single request was sent
        self.assertEqual(self.b.mode(), "paused")


if __name__ == "__main__":
    unittest.main()
