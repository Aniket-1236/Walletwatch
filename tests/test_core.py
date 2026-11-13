import os, sys, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import config, evm, solana, signals, market
from rpc import RpcError

W = "WaLLetWaLLetWaLLetWaLLetWaLLetWaLLet111111"
TOKEN = "TokenMint1111111111111111111111111111111111"
OTHER = "OtherMint111111111111111111111111111111111"


def sol_tx(pre_tok, post_tok, pre_sol, post_sol, fee=5000, err=None, sig="sig1"):
    return {"blockTime": 1_700_000_000,
            "transaction": {"signatures": [sig], "message": {"accountKeys": [{"pubkey": W}, {"pubkey": "Other"}]}},
            "meta": {"err": err, "fee": fee, "preBalances": [pre_sol, 0], "postBalances": [post_sol, 0],
                     "preTokenBalances": pre_tok, "postTokenBalances": post_tok}}


def tb(mint, raw, dec=6, owner=W):
    return {"mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(raw), "decimals": dec}}


class SolanaParse(unittest.TestCase):
    def test_swap_sol_for_token_is_a_buy(self):
        tx = sol_tx([], [tb(TOKEN, 5_000_000)], 2_000_000_000, 1_000_000_000)
        buys = solana.parse_solana_tx(tx, W)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["token"], TOKEN)
        self.assertAlmostEqual(buys[0]["amount"], 5.0)
        self.assertEqual(buys[0]["tx"], "sig1")

    def test_token_for_token_swap_is_a_buy(self):
        tx = sol_tx([tb(OTHER, 9_000_000)], [tb(OTHER, 1_000_000), tb(TOKEN, 3_000_000)], 1_000_000_000, 1_000_000_000 - 5000)
        self.assertEqual([b["token"] for b in solana.parse_solana_tx(tx, W)], [TOKEN])

    def test_incoming_transfer_without_spend_is_not_a_buy(self):
        tx = sol_tx([], [tb(TOKEN, 1_000_000)], 1_000_000_000, 1_000_000_000 - 5000)  # only the fee left the wallet
        self.assertEqual(solana.parse_solana_tx(tx, W), [])

    def test_selling_is_not_a_buy(self):
        tx = sol_tx([tb(TOKEN, 5_000_000)], [], 1_000_000_000, 1_500_000_000)
        self.assertEqual(solana.parse_solana_tx(tx, W), [])

    def test_failed_tx_and_stable_ignored(self):
        self.assertEqual(solana.parse_solana_tx(sol_tx([], [tb(TOKEN, 1)], 10**9, 0, err={"x": 1}), W), [])
        usdc = sol_tx([], [tb(solana.USDC, 5_000_000)], 10**9, 10**8)
        self.assertEqual(solana.parse_solana_tx(usdc, W), [])

    def test_other_owners_balances_are_ignored(self):
        tx = sol_tx([], [tb(TOKEN, 5_000_000, owner="SomeoneElse")], 2 * 10**9, 10**9)
        self.assertEqual(solana.parse_solana_tx(tx, W), [])


class SolanaPoll(unittest.TestCase):
    def setUp(self):
        self.orig = solana.rpc_call

    def tearDown(self):
        solana.rpc_call = self.orig

    def test_cursor_and_seed_window(self):
        now = 1_700_000_100
        sigs = [{"signature": "new", "err": None, "blockTime": now - 10},
                {"signature": "old", "err": None, "blockTime": now - 999_999}]  # newest first
        txs = {"new": sol_tx([], [tb(TOKEN, 1_000_000)], 10**9, 5 * 10**8, sig="new")}
        calls = []

        def fake(url, method, params):
            calls.append(method)
            return sigs if method == "getSignaturesForAddress" else txs[params[0]]
        solana.rpc_call = fake
        buys, cur = solana.poll_wallet("u", W, None, 6 * 3600, now=now)
        self.assertEqual([b["tx"] for b in buys], ["new"])
        self.assertEqual(cur, "new")
        self.assertEqual(calls.count("getTransaction"), 1)  # the day-old signature was not fetched

    def test_failed_fetch_does_not_skip_transactions(self):
        sigs = [{"signature": "b", "err": None, "blockTime": 1}, {"signature": "a", "err": None, "blockTime": 0}]

        def fake(url, method, params):
            if method == "getSignaturesForAddress":
                return sigs
            if params[0] == "a":
                return sol_tx([], [tb(TOKEN, 1_000_000)], 10**9, 5 * 10**8, sig="a")
            raise RpcError("boom")
        solana.rpc_call = fake
        buys, cur = solana.poll_wallet("u", W, "prev", 10**9)
        self.assertEqual([b["tx"] for b in buys], ["a"])
        self.assertEqual(cur, "a")  # "b" will be retried next poll


EW = "0x" + "ab" * 20
def topic(a): return "0x" + "0" * 24 + a[2:]
TK, TK2 = "0x" + "11" * 20, "0x" + "22" * 20
def log(token, frm, to, val, tx, block=100):
    return {"address": token, "topics": [evm.TRANSFER_TOPIC, topic(frm), topic(to)], "data": hex(val),
            "transactionHash": tx, "blockNumber": hex(block)}


class EvmDetect(unittest.TestCase):
    def test_buy_with_native_value(self):
        inc = [log(TK, "0x" + "cc" * 20, EW, 10**18, "0xt1")]
        look = lambda h: {"from": EW, "value": hex(10**17)}
        b = evm.detect_evm_buys(EW, inc, [], look)
        self.assertEqual((b[0]["token"], b[0]["raw"]), (TK, 10**18))

    def test_buy_paying_with_another_token(self):
        inc = [log(TK, "0x" + "cc" * 20, EW, 5, "0xt2")]
        out = [log(TK2, EW, "0x" + "cc" * 20, 9, "0xt2")]
        self.assertEqual(len(evm.detect_evm_buys(EW, inc, out, lambda h: None)), 1)

    def test_plain_transfer_and_nft_skipped(self):
        inc = [log(TK, "0x" + "cc" * 20, EW, 5, "0xt3")]
        self.assertEqual(evm.detect_evm_buys(EW, inc, [], lambda h: {"from": "0x" + "cc" * 20, "value": "0x0"}), [])
        nft = {"address": TK, "topics": [evm.TRANSFER_TOPIC, topic("0x" + "cc" * 20), topic(EW), "0x1"], "data": "0x",
               "transactionHash": "0xt4", "blockNumber": "0x64"}
        self.assertEqual(evm.detect_evm_buys(EW, [nft], [], lambda h: {"from": EW, "value": "0x5"}), [])

    def test_ignore_list(self):
        inc = [log(TK, "0x" + "cc" * 20, EW, 5, "0xt5")]
        self.assertEqual(evm.detect_evm_buys(EW, inc, [], lambda h: {"from": EW, "value": "0x5"}, [TK]), [])

    def test_poll_wallet_end_to_end_with_stubbed_rpc(self):
        orig = evm.rpc_call
        evm._symbols.clear(); evm._block_ts.clear()
        sym = "0x" + "20".rjust(64, "0") + "3".rjust(64, "0") + b"ABC".hex().ljust(64, "0")

        def fake(url, method, params):
            if method == "eth_blockNumber": return hex(1000)
            if method == "eth_getLogs":
                is_incoming = len(params[0]["topics"]) == 3
                return [log(TK, "0x" + "cc" * 20, EW, 2 * 10**6, "0xbuy", 990)] if is_incoming else []
            if method == "eth_getTransactionByHash": return {"from": EW, "value": hex(10**16)}
            if method == "eth_getBlockByNumber": return {"timestamp": hex(1_700_000_000)}
            if method == "eth_call": return sym if params[0]["data"] == "0x95d89b41" else hex(6)
            raise AssertionError(method)
        evm.rpc_call = fake
        try:
            buys, cur = evm.poll_wallet("test", "u", EW, None, 300)
        finally:
            evm.rpc_call = orig
        self.assertEqual(cur, 1000)
        self.assertEqual((buys[0]["symbol"], buys[0]["amount"], buys[0]["ts"]), ("ABC", 2.0, 1_700_000_000))


def mk(w, label, token="T1", ts=None, chain="solana", sym=None):
    return {"wallet_id": w, "wallet_label": label, "chain": chain, "token": token, "ts": ts, "amount": 1, "symbol": sym}


class Signals(unittest.TestCase):
    now = 1_000_000

    def test_needs_distinct_wallets_in_window(self):
        buys = [mk(1, "A", ts=self.now - 60), mk(1, "A", ts=self.now - 30),   # same wallet twice = 1
                mk(2, "B", token="T2", ts=self.now - 60), mk(3, "C", token="T2", ts=self.now - 99_999)]  # C outside window
        self.assertEqual(signals.compute_signals(buys, {}, self.now, 3600, 2), [])

    def test_two_wallets_make_a_signal_and_liquidity_ranks_it(self):
        buys = [mk(1, "A", "GOOD", self.now - 100), mk(2, "B", "GOOD", self.now - 200),
                mk(1, "A", "THIN", self.now - 100), mk(2, "B", "THIN", self.now - 200)]
        mkt = {("solana", "GOOD"): {"symbol": "GOOD", "liquidity_usd": 500_000, "change_24h": 10},
               ("solana", "THIN"): {"symbol": "THIN", "liquidity_usd": 6_000, "change_24h": 900}}
        out = signals.compute_signals(buys, mkt, self.now, 3600, 2)
        self.assertEqual([s["symbol"] for s in out], ["GOOD", "THIN"])
        self.assertGreater(out[0]["score"], out[1]["score"])
        self.assertTrue(any("liquidity" in f["text"].lower() for f in out[1]["flags"]))
        self.assertTrue(any("up 900%" in f["text"] for f in out[1]["flags"]))
        self.assertTrue(0 <= out[1]["score"] <= 100 and out[0]["rating"] in ("Strong", "Moderate", "Weak"))

    def test_more_wallets_score_higher_and_stables_are_dropped(self):
        buys = [mk(i, str(i), "X", self.now - 60) for i in range(1, 5)] + [mk(1, "1", "Y", self.now - 60), mk(2, "2", "Y", self.now - 60)]
        buys += [mk(1, "1", "U", self.now - 60), mk(2, "2", "U", self.now - 60)]
        mkt = {("solana", "U"): {"symbol": "USDC", "liquidity_usd": 10**9}}
        out = signals.compute_signals(buys, mkt, self.now, 3600, 2)
        self.assertEqual([s["token"] for s in out], ["X", "Y"])
        self.assertEqual(out[0]["wallet_count"], 4)

    def test_unknown_market_is_flagged_not_hidden(self):
        out = signals.compute_signals([mk(1, "A", ts=self.now), mk(2, "B", ts=self.now)], {}, self.now, 3600, 2)
        self.assertEqual(len(out), 1)
        self.assertTrue(any("No market data" in f["text"] for f in out[0]["flags"]))


class Misc(unittest.TestCase):
    def test_address_validation(self):
        self.assertEqual(config.normalize_address("ethereum", "0x" + "AB" * 20), "0x" + "ab" * 20)
        for chain, bad in [("ethereum", "0x123"), ("solana", "0xabc"), ("solana", "short"), ("nope", "x"), ("arc", "")]:
            with self.assertRaises(ValueError):
                config.normalize_address(chain, bad)
        self.assertTrue(config.normalize_address("solana", "So11111111111111111111111111111111111111112"))

    def test_dexscreener_parse_picks_deepest_pool_on_right_chain(self):
        data = {"pairs": [
            {"chainId": "solana", "priceUsd": "0.5", "liquidity": {"usd": 10}, "baseToken": {"symbol": "LOW"}},
            {"chainId": "solana", "priceUsd": "0.4", "liquidity": {"usd": 900}, "baseToken": {"symbol": "DEEP", "name": "Deep"},
             "priceChange": {"h24": 12.5}, "url": "https://dexscreener.com/solana/abc"},
            {"chainId": "ethereum", "priceUsd": "9", "liquidity": {"usd": 10**9}, "baseToken": {"symbol": "WRONG"}}]}
        m = market.parse_pairs(data, "solana")
        self.assertEqual((m["symbol"], m["price_usd"], m["change_24h"]), ("DEEP", 0.4, 12.5))
        self.assertIsNone(market.parse_pairs({"pairs": None}, "solana"))
        self.assertIsNone(market.parse_pairs({"pairs": [{"chainId": "solana", "url": "http://evil"}]}, "ethereum"))


if __name__ == "__main__":
    unittest.main()
