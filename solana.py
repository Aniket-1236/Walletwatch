"""Solana wallet reader using plain JSON-RPC (no API key required, but a private RPC such as Helius is much faster).

Accuracy rules:
* All reads use the "confirmed" commitment (the default "finalized" is ~13 seconds slower).
* A transaction the RPC has not indexed yet is retried, never skipped.
* More than one page of new signatures is fully read, so a busy wallet loses nothing.
* A buy only counts as FIRST EVER if the wallet's token account for that coin has no older history on-chain."""
import time
from collections import OrderedDict

from rpc import RpcError, rpc_call

TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


class SeenSigs:
    """Signatures already handled (bounded), so the same transaction is never downloaded twice."""

    def __init__(self, cap=4000):
        self.cap, self.d = cap, OrderedDict()

    def add(self, sig):
        self.d[sig] = True
        self.d.move_to_end(sig)
        while len(self.d) > self.cap:
            self.d.popitem(last=False)

    def __contains__(self, sig):
        return sig in self.d

    def __len__(self):
        return len(self.d)


def may_be_buy(logs):
    """Cheap pre-filter on the websocket logs: a token buy always runs the SPL Token program.
    Plain SOL transfers, votes etc. never do, so they are skipped without spending a request.
    Unknown / empty / truncated logs are never skipped (accuracy first)."""
    if not logs:
        return True
    text = "\n".join(logs) if not isinstance(logs, str) else logs
    if "Log truncated" in text:
        return True
    return any(p in text for p in TOKEN_PROGRAMS)

WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
IGNORE = {WSOL, USDC}
MIN_SPEND_LAMPORTS = 200_000  # 0.0002 SOL, filters out pure rent / dust movements
PAGE = 100
SEED_PAGE = 50
MAX_PAGES = 5
NULL_RETRIES = 2          # getTransaction can return null for a moment right after confirmation
NULL_RETRY_DELAY = 0.6
MAX_NULL_POLLS = 6        # after this many polls a signature that never resolves is given up on
_null_polls = {}


def _balances(entries, wallet):
    """mint -> (raw amount, decimals, token account index) for tokens OWNED by the wallet."""
    out = {}
    for e in entries or []:
        if e.get("owner") != wallet:
            continue
        ui = e.get("uiTokenAmount") or {}
        raw, dec = int(ui.get("amount", "0")), ui.get("decimals")
        prev = out.get(e["mint"], (0, dec, e.get("accountIndex")))
        out[e["mint"]] = (prev[0] + raw, dec if dec is not None else prev[1],
                          prev[2] if prev[2] is not None else e.get("accountIndex"))
    return out


def parse_solana_tx(tx, wallet, extra_ignore=()):
    """Return the tokens this wallet ACQUIRED in a transaction by paying with SOL or another token.

    A 'buy' = token balance of the wallet went up AND the wallet spent something in the same
    transaction (SOL or another token). Pure transfers in / airdrops therefore don't count.
    Each result also carries pre_raw (balance before) and account (token account address)."""
    if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
        return []
    meta = tx["meta"]
    pre = _balances(meta.get("preTokenBalances"), wallet)
    post = _balances(meta.get("postTokenBalances"), wallet)
    deltas = {}
    for mint in set(pre) | set(post):
        r0, d0, _ = pre.get(mint, (0, None, None))
        r1, d1, i1 = post.get(mint, (0, None, None))
        deltas[mint] = (r1 - r0, d1 if d1 is not None else d0, r0, i1)

    keys = tx["transaction"]["message"]["accountKeys"]
    pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    native_spent = 0
    if wallet in pubkeys:
        i = pubkeys.index(wallet)
        delta = meta["postBalances"][i] - meta["preBalances"][i]
        native_spent = -delta - (meta.get("fee", 0) if i == 0 else 0)
    spent = native_spent > MIN_SPEND_LAMPORTS or any(v[0] < 0 for v in deltas.values())
    if not spent:
        return []

    sigs = tx["transaction"].get("signatures") or [""]
    ignore = IGNORE | set(extra_ignore)
    ts = tx.get("blockTime") or time.time()
    out = []
    for mint, (d, dec, r0, idx) in deltas.items():
        if d > 0 and mint not in ignore:
            acct = pubkeys[idx] if idx is not None and 0 <= idx < len(pubkeys) else None
            out.append({"token": mint, "symbol": None, "amount": d / (10 ** dec) if dec is not None else float(d),
                        "ts": ts, "tx": sigs[0], "pre_raw": r0, "account": acct})
    return out


def is_first_ever(rpc_url, account, sig):
    """True when the wallet's token account has no transaction older than `sig` (so this is its first ever buy)."""
    if not account:
        return True
    older = rpc_call(rpc_url, "getSignaturesForAddress",
                     [account, {"before": sig, "limit": 1, "commitment": "confirmed"}]) or []
    return len(older) == 0


def _new_signatures(rpc_url, wallet, cursor):
    out, before = [], None
    for _ in range(MAX_PAGES):
        limit = PAGE if cursor else SEED_PAGE
        opts = {"limit": limit, "commitment": "confirmed"}
        if cursor:
            opts["until"] = cursor
        if before:
            opts["before"] = before
        got = rpc_call(rpc_url, "getSignaturesForAddress", [wallet, opts]) or []
        out += got
        if len(got) < limit or not cursor:
            break
        before = got[-1]["signature"]
    return out


def _get_tx(rpc_url, sig):
    for attempt in range(NULL_RETRIES):
        tx = rpc_call(rpc_url, "getTransaction", [sig, {
            "encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}])
        if tx is not None:
            return tx
        if attempt < NULL_RETRIES - 1:
            time.sleep(NULL_RETRY_DELAY)
    return None


def poll_wallet(rpc_url, wallet, cursor, seed_seconds, extra_ignore=(), now=None, known=None, seen_sigs=None,
                verify_first=True):
    """Returns (buys, new_cursor). Cursor = newest signature already handled.

    `known(mint)` says whether this wallet's buy of that coin is already recorded. Every buy is returned;
    repeats get first_ever=False without any extra RPC call."""
    now = now or time.time()
    known = known or (lambda mint: False)
    sigs = _new_signatures(rpc_url, wallet, cursor)
    if not sigs:
        return [], cursor
    handled, buys, seen = cursor, [], set()
    seen_sigs = seen_sigs if seen_sigs is not None else SeenSigs()
    for s in reversed(sigs):  # oldest first so the cursor only advances past finished work
        sig = s["signature"]
        if sig in seen_sigs:       # already handled through the live websocket: costs nothing
            handled = sig
            continue
        if s.get("err") is None:
            recent_enough = cursor or not s.get("blockTime") or now - s["blockTime"] <= seed_seconds
            if recent_enough:
                try:
                    tx = _get_tx(rpc_url, sig)
                    if tx is None:
                        n = _null_polls.get(sig, 0) + 1
                        if n < MAX_NULL_POLLS:
                            _null_polls[sig] = n
                            break  # not indexed yet: retry from this signature on the next check
                        _null_polls.pop(sig, None)
                    else:
                        _null_polls.pop(sig, None)
                        for b in parse_solana_tx(tx, wallet, extra_ignore):
                            repeat = b["token"] in seen or known(b["token"])  # repeats still go to All Buys
                            seen.add(b["token"])
                            b["first_ever"] = (not repeat and b["pre_raw"] == 0
                                               and (not verify_first or is_first_ever(rpc_url, b["account"], sig)))
                            buys.append(b)
                except RpcError:
                    break  # retry from here next poll
            seen_sigs.add(sig)
        handled = sig
    if len(_null_polls) > 5000:
        _null_polls.clear()
    return buys, handled


def process_hints(rpc_url, wallet, hints, extra_ignore=(), known=None, seen_sigs=None, verify_first=True):
    """Handle transactions the websocket just announced: ONE getTransaction each (no signature listing).

    hints = [(signature, logs)]. Returns (buys, retry_sigs). retry_sigs are signatures the RPC could not
    return yet; the caller schedules a normal listing check shortly so nothing is lost."""
    known = known or (lambda mint: False)
    seen_sigs = seen_sigs if seen_sigs is not None else SeenSigs()
    buys, retry, seen = [], [], set()
    for sig, logs in hints:
        if sig in seen_sigs:
            continue
        if not may_be_buy(logs):
            seen_sigs.add(sig)
            continue
        try:
            tx = _get_tx(rpc_url, sig)
            if tx is None:
                retry.append(sig)
                continue
            for b in parse_solana_tx(tx, wallet, extra_ignore):
                repeat = b["token"] in seen or known(b["token"])
                seen.add(b["token"])
                b["first_ever"] = (not repeat and b["pre_raw"] == 0
                                   and (not verify_first or is_first_ever(rpc_url, b["account"], sig)))
                buys.append(b)
            seen_sigs.add(sig)
        except RpcError:
            retry.append(sig)
    return buys, retry
