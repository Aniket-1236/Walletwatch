import os, socket, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check, config, db as dbmod
from rpc import RpcError
from test_live import FakeChain, FakeRpcServer, FakeWsServer, wallet, MINT

KEYS = ("HELIUS_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL", "DB_PATH", "USE_WS", "MOCK")


class CheckTool(unittest.TestCase):
    def setUp(self):
        self.keep = {k: os.environ.get(k) for k in KEYS}
        for k in KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.mkdtemp()
        os.environ["DB_PATH"] = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        for k, v in self.keep.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def run_check(self, argv=None):
        lines = []
        code = check.run(argv if argv is not None else [], out=lines.append, folder=self.tmp)
        return code, "\n".join(lines)

    def test_everything_ok_and_key_is_never_printed(self):
        chain = FakeChain(); rpc = FakeRpcServer(chain); ws = FakeWsServer()
        try:
            dbmod.DB(os.environ["DB_PATH"]).add_wallet("solana", wallet(1), "Whale")
            chain.add(wallet(1), MINT)
            os.environ.update(HELIUS_API_KEY="SECRETKEY1234567890", SOLANA_RPC_URL=f"http://127.0.0.1:{rpc.port}",
                              SOLANA_WS_URL=f"ws://127.0.0.1:{ws.port}")
            code, out = self.run_check()
            self.assertEqual(code, 0, out)
            for needle in ("[OK] getSlot works: slot 12345", "[OK] reading your wallet 'Whale'", "[OK] websocket works", "EVERYTHING OK"):
                self.assertIn(needle, out)
            self.assertNotIn("SECRETKEY1234567890", out)                  # only a masked version is shown
        finally:
            rpc.close(); ws.close()

    def test_unreachable_server_gives_plain_advice(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()   # a port nobody listens on
        os.environ.update(SOLANA_RPC_URL=f"http://127.0.0.1:{port}", USE_WS="0")
        import rpc as rpcmod
        old = rpcmod.http_json.__defaults__
        rpcmod.http_json.__defaults__ = (None, None, 3, 0)          # timeout=3, retries=0 so the test is quick
        try:
            code, out = self.run_check()
        finally:
            rpcmod.http_json.__defaults__ = old
        self.assertEqual(code, 1)
        self.assertIn("[FAIL] getSlot", out); self.assertIn("Cannot reach", out)

    def test_save_key_writes_env_and_keeps_other_lines(self):
        with open(os.path.join(self.tmp, ".env"), "w") as f: f.write("APP_PASSWORD=abc\nHELIUS_API_KEY=old\n")
        path = check.save_key("newkey123456789", self.tmp)
        with open(path) as f: got = f.read().splitlines()
        self.assertEqual(got, ["HELIUS_API_KEY=newkey123456789", "APP_PASSWORD=abc"])

    def test_env_txt_from_notepad_is_read(self):
        p = os.path.join(self.tmp, ".env.txt")
        with open(p, "w", encoding="utf-8-sig") as f: f.write("HELIUS_API_KEY=fromnotepad\n")
        self.assertEqual(config.load_dotenv(p), [p])
        self.assertEqual(os.environ["HELIUS_API_KEY"], "fromnotepad")

    def test_error_explanations(self):
        self.assertIn("key is wrong", check.explain(RpcError("HTTP 401 from https://x")))
        self.assertIn("internet", check.explain(RpcError("Network error: <urlopen error>")))


if __name__ == "__main__":
    unittest.main()
