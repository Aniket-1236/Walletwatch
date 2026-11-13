"""Website (WSGI) + background worker sharing one database file: how PythonAnywhere runs the project."""
import base64, importlib, io, json, os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from wsgiref.util import setup_testing_defaults

W1, W2 = "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
KEYS = ("DB_PATH", "MOCK", "APP_PASSWORD", "APP_PASSWORD_HASH", "APP_USER")


class SplitBase(unittest.TestCase):
    def setUp(self):
        self.keep = {k: os.environ.get(k) for k in KEYS}
        for k in ("APP_PASSWORD", "APP_USER"):
            os.environ.pop(k, None)
        os.environ["MOCK"] = "1"
        os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "shared.db")
        import wsgi_app
        self.web = importlib.reload(wsgi_app)                  # website process, opens DB_PATH
        import server, worker
        self.worker_mod = worker
        self.wapp = server.App()                               # worker process: the same file, but the full poller

    def tearDown(self):
        for k, v in self.keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def call(self, method, path, body=None, headers=None):
        env = {}
        setup_testing_defaults(env)
        raw = json.dumps(body).encode() if body is not None else b""
        env.update(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING="", CONTENT_LENGTH=str(len(raw)),
                   CONTENT_TYPE="application/json", **{"wsgi.input": io.BytesIO(raw)})
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}
        res = self.web.application(env, lambda status, hdrs, exc=None: out.update(status=status, headers=dict(hdrs)))
        payload = b"".join(res)
        return out["status"], out["headers"], payload

    def jget(self, path):
        status, _, payload = self.call("GET", path)
        self.assertTrue(status.startswith("200"), status)
        return json.loads(payload)


class WsgiRoutes(SplitBase):
    def test_pages_state_wallets_and_healthz(self):
        status, hdr, body = self.call("GET", "/")
        self.assertEqual((status[:3], hdr["Content-Type"]), ("200", "text/html; charset=utf-8"))
        self.assertIn(b"WalletWatch", body)
        self.assertEqual(self.call("GET", "/sw.js")[0][:3], "200")
        self.assertEqual(self.jget("/healthz"), {"ok": True})
        st = self.jget("/api/state")
        self.assertEqual((st["web"], st["worker"]["alive"], st["wallets"]), (True, False, []))

        status, _, body = self.call("POST", "/api/wallets", {"chain": "solana", "address": W1, "label": "Alpha"})
        self.assertEqual(status[:3], "201", body)
        self.assertEqual([w["label"] for w in self.jget("/api/state")["wallets"]], ["Alpha"])   # visible at once
        wid = self.jget("/api/state")["wallets"][0]["id"]
        self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": "bad"})[0][:3], "400")
        self.assertEqual(self.call("DELETE", f"/api/wallets/{wid}")[0][:3], "200")
        self.assertEqual(self.jget("/api/state")["wallets"], [])
        status, hdr, body = self.call("GET", "/api/stream")                   # no long-lived connection on WSGI
        self.assertEqual((status[:3], hdr["Content-Type"], body), ("200", "text/event-stream", b"retry: 600000\n\n"))
        self.assertEqual(self.call("GET", "/nope.exe")[0][:3], "404")

    def test_password_protection_works_through_wsgi(self):
        import auth
        os.environ["APP_PASSWORD_HASH"] = auth.hash_password("s3cret-long-phrase", iterations=1000)
        try:
            self.assertEqual(self.call("GET", "/api/state")[0][:3], "401")
            status, hdr, _ = self.call("POST", "/api/login", {"password": "wrong"})
            self.assertEqual(status[:3], "401")
            status, hdr, _ = self.call("POST", "/api/login", {"password": "s3cret-long-phrase"})
            self.assertEqual(status[:3], "200")
            cookie = {"Cookie": hdr["Set-Cookie"].split(";")[0]} if "Set-Cookie" in hdr else None
            self.assertIsNotNone(cookie)
            self.assertEqual(self.call("GET", "/api/state", headers=cookie)[0][:3], "200")
            self.assertEqual(self.call("POST", "/api/wallets", {"chain": "solana", "address": W1}, headers=cookie)[0][:3], "201")
        finally:
            os.environ.pop("APP_PASSWORD_HASH", None)


class WorkerRoundTrip(SplitBase):
    def test_worker_results_appear_on_the_website(self):
        for i, a in enumerate((W1, W2, "Es9vMFrzaCERmJfrF4H2FYD4KCoNyFpyNwYbaXeS4WbG", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")):
            self.call("POST", "/api/wallets", {"chain": "solana", "address": a, "label": f"W{i}"})
        self.assertEqual(self.jget("/api/state")["signals"], [])             # worker has not run yet
        self.wapp.poller.poll_once(); self.wapp.poller.poll_once()
        self.worker_mod.publish(self.wapp, {})
        st = self.jget("/api/state")
        self.assertTrue(st["worker"]["alive"])
        self.assertGreater(len(st["feed"]), 0); self.assertGreater(len(st["all_buys"]), 0)
        self.assertEqual(len(st["wallets"]), 4)
        self.assertTrue(all(w["status"] and w["status"]["ok"] for w in st["wallets"]))   # statuses come from the worker
        self.assertEqual(st["chart"]["top"], self.wapp.state()["chart"]["top"])
        self.assertEqual([s["token"] for s in st["signals"]], [s["token"] for s in self.wapp.state()["signals"]])
        # a wallet added on the website shows immediately even before the worker has seen it
        self.call("POST", "/api/wallets", {"chain": "solana", "address": "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "label": "New"})
        new = [w for w in self.jget("/api/state")["wallets"] if w["label"] == "New"][0]
        self.assertIsNone(new["status"])

    def test_dead_worker_is_reported(self):
        self.worker_mod.publish(self.wapp, {})
        self.assertTrue(self.jget("/api/state")["worker"]["alive"])
        self.wapp.db.set_setting("snapshot_ts", time.time() - 120)
        self.assertFalse(self.jget("/api/state")["worker"]["alive"])

    def test_website_requests_reach_the_worker(self):
        calls = []
        self.wapp.poller.trigger = lambda *a, **k: calls.append("trigger")
        self.wapp.poller.evaluate = lambda *a, **k: calls.append("evaluate")
        seen = {}
        self.worker_mod.process_kicks(self.wapp, seen)                       # first look only records the starting values
        time.sleep(0.05)
        self.assertEqual(calls, [])
        self.call("POST", "/api/settings", {"min_wallets": 3})              # website asks for a recalculation
        end = time.time() + 3                                                # (the website does this in a thread)
        while self.wapp.db.get_setting("kick") is None and time.time() < end:
            time.sleep(0.02)
        self.call("POST", "/api/poll", {})                                   # website asks for "check now"
        self.worker_mod.process_kicks(self.wapp, seen)
        end = time.time() + 3
        while len(calls) < 2 and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(sorted(calls), ["evaluate", "trigger"])
        self.assertEqual(self.jget("/api/state")["settings"]["min_wallets"], 3)   # the setting itself is shared via the file
        self.worker_mod.process_kicks(self.wapp, seen); time.sleep(0.1)
        self.assertEqual(len(calls), 2)                                      # no repeat without a new request

    def test_default_database_path_is_absolute_and_shared(self):
        os.environ.pop("DB_PATH", None)
        import config
        self.assertTrue(os.path.isabs(config.db_path()))
        self.assertTrue(config.db_path().endswith("walletwatch.db"))


if __name__ == "__main__":
    unittest.main()
