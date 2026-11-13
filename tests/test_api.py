import base64, http.client, json, os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MOCK"] = "1"
for k in ("APP_PASSWORD", "NTFY_TOPIC", "TELEGRAM_BOT_TOKEN"):
    os.environ.pop(k, None)
import server

A = "So11111111111111111111111111111111111111112"
B = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
C = "Es9vMFrzaCERmJfrF4H2FYD4KCoNyFpyNwYbaXeS4WbG"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = server.App(os.path.join(cls.tmp, "t.db"))
        cls.srv = server.make_server(cls.app, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        r = c.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def test_1_static_and_health(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        st, html = self.call("GET", "/")
        self.assertEqual(st, 200); self.assertIn(b"WalletWatch", html)
        for p in ("/manifest.json", "/sw.js", "/icon-192.png", "/icon-512.png"):
            self.assertEqual(self.call("GET", p)[0], 200, p)
        for p in ("/server.py", "/../server.py", "/static/../config.py", "/nope"):
            self.assertEqual(self.call("GET", p)[0], 404, p)

    def test_2_wallet_validation_and_crud(self):
        J = {"Content-Type": "application/json"}
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "0xabc"}, J)[0], 400)
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "bogus", "address": A}, J)[0], 400)
        st, w = self.call("POST", "/api/wallets", {"chain": "solana", "address": A, "label": "Whale <b>1</b>"}, J)
        self.assertEqual(st, 201)
        st, dup = self.call("POST", "/api/wallets", {"chain": "solana", "address": A}, J)
        self.assertEqual(st, 400); self.assertIn("already", dup["error"])
        self.assertEqual(self.call("POST", "/api/settings", {"min_wallets": 99}, J)[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"window_hours": 5}, J)[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"min_wallets": 2, "window_hours": 6}, J)[0], 200)
        self.assertEqual(self.call("DELETE", "/api/wallets/abc")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/wallets/9999")[0], 404)
        self.assertEqual(self.call("DELETE", f"/api/wallets/{w['id']}")[0], 200)
        st, state = self.call("GET", "/api/state")
        self.assertNotIn(A, [x["address"] for x in state["wallets"]])

    def test_3_convergence_alert_flow_and_sse(self):
        db, p = self.app.db, self.app.poller
        ids = [db.add_wallet("solana", a, n) for a, n in ((A, "Alpha"), (B, "Beta"), (C, "Gamma"))]
        now = time.time()
        db.add_buy(ids[0], "solana", "DEMO1", None, 100, "tx-a", now - 30)
        p.evaluate()
        self.assertEqual(self.call("GET", "/api/state")[1]["signals"], [])   # one wallet alone is not a signal

        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)   # phone listening for live events
        c.request("GET", "/api/stream"); resp = c.getresponse()
        events = []
        def reader():
            ev = None
            try:
                while True:
                    line = resp.fp.readline().decode()
                    if not line: break
                    if line.startswith("event:"): ev = line.split(":", 1)[1].strip()
                    elif line.startswith("data:") and ev: events.append((ev, json.loads(line[5:]))); ev = None
            except Exception: pass
        threading.Thread(target=reader, daemon=True).start()
        time.sleep(0.3)

        db.add_buy(ids[1], "solana", "DEMO1", None, 200, "tx-b", now - 10)   # second wallet, same token
        p.evaluate(); time.sleep(0.5)
        state = self.call("GET", "/api/state")[1]
        self.assertEqual(len(state["signals"]), 1)
        sig = state["signals"][0]
        self.assertEqual((sig["symbol"], sig["wallet_count"]), ("DEMOAI", 2))
        self.assertEqual(sorted(w["label"] for w in sig["wallets"]), ["Alpha", "Beta"])
        alerts = [e for e in events if e[0] == "alert"]
        self.assertEqual(len(alerts), 1); self.assertIn("2 wallets", alerts[0][1]["title"])

        p.evaluate(); time.sleep(0.3)                                          # re-evaluating must not repeat the alert
        self.assertEqual(len([e for e in events if e[0] == "alert"]), 1)
        db.add_buy(ids[2], "solana", "DEMO1", None, 300, "tx-c", now - 5)     # third wallet joins -> new alert
        p.evaluate(); time.sleep(0.5)
        self.assertEqual(len([e for e in events if e[0] == "alert"]), 2)
        self.assertEqual(self.call("GET", "/api/state")[1]["signals"][0]["wallet_count"], 3)
        self.assertGreaterEqual(len(self.call("GET", "/api/state")[1]["alerts"]), 2)

    def test_4_old_history_records_but_does_not_page(self):
        db, p = self.app.db, self.app.poller
        ids = [db.add_wallet("ethereum", "0x" + "1" * 40, "E1"), db.add_wallet("ethereum", "0x" + "2" * 40, "E2")]
        q = self.app.hub.subscribe()
        old = time.time() - 4 * 3600
        db.add_buy(ids[0], "ethereum", "DEMO4", "DEMOSWAP", 5, "old1", old)
        db.add_buy(ids[1], "ethereum", "DEMO4", "DEMOSWAP", 5, "old2", old)
        p.evaluate()
        kinds = []
        while not q.empty(): kinds.append(q.get_nowait()[0])
        self.assertNotIn("alert", kinds)
        self.assertTrue(any(s["token"] == "DEMO4" for s in self.call("GET", "/api/state")[1]["signals"]))

    def test_5_poll_once_in_demo_mode_reports_status(self):
        self.app.poller.poll_once()
        state = self.call("GET", "/api/state")[1]
        self.assertTrue(state["mock"])
        self.assertTrue(all(w["status"] and w["status"]["ok"] for w in state["wallets"]))
        self.assertTrue(state["feed"])

    def call_full(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        r = c.getresponse()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), r.read(), r.getheader("Set-Cookie")

    def test_6_password_protection(self):
        import auth
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password("a very long secret phrase", iterations=1000)
        try:
            self.assertEqual(self.call("GET", "/api/state")[0], 401)
            self.assertEqual(self.call("GET", "/healthz")[0], 200)
            st, hdr, body, _ = self.call_full("GET", "/")
            self.assertIn(b"Password", body); self.assertNotIn(b"/api/state", body)       # only the login form, no panel
            st, _, js, _ = self.call_full("GET", "/app.js")                              # panel code is NOT served before login
            self.assertNotIn(b"renderSignals", js); self.assertIn(b"Password", js)
            self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": A}, {})[0], 401)
            st, _, _, cookie = self.call_full("POST", "/api/login", {"password": "wrong password"}, {})
            self.assertEqual((st, cookie), (401, None))
            st, _, _, cookie = self.call_full("POST", "/api/login", {"password": "a very long secret phrase"}, {})
            self.assertEqual(st, 200)
            self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)
            ck = {"Cookie": cookie.split(";")[0]}
            self.assertEqual(self.call("GET", "/api/state", headers=ck)[0], 200)
            self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}, ck)[0], 201)
        finally:
            os.environ.pop("APP_PASSWORD_HASH")


class LiveModeErrors(unittest.TestCase):
    def test_missing_rpc_is_reported_per_wallet_not_crash(self):
        os.environ.pop("MOCK", None)
        os.environ.pop("ARC_RPC_URL", None)
        try:
            app = server.App(":memory:")
            wid = app.db.add_wallet("arc", "0x" + "3" * 40, "ArcWallet")
            app.poller.poll_once()
            st = app.poller.status[wid]
            self.assertFalse(st["ok"]); self.assertIn("ARC_RPC_URL", st["error"])
        finally:
            os.environ["MOCK"] = "1"

    def test_network_down_is_reported_not_crash(self):
        os.environ.pop("MOCK", None)
        os.environ["SOLANA_RPC_URL"] = "http://127.0.0.1:9"   # nothing listens here
        try:
            app = server.App(":memory:")
            wid = app.db.add_wallet("solana", A, "S1")
            app.poller.poll_once()
            self.assertFalse(app.poller.status[wid]["ok"])
            self.assertIn("Network error", app.poller.status[wid]["error"])
        finally:
            os.environ["MOCK"] = "1"; os.environ.pop("SOLANA_RPC_URL")


if __name__ == "__main__":
    unittest.main()
