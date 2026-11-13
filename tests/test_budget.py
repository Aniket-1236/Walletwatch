import datetime as dt, json, os, sys, threading, unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import budget, db as dbmod, rpc, solana


def ts(y, m, d, h=12):
    return dt.datetime(y, m, d, h, tzinfo=dt.timezone.utc).timestamp()


class Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t


class BudgetMath(unittest.TestCase):
    def setUp(self):
        self.db = dbmod.DB(":memory:"); self.clk = Clock(ts(2026, 11, 1))

    def make(self, **kw):
        return budget.Budget(self.db, clock=self.clk, **kw)

    def test_billing_period_follows_cycle_day(self):
        b = self.make(cycle_day=1)
        self.assertEqual([p.strftime("%Y-%m-%d") for p in b.period(ts(2026, 11, 20))], ["2026-11-01", "2026-12-01"])
        b = self.make(cycle_day=15)
        self.assertEqual([p.strftime("%Y-%m-%d") for p in b.period(ts(2026, 11, 20))], ["2026-11-15", "2026-12-15"])
        self.assertEqual([p.strftime("%Y-%m-%d") for p in b.period(ts(2026, 11, 3))], ["2026-10-15", "2026-11-15"])
        self.assertEqual([p.strftime("%Y-%m-%d") for p in b.period(ts(2027, 1, 3))], ["2026-12-15", "2027-01-15"])

    def test_counts_are_saved_and_survive_restart(self):
        b = self.make(); b.add(5); b.add_ws_bytes(100_000); b.flush()          # 0.1 MB = 2 credits
        self.assertAlmostEqual(b.used(), 7)
        again = self.make()
        self.assertAlmostEqual(again.used(), 7); self.assertAlmostEqual(again.today_used(), 7)

    def test_new_month_and_new_day_reset_the_right_counter(self):
        b = self.make(); b.add(100)
        self.clk.t = ts(2026, 11, 2); self.assertEqual((b.used(), b.today_used()), (100, 0))
        self.clk.t = ts(2026, 12, 1); self.assertEqual((b.used(), b.today_used()), (0, 0))

    def test_daily_allowance_and_modes(self):
        b = self.make(limit=1_000_000, reserve=0.08)
        self.clk.t = ts(2026, 11, 1, 0)
        self.assertAlmostEqual(b.daily_allowance(), 920_000 / 30, delta=5)
        self.assertEqual(b.mode(), "normal")
        self.clk.t = ts(2026, 11, 11, 0); b.add(0); b._used = b._today = 0; b.add(0)
        b._used = 500_000                                             # 10 days in, already 500k used -> will overspend
        self.assertEqual(b.mode(), "lean")
        b._used = 100_000; self.assertEqual(b.mode(), "normal")
        b._used = 920_000; self.assertEqual(b.mode(), "paused")
        with self.assertRaises(budget.BudgetExceeded) as cm:
            b.check()
        self.assertIn("920,000", str(cm.exception))

    def test_snapshot_and_disabled(self):
        self.assertEqual(self.make(enabled=False).snapshot(), {"enabled": False})
        snap = self.make().snapshot()
        self.assertEqual((snap["enabled"], snap["limit"], snap["usable"], snap["mode"]), (True, 1_000_000, 920_000, "normal"))


class Count(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 1}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class RpcHook(unittest.TestCase):
    def test_every_request_is_counted_and_blocked_at_the_limit(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), Count); threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        b = budget.Budget(dbmod.DB(":memory:"), limit=100, reserve=0.05, hosts=("127.0.0.1",))   # usable = 95
        rpc.set_budget(b)
        try:
            for _ in range(95):
                rpc.rpc_call(url, "x", [])
            self.assertEqual(b.used(), 95)
            with self.assertRaises(budget.BudgetExceeded):
                rpc.rpc_call(url, "x", [])
            self.assertEqual(b.used(), 95)                              # the blocked request was not sent or counted
        finally:
            rpc.set_budget(None); srv.shutdown(); srv.server_close()

    def test_other_hosts_are_not_counted(self):
        b = budget.Budget(dbmod.DB(":memory:"), hosts=("helius-rpc.com",))
        self.assertTrue(b.counts("mainnet.helius-rpc.com")); self.assertFalse(b.counts("api.mainnet-beta.solana.com"))


class Filters(unittest.TestCase):
    def test_log_prefilter_is_conservative(self):
        tok = "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]"
        self.assertTrue(solana.may_be_buy([tok]))
        self.assertTrue(solana.may_be_buy(["Program TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb invoke [2]"]))
        self.assertFalse(solana.may_be_buy(["Program 11111111111111111111111111111111 invoke [1]"]))
        self.assertTrue(solana.may_be_buy([]) and solana.may_be_buy(None))              # unknown -> never skipped
        self.assertTrue(solana.may_be_buy(["Log truncated"]))

    def test_seen_signatures_are_bounded(self):
        s = solana.SeenSigs(cap=3)
        for i in range(5): s.add(f"s{i}")
        self.assertEqual((len(s), "s0" in s, "s4" in s), (3, False, True))


if __name__ == "__main__":
    unittest.main()
