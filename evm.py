"""EVM wallet reader (Ethereum, Robinhood Chain, Arc, any EVM chain) via standard eth_* JSON-RPC."""
import time

from rpc import RpcError, rpc_call

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_range = {}         # chain key -> adaptive getLogs block range
_symbols = {}       # (rpc, token) -> (symbol, decimals)
_block_ts = {}      # (rpc, block) -> timestamp
DEFAULT_RANGE = 500
MAX_CHUNKS = 6


def pad_addr(addr):
    return "0x" + "0" * 24 + addr.lower()[2:]


def _group(logs):
    """tx hash -> list of (token, from, to, value, block). Skips NFT transfers (4 topics)."""
    out = {}
    for lg in logs or []:
        topics = lg.get("topics") or []
        if len(topics) != 3:
            continue
        data = lg.get("data") or "0x"
        value = int(data, 16) if len(data) > 2 else 0
        out.setdefault(lg["transactionHash"], []).append(
            (lg["address"].lower(), "0x" + topics[1][-40:], "0x" + topics[2][-40:],
             value, int(lg["blockNumber"], 16)))
    return out


def detect_evm_buys(wallet, incoming_logs, outgoing_logs, tx_lookup, extra_ignore=()):
    """Buy = wallet received a token AND paid in the same tx (sent another token, or sent native value)."""
    w = wallet.lower()
    inc, out = _group(incoming_logs), _group(outgoing_logs)
    ignore = {t.lower() for t in extra_ignore}
    buys = []
    for txh, transfers in inc.items():
        received, block = {}, 0
        for token, _f, _t, val, blk in transfers:
            received[token] = received.get(token, 0) + val
            block = blk
        sent_tokens = {t[0] for t in out.get(txh, [])}
        spent = bool(sent_tokens - set(received))
        if not spent:
            tx = tx_lookup(txh)
            spent = bool(tx) and (tx.get("from") or "").lower() == w and int(tx.get("value") or "0x0", 16) > 0
        if not spent:
            continue
        for token, raw in received.items():
            if token not in ignore:
                buys.append({"token": token, "raw": raw, "tx": txh, "block": block})
    return buys


def _decode_string(hexdata):
    try:
        b = bytes.fromhex(hexdata[2:])
    except (ValueError, TypeError):
        return None
    if len(b) == 32:
        return b.rstrip(b"\x00").decode("utf-8", "ignore") or None
    if len(b) >= 64:
        off = int.from_bytes(b[0:32], "big")
        ln = int.from_bytes(b[off:off + 32], "big")
        return b[off + 32:off + 32 + ln].decode("utf-8", "ignore") or None
    return None


def token_meta(rpc_url, token):
    key = (rpc_url, token)
    if key not in _symbols:
        symbol, decimals = None, 18
        try:
            symbol = _decode_string(rpc_call(rpc_url, "eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]))
            dec = rpc_call(rpc_url, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
            decimals = int(dec, 16) if dec and dec != "0x" else 18
        except (RpcError, ValueError):
            pass
        if len(_symbols) > 2000:
            _symbols.clear()
        _symbols[key] = (symbol, decimals)
    return _symbols[key]


def _block_time(rpc_url, block):
    key = (rpc_url, block)
    if key not in _block_ts:
        blk = rpc_call(rpc_url, "eth_getBlockByNumber", [hex(block), False])
        _block_ts[key] = int(blk["timestamp"], 16) if blk else time.time()
        if len(_block_ts) > 1000:
            _block_ts.clear()
    return _block_ts[key]


def had_token_before(rpc_url, token, wallet, block):
    """True/False if the wallet received this token before `block`; None when the RPC can't tell
    (then the buy is accepted as 'first seen by WalletWatch')."""
    prev = max(0, block - 1)
    try:
        logs = rpc_call(rpc_url, "eth_getLogs", [{"address": token, "fromBlock": "0x0", "toBlock": hex(prev),
                                                  "topics": [TRANSFER_TOPIC, None, pad_addr(wallet)]}])
        return bool(logs)
    except RpcError:
        pass
    try:
        bal = rpc_call(rpc_url, "eth_call", [{"to": token, "data": "0x70a08231" + pad_addr(wallet)[2:]}, hex(prev)])
        return int(bal, 16) > 0
    except (RpcError, ValueError, TypeError):
        return None


def poll_wallet(chain_key, rpc_url, wallet, cursor, lookback_blocks, extra_ignore=(), known=None):
    """Returns (buys, new_cursor). Cursor = last block fully scanned. Each buy has first_ever=True/False."""
    known = known or (lambda token: False)
    seen = set()
    latest = int(rpc_call(rpc_url, "eth_blockNumber", []), 16)
    start = int(cursor) + 1 if cursor is not None else max(0, latest - lookback_blocks)
    buys, new_cursor = [], cursor
    lookup_cache = {}

    def tx_lookup(txh):
        if txh not in lookup_cache:
            lookup_cache[txh] = rpc_call(rpc_url, "eth_getTransactionByHash", [txh])
        return lookup_cache[txh]

    for _ in range(MAX_CHUNKS):
        if start > latest:
            break
        size = _range.get(chain_key, DEFAULT_RANGE)
        end = min(latest, start + size - 1)
        rng = {"fromBlock": hex(start), "toBlock": hex(end)}
        try:
            incoming = rpc_call(rpc_url, "eth_getLogs", [{**rng, "topics": [TRANSFER_TOPIC, None, pad_addr(wallet)]}])
            outgoing = rpc_call(rpc_url, "eth_getLogs", [{**rng, "topics": [TRANSFER_TOPIC, pad_addr(wallet)]}])
        except RpcError as e:
            if any(w in str(e).lower() for w in ("range", "limit", "exceed", "too many", "10000")):
                _range[chain_key] = max(10, size // 2)  # provider wants smaller windows; shrink and retry next poll
            raise
        for b in sorted(detect_evm_buys(wallet, incoming, outgoing, tx_lookup, extra_ignore), key=lambda x: x["block"]):
            symbol, dec = token_meta(rpc_url, b["token"])
            repeat = b["token"] in seen or known(b["token"])   # repeat buys are still reported (All Buys tab)
            seen.add(b["token"])
            before = True if repeat else had_token_before(rpc_url, b["token"], wallet, b["block"])
            buys.append({"token": b["token"], "symbol": symbol, "amount": b["raw"] / (10 ** dec),
                         "ts": _block_time(rpc_url, b["block"]), "tx": b["tx"], "first_ever": before is not True})
        new_cursor = end
        start = end + 1
    return buys, new_cursor
