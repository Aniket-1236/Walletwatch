import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse


class RpcError(Exception):
    pass


class RateLimited(RpcError):
    pass


class _Limiter:
    """Spaces requests to one host so many wallet checks can't trigger HTTP 429, and slows ALL
    threads down together when a host does answer 429."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next = {}

    @staticmethod
    def rate_for(host):
        env = os.getenv("RPC_MAX_RPS", "").strip()
        if env:
            try:
                return max(0.5, float(env))
            except ValueError:
                pass
        if host in ("127.0.0.1", "localhost", "::1"):
            return 0                                   # local servers (tests): unlimited
        if host.endswith("helius-rpc.com"):
            try:                                       # Helius free plan = 10 requests/second; stay just under it
                return max(1.0, float(os.getenv("HELIUS_RPS", "9")))
            except ValueError:
                return 9.0
        if host.endswith("solana.com"):
            return 2.5                                 # free public Solana RPC is very strict
        return 20.0                                    # private RPC (Helius, QuickNode, Alchemy...)

    def wait(self, host):
        rate = self.rate_for(host)
        if not rate:
            return
        with self.lock:
            now = time.time()
            slot = max(now, self.next.get(host, 0))
            self.next[host] = slot + 1.0 / rate
        if slot > now:
            time.sleep(slot - now)

    def cooldown(self, host, seconds):
        with self.lock:
            self.next[host] = max(self.next.get(host, 0), time.time() + seconds)


LIMITER = _Limiter()
_budget = None


def set_budget(b):
    """Install the monthly request budget (see budget.py). Every request to a budgeted host is counted."""
    global _budget
    _budget = b


def http_json(url, payload=None, headers=None, timeout=15, retries=4):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "walletwatch/1.0"}
    hdrs.update(headers or {})
    host = urlparse(url).hostname or ""
    last = None
    counted = _budget is not None and _budget.counts(host)
    for attempt in range(retries + 1):
        if counted:
            _budget.check()            # raises BudgetExceeded once the monthly reserve is reached
        LIMITER.wait(host)
        if counted:
            _budget.add(1)
        req = urllib.request.Request(url, data=data, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = min(15.0, float(e.headers.get("Retry-After", "")))
                except (TypeError, ValueError):
                    wait = min(10.0, 1.0 * (2 ** attempt)) + random.random()
                LIMITER.cooldown(host, wait)           # every thread pauses, not just this one
                last = RateLimited(f"Rate limited by {host} (HTTP 429). It will retry by itself. "
                                   "For fast, reliable alerts set SOLANA_RPC_URL to a free Helius or QuickNode key.")
                if attempt < retries:
                    continue
                raise last from e
            last = RpcError(f"HTTP {e.code} from {url.split('?')[0]}")
            if e.code in (502, 503, 504) and attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise last from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = RpcError(f"Network error: {e}")
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise last from e
        except ValueError as e:
            raise RpcError("Invalid JSON in response") from e
    raise last or RpcError("Request failed")


def rpc_call(url, method, params):
    res = http_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if isinstance(res, dict) and res.get("error"):
        err = res["error"]
        msg = err.get("message", err) if isinstance(err, dict) else err
        if isinstance(err, dict) and err.get("code") == 429:
            raise RateLimited(f"{method}: rate limited")
        raise RpcError(f"{method}: {msg}")
    return res.get("result") if isinstance(res, dict) else None
