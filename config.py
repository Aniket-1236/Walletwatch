"""All settings come from environment variables so hosting is just 'set env vars, run server.py'."""
import os
import re


def _read_env_file(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
        return True
    except OSError:
        return False


def env_candidates():
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    for d in (here, os.getcwd()):
        for name in (".env", ".env.txt"):          # Notepad on Windows often saves ".env" as ".env.txt"
            p = os.path.join(d, name)
            if p not in out:
                out.append(p)
    return out


def load_dotenv(path=None):
    """Read KEY=VALUE lines from .env (existing environment variables win). Returns the files that were read."""
    return [p for p in ([path] if path else env_candidates()) if _read_env_file(p)]


load_dotenv()

CHAIN_DEFS = {
    "solana": dict(label="Solana", type="solana", env="SOLANA_RPC_URL",
                   default="https://api.mainnet-beta.solana.com",
                   tx_url="https://solscan.io/tx/", token_url="https://solscan.io/token/"),
    "ethereum": dict(label="Ethereum", type="evm", env="ETHEREUM_RPC_URL",
                     default="https://ethereum-rpc.publicnode.com",
                     tx_url="https://etherscan.io/tx/", token_url="https://etherscan.io/token/"),
    "robinhood": dict(label="Robinhood Chain", type="evm", env="ROBINHOOD_RPC_URL",
                      default="", tx_url="", token_url=""),
    "arc": dict(label="Arc", type="evm", env="ARC_RPC_URL",
                default="", tx_url="", token_url=""),
}

# Tokens that are "money", not "a coin you bought" (wrapped native + big stablecoins).
DEFAULT_IGNORE = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC (Solana)
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",   # WETH
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",   # USDC
    "0xdac17f958d2ee523a2206206994597c13d831ec7",   # USDT
    "0x6b175474e89094c44da98b954eedeac495271d0f",   # DAI
}


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def mock():
    return os.getenv("MOCK", "") == "1"


def helius_key():
    return os.getenv("HELIUS_API_KEY", "").strip()


def chain_cfg(key):
    d = CHAIN_DEFS[key]
    rpc = os.getenv(d["env"], "").strip()
    if not rpc and key == "solana" and helius_key():
        rpc = "https://mainnet.helius-rpc.com/?api-key=" + helius_key()   # just set HELIUS_API_KEY
    return {**d, "key": key, "rpc": rpc or d["default"]}


def poll_seconds():
    """How often each wallet is checked when no live push is available (default 5s, minimum 2s)."""
    return max(2, env_int("POLL_SECONDS", 5))


def safety_poll_seconds():
    """Backup check interval for Solana wallets that have a live websocket subscription."""
    return max(5, env_int("SAFETY_POLL_SECONDS", 30))


def workers():
    """Wallets checked in parallel."""
    return min(64, max(1, env_int("WORKERS", 16)))


def use_ws():
    return os.getenv("USE_WS", "1") != "0"


def solana_ws_url():
    explicit = os.getenv("SOLANA_WS_URL", "").strip()
    if explicit:
        return explicit
    if helius_key() and not os.getenv("SOLANA_RPC_URL", "").strip():
        return "wss://mainnet.helius-rpc.com/?api-key=" + helius_key()
    rpc = chain_cfg("solana")["rpc"]
    for a, b in (("https://", "wss://"), ("http://", "ws://")):
        if rpc.startswith(a):
            return b + rpc[len(a):]
    return ""


def notify_first_buys():
    return os.getenv("NOTIFY_FIRST_BUYS", "1") != "0"


def max_wallets():
    return max(1, env_int("MAX_WALLETS", 500))


def seed_hours():
    return max(1, env_int("SEED_HOURS", 6))


def notify_max_age():
    return env_int("NOTIFY_MAX_AGE", 900)


def db_path():
    """Default: walletwatch.db inside the project folder (an absolute path, so the website and the
    background worker on PythonAnywhere always open the SAME file whatever folder they start from)."""
    return os.getenv("DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "walletwatch.db")


def ignore_tokens():
    extra = {t.strip() for t in os.getenv("IGNORE_TOKENS", "").split(",") if t.strip()}
    return {t.lower() for t in DEFAULT_IGNORE | extra}


_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_address(chain, address):
    address = (address or "").strip()
    if chain not in CHAIN_DEFS:
        raise ValueError("Unknown chain")
    if CHAIN_DEFS[chain]["type"] == "solana":
        if not _B58.match(address):
            raise ValueError("That doesn't look like a Solana address (32-44 base58 characters).")
        return address
    if not _EVM.match(address):
        raise ValueError("That doesn't look like an EVM address (0x followed by 40 hex characters).")
    return address.lower()


def budget_enabled():
    return os.getenv("BUDGET", "1") != "0"


def budget_limit():
    return max(1000, env_int("HELIUS_MONTHLY_LIMIT", 1_000_000))


def budget_reserve():
    try:
        return min(0.5, max(0.0, float(os.getenv("BUDGET_RESERVE", "0.08"))))
    except ValueError:
        return 0.08


def budget_hosts():
    raw = os.getenv("BUDGET_HOSTS", "").strip()
    return tuple(h.strip() for h in raw.split(",") if h.strip()) or None


def cycle_day():
    return env_int("HELIUS_CYCLE_DAY", 1)


def reconcile_seconds():
    """With the live websocket, how often each Solana wallet is double-checked for anything it missed."""
    return max(60, env_int("RECONCILE_SECONDS", 900))
