"""Demo mode (MOCK=1): fake wallets activity so you can try the whole app with no network or keys."""
import random
import time
import uuid

TOKENS = ["DEMO1", "DEMO2", "DEMO3", "DEMO4", "DEMO5", "DEMO6", "DEMO7", "DEMO8", "DEMO9", "DEMO10"]
WEIGHTS = [5, 4, 3, 2, 1, 1, 2, 2, 1, 1]

MARKET = {
    "DEMO1": dict(symbol="DEMOAI", name="Demo AI Coin", price_usd=0.0421, liquidity_usd=380_000, volume_24h=1_200_000,
                  change_1h=1.2, change_24h=18.4, pair_created_ms=None, url=None),
    "DEMO2": dict(symbol="DEMOCAT", name="Demo Cat", price_usd=0.00031, liquidity_usd=64_000, volume_24h=210_000,
                  change_1h=-2.0, change_24h=62.0, pair_created_ms=None, url=None),
    "DEMO3": dict(symbol="DEMOPUMP", name="Demo Pump", price_usd=0.0000082, liquidity_usd=11_000, volume_24h=90_000,
                  change_1h=40.0, change_24h=520.0, pair_created_ms=int(time.time() * 1000) - 3_600_000, url=None),
    "DEMO4": dict(symbol="DEMOSWAP", name="Demo Swap", price_usd=1.83, liquidity_usd=2_400_000, volume_24h=5_000_000,
                  change_1h=0.3, change_24h=-4.1, pair_created_ms=None, url=None),
    "DEMO5": None,
    "DEMO6": dict(symbol="DEMOFI", name="Demo Finance", price_usd=0.27, liquidity_usd=150_000, volume_24h=400_000,
                  change_1h=0.0, change_24h=7.7, pair_created_ms=None, url=None),
}


def fetch(wallet, cursor, now=None):
    now = now or time.time()
    buys = []
    n = 2 if cursor is None else (1 if random.random() < 0.5 else 0)
    for _ in range(n):
        token = random.choices(TOKENS, WEIGHTS)[0]
        buys.append({"token": token, "symbol": None, "amount": round(random.uniform(1_000, 900_000), 2),
                     "ts": now - random.uniform(0, 600 if cursor is None else 5), "tx": "demo-" + uuid.uuid4().hex[:12]})
    return buys, str(int(now))
