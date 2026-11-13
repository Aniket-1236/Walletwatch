"""Token market data from DexScreener's free public API (no key). Used to rate and label signals."""
import time

import config
from rpc import RpcError, http_json

_cache = {}
TTL_OK, TTL_MISS = 120, 30


def _key(chain, token):
    return (chain, token if config.CHAIN_DEFS[chain]["type"] == "solana" else token.lower())


def parse_pairs(data, chain):
    pairs = [p for p in (data or {}).get("pairs") or [] if p.get("chainId") == chain]
    if not pairs:
        return None
    best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    url = best.get("url") or ""
    return {
        "symbol": (best.get("baseToken") or {}).get("symbol"),
        "name": (best.get("baseToken") or {}).get("name"),
        "price_usd": float(best["priceUsd"]) if best.get("priceUsd") else None,
        "liquidity_usd": (best.get("liquidity") or {}).get("usd"),
        "volume_24h": (best.get("volume") or {}).get("h24"),
        "change_1h": (best.get("priceChange") or {}).get("h1"),
        "change_24h": (best.get("priceChange") or {}).get("h24"),
        "pair_created_ms": best.get("pairCreatedAt"),
        "url": url if url.startswith("https://dexscreener.com/") else None,
    }


def peek(chain, token):
    """Cached market info only. Never touches the network, so it is safe on the alert path."""
    if config.mock():
        import mock
        return mock.MARKET.get(token)
    hit = _cache.get(_key(chain, token))
    return hit[1] if hit else None


def needs_refresh(chain, token):
    if config.mock():
        return False
    hit = _cache.get(_key(chain, token))
    return hit is None or time.time() >= hit[0]


def get_market(chain, token, timeout=10, retries=1):
    if config.mock():
        import mock
        return mock.MARKET.get(token)
    key = _key(chain, token)
    hit = _cache.get(key)
    if hit and time.time() < hit[0]:
        return hit[1]
    try:
        data = http_json(f"https://api.dexscreener.com/latest/dex/tokens/{token}", timeout=timeout, retries=retries)
        info, ttl = parse_pairs(data, chain), TTL_OK
    except (RpcError, ValueError, KeyError, TypeError):
        info, ttl = (hit[1] if hit else None), TTL_MISS
    if len(_cache) > 3000:
        _cache.clear()
    _cache[key] = (time.time() + ttl, info)
    return info
