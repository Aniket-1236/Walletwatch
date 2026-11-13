import json, os, sys, threading, time, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rpc


class Flaky(BaseHTTPRequestHandler):
    hits = 0
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).hits += 1
        if type(self).hits <= 2:
            self.send_response(429); self.send_header("Retry-After", "0"); self.send_header("Content-Length", "0"); self.end_headers(); return
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 42}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class RateLimitTests(unittest.TestCase):
    def test_429_is_retried_until_success(self):
        Flaky.hits = 0
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertEqual(rpc.rpc_call(f"http://127.0.0.1:{srv.server_address[1]}", "x", []), 42)
            self.assertEqual(Flaky.hits, 3)
        finally:
            srv.shutdown(); srv.server_close()

    def test_persistent_429_raises_friendly_error(self):
        Flaky.hits = -100
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(rpc.RateLimited) as cm:
                rpc.http_json(f"http://127.0.0.1:{srv.server_address[1]}", {"a": 1}, retries=1)
            self.assertIn("SOLANA_RPC_URL", str(cm.exception))
        finally:
            srv.shutdown(); srv.server_close()

    def test_public_solana_host_is_spaced_out(self):
        lim = rpc._Limiter()
        t = time.time()
        for _ in range(4):
            lim.wait("api.mainnet-beta.solana.com")
        self.assertGreater(time.time() - t, 3 / 2.5 - 0.05)       # 4 calls need >= 3 gaps
        self.assertEqual(rpc._Limiter.rate_for("127.0.0.1"), 0)
        self.assertEqual(rpc._Limiter.rate_for("example.quiknode.pro"), 20.0)


class HeliusConfig(unittest.TestCase):
    def setUp(self):
        self.keep = {k: os.environ.get(k) for k in ("HELIUS_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL", "HELIUS_RPS", "RPC_MAX_RPS")}
        for k in self.keep:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_key_builds_rpc_and_websocket_urls(self):
        import config
        self.assertEqual(config.chain_cfg("solana")["rpc"], "https://api.mainnet-beta.solana.com")
        os.environ["HELIUS_API_KEY"] = "abc-123"
        self.assertEqual(config.chain_cfg("solana")["rpc"], "https://mainnet.helius-rpc.com/?api-key=abc-123")
        self.assertEqual(config.solana_ws_url(), "wss://mainnet.helius-rpc.com/?api-key=abc-123")
        os.environ["SOLANA_RPC_URL"] = "https://other.example/rpc"      # an explicit URL always wins
        self.assertEqual(config.chain_cfg("solana")["rpc"], "https://other.example/rpc")

    def test_helius_is_held_under_10_requests_per_second(self):
        self.assertEqual(rpc._Limiter.rate_for("mainnet.helius-rpc.com"), 9.0)
        lim = rpc._Limiter(); t = time.time()
        for _ in range(19):
            lim.wait("mainnet.helius-rpc.com")
        self.assertGreater(time.time() - t, 18 / 9.0 - 0.05)           # 19 calls need 18 gaps of 1/9 s
        self.assertLess(19 / (time.time() - t + 1 / 9.0), 10.0)

    def test_dotenv_file_is_loaded_without_overriding_real_env(self):
        import config, tempfile
        p = os.path.join(tempfile.mkdtemp(), ".env")
        open(p, "w", encoding="utf-8").write("# c\nHELIUS_API_KEY = \"fromfile\"\nHELIUS_RPS=7\n")
        os.environ["HELIUS_RPS"] = "5"
        config.load_dotenv(p)
        self.assertEqual(os.environ["HELIUS_API_KEY"], "fromfile")
        self.assertEqual(os.environ["HELIUS_RPS"], "5")


if __name__ == "__main__":
    unittest.main()
