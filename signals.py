"""Turns raw buys from your wallets into ranked 'convergence' signals.

Score (0-100) = wallets in agreement 45 + recency 15 + liquidity 25 + not-already-pumped 15.
It measures how strongly YOUR wallets agree and how tradable/risky the token looks. It is not a price prediction."""
import math

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDS", "PYUSD", "FDUSD", "TUSD", "USDE", "USD1", "EURC",
                  "WETH", "ETH", "WSOL", "SOL", "WBTC", "CBBTC", "STETH", "WSTETH"}


def liquidity_score(liq):
    if not liq or liq <= 0:
        return 0.0
    return min(1.0, max(0.0, math.log10(liq / 5000) / math.log10(40)))  # $5k -> 0, $200k -> 1


def momentum_score(change_24h):
    if change_24h is None:
        return 0.5
    if change_24h > 300:
        return 0.0
    if change_24h > 100:
        return 0.4
    if change_24h < -50:
        return 0.3
    return 1.0


def rating(score):
    return "Strong" if score >= 70 else "Moderate" if score >= 50 else "Weak"


def compute_signals(buys, market, now, window_s, min_wallets=2, ignore=()):
    ignore = {i.lower() for i in ignore}
    groups = {}
    for b in buys:
        if now - b["ts"] <= window_s:
            groups.setdefault((b["chain"], b["token"]), []).append(b)

    out = []
    for (chain, token), rows in groups.items():
        m = market.get((chain, token)) or {}
        symbol = m.get("symbol") or next((r.get("symbol") for r in rows if r.get("symbol")), None)
        if token.lower() in ignore or (symbol or "").upper() in STABLE_SYMBOLS:
            continue
        latest = {}
        for r in rows:
            if r["wallet_id"] not in latest or r["ts"] > latest[r["wallet_id"]]["ts"]:
                latest[r["wallet_id"]] = r
        n = len(latest)
        if n < min_wallets:
            continue

        fresh = sum(max(0.0, 1 - (now - r["ts"]) / window_s) for r in latest.values()) / n
        liq, ch24 = m.get("liquidity_usd"), m.get("change_24h")
        score = round(45 * min(n, 5) / 5 + 15 * fresh + 25 * liquidity_score(liq) + 15 * momentum_score(ch24))

        flags = []
        if not m:
            flags.append({"level": "warn", "text": "No market data found. It may be brand new or not listed on a DEX."})
        elif liq is not None and liq < 25_000:
            flags.append({"level": "warn", "text": f"Thin liquidity (${liq:,.0f}). Hard to exit without moving the price."})
        if m.get("pair_created_ms") and now * 1000 - m["pair_created_ms"] < 86_400_000:
            flags.append({"level": "warn", "text": "Trading pair is less than 24 hours old."})
        if ch24 is not None and ch24 > 300:
            flags.append({"level": "warn", "text": f"Already up {ch24:,.0f}% in 24h. You'd be buying after the move."})
        if n == min_wallets and n == 2:
            flags.append({"level": "info", "text": "Only 2 wallets agree so far. This may be coincidence."})

        out.append({
            "chain": chain, "token": token, "symbol": symbol, "name": m.get("name"),
            "wallet_count": n, "score": score, "rating": rating(score),
            "wallets": sorted(({"id": r["wallet_id"], "label": r["wallet_label"], "ts": r["ts"], "amount": r.get("amount")}
                               for r in latest.values()), key=lambda w: w["ts"]),
            "first_ts": min(r["ts"] for r in rows), "last_ts": max(r["ts"] for r in rows),
            "price_usd": m.get("price_usd"), "liquidity_usd": liq, "change_24h": ch24,
            "change_1h": m.get("change_1h"), "url": m.get("url"), "flags": flags,
        })
    out.sort(key=lambda s: (-s["score"], -s["wallet_count"], -s["last_ts"]))
    return out
