import os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MOCK"] = "1"
import db as dbmod, server

W = "So11111111111111111111111111111111111111112"


class AllBuysDb(unittest.TestCase):
    def test_every_tx_is_stored_but_first_buy_only_once(self):
        d = dbmod.DB(":memory:")
        w = d.add_wallet("solana", W, "Whale")
        self.assertTrue(d.add_tx_buy(w, "solana", "T1", "AAA", 1, "tx1", 100.0))
        self.assertTrue(d.add_tx_buy(w, "solana", "T1", "AAA", 2, "tx2", 200.0))      # repeat buy of same coin
        self.assertFalse(d.add_tx_buy(w, "solana", "T1", "AAA", 2, "tx2", 200.0))     # same tx twice is ignored
        self.assertTrue(d.add_buy(w, "solana", "T1", "AAA", 1, "tx1", 100.0))
        self.assertFalse(d.add_buy(w, "solana", "T1", "AAA", 2, "tx2", 200.0))
        rows = d.latest_tx_buys()
        self.assertEqual([r["tx"] for r in rows], ["tx2", "tx1"])                      # newest first
        self.assertEqual([r["is_first"] for r in rows], [0, 1])
        self.assertEqual(len(d.recent_buys()), 1)                                      # Activity tab unchanged

    def test_symbol_filled_in_later_reaches_all_buys(self):
        d = dbmod.DB(":memory:")
        w = d.add_wallet("solana", W, "Whale")
        d.add_tx_buy(w, "solana", "T1", None, 1, "tx1", 100.0)
        d.add_buy(w, "solana", "T1", None, 1, "tx1", 100.0)
        d.set_symbol("solana", "T1", "PEPE")
        self.assertEqual(d.latest_tx_buys()[0]["symbol"], "PEPE")

    def test_old_database_gets_new_column(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        c = sqlite3.connect(path)
        c.executescript("""CREATE TABLE wallets(id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL, address TEXT NOT NULL,
            label TEXT NOT NULL, added_at REAL NOT NULL, UNIQUE(chain, address));
            CREATE TABLE buys(id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id INTEGER NOT NULL, chain TEXT NOT NULL,
            token TEXT NOT NULL, symbol TEXT, amount REAL, tx TEXT NOT NULL, ts REAL NOT NULL, UNIQUE(wallet_id, tx, token));
            INSERT INTO wallets(chain,address,label,added_at) VALUES('solana','%s','Old',1);
            INSERT INTO buys(wallet_id,chain,token,symbol,amount,tx,ts) VALUES(1,'solana','OLDTOK','OLD',5,'oldtx',50);""" % W)
        c.commit(); c.close()
        d = dbmod.DB(path)
        rows = d.latest_tx_buys()
        self.assertEqual((rows[0]["symbol"], rows[0]["wallet_label"], rows[0]["detected_at"]), ("OLD", "Old", None))
        self.assertTrue(d.add_tx_buy(1, "solana", "NEW", "N", 1, "newtx", 60.0))


class AllBuysState(unittest.TestCase):
    def test_state_lists_latest_buys_and_graph_needs_two_wallets(self):
        app = server.App(os.path.join(tempfile.mkdtemp(), "s.db"))
        a = app.db.add_wallet("solana", W, "A")
        b = app.db.add_wallet("solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "B")
        import time
        now = time.time()
        for wid, tx, ts in ((a, "t1", now - 30), (a, "t2", now - 20), (b, "t3", now - 10)):
            app.db.add_tx_buy(wid, "solana", "COIN1", "PEPE", 5, tx, ts, now)
        app.db.add_buy(a, "solana", "COIN1", "PEPE", 5, "t1", now - 30, True, now)
        app.poller.evaluate()
        st = app.state()
        self.assertEqual([x["wallet_label"] for x in st["all_buys"]], ["B", "A", "A"])
        self.assertEqual([x["is_first"] for x in st["all_buys"]], [False, False, True])
        self.assertEqual(st["chart"]["top"], [])                       # only one wallet bought it as a FIRST buy
        app.db.add_buy(b, "solana", "COIN1", "PEPE", 5, "t3", now - 10, True, now)
        app.poller.evaluate()
        top = app.state()["chart"]["top"]
        self.assertEqual((top[0]["symbol"], top[0]["count"], sorted(top[0]["wallets"])), ("PEPE", 2, ["A", "B"]))

    def test_names_are_looked_up_for_every_coin_on_screen_and_exposed(self):
        app = server.App(os.path.join(tempfile.mkdtemp(), "n.db"))
        a = app.db.add_wallet("solana", W, "A")
        import time
        now = time.time()
        app.db.add_tx_buy(a, "solana", "DEMO1", None, 5, "x1", now - 5, now)       # a repeat buy only: no first-buy row
        app.db.add_buy(a, "solana", "DEMO1", None, 5, "x0", now - 50, False, now)
        asked = []
        orig = app.poller._schedule_market
        app.poller._schedule_market = lambda keys: (asked.extend(keys), orig(keys))
        app.poller.evaluate()
        self.assertIn(("solana", "DEMO1"), asked)                                    # still gets its name looked up
        row = app.state()["all_buys"][0]
        self.assertEqual((row["symbol"], row["name"], row["token"]), ("DEMOAI", "Demo AI Coin", "DEMO1"))


if __name__ == "__main__":
    unittest.main()
