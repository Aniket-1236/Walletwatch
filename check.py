"""Checks your Helius setup in about 10 seconds and tells you exactly what is wrong.

    python check.py                 test everything
    python check.py YOUR_API_KEY    save the key into .env (correct file name, no Notepad problems), then test
"""
import json
import os
import sys
import time

import config
from rpc import RateLimited, RpcError, rpc_call
from ws import WSClient, WSError

HERE = os.path.dirname(os.path.abspath(__file__))


def mask(key):
    return (key[:4] + "..." + key[-2:] + f" ({len(key)} characters)") if len(key) > 8 else "(too short)"


def save_key(key, folder=HERE):
    path = os.path.join(folder, ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = [l.rstrip("\n") for l in f if not l.strip().startswith("HELIUS_API_KEY")]
    lines.insert(0, "HELIUS_API_KEY=" + key)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def explain(err):
    t = str(err)
    if "401" in t or "403" in t:
        return "Helius refused the key (HTTP 401/403). The key is wrong, has a typo/extra space, or was deleted. Copy it again from the Helius dashboard."
    if isinstance(err, RateLimited) or "429" in t:
        return "Helius says too many requests (429). Wait a minute and run again; if it keeps happening your monthly credits may be used up."
    if "Network error" in t or "timed out" in t or "connect failed" in t:
        return "Cannot reach the internet / Helius from this PC. Check Wi-Fi, VPN, firewall or antivirus (allow python.exe)."
    if "budget" in t.lower():
        return t
    return t


def run(argv=None, out=print, folder=HERE):
    argv = argv if argv is not None else sys.argv[1:]
    problems = []
    if argv:
        key = argv[0].strip().strip('"').strip("'")
        path = save_key(key, folder)
        os.environ["HELIUS_API_KEY"] = key
        out(f"Saved your key in {path}")
    else:
        files = config.load_dotenv()
        if files:
            out("Settings file found: " + ", ".join(files))
        else:
            out("No .env file found in " + folder)
            if os.path.exists(os.path.join(folder, ".env.txt")):
                out("  (found .env.txt: it is read automatically, but rename it to .env)")

    key, explicit = config.helius_key(), os.getenv("SOLANA_RPC_URL", "").strip()
    cfg = config.chain_cfg("solana")
    host = cfg["rpc"].split("//")[-1].split("/")[0].split("?")[0]
    if explicit:
        out(f"SOLANA_RPC_URL is set in this window, so it is used instead of the key: {host}")
    elif key:
        out(f"Helius key found: {mask(key)}")
    else:
        out("NO Helius key found. Run:  python check.py YOUR_API_KEY")
        problems.append("no key")
    out(f"RPC server in use: {host}")

    # 1. normal request (same as your JavaScript getSlot test)
    t = time.time()
    try:
        slot = rpc_call(cfg["rpc"], "getSlot", [])
        out(f"[OK] getSlot works: slot {slot} ({int((time.time() - t) * 1000)} ms)")
    except RpcError as e:
        out("[FAIL] getSlot: " + explain(e))
        problems.append("rpc")
        slot = None

    # 2. real wallet read
    wallet = None
    try:
        import db as dbmod
        wallets = [w for w in dbmod.DB(config.db_path()).list_wallets() if w["chain"] == "solana"]
        wallet = wallets[0]["address"] if wallets else None
        if slot is not None and wallet:
            sigs = rpc_call(cfg["rpc"], "getSignaturesForAddress", [wallet, {"limit": 3, "commitment": "confirmed"}])
            out(f"[OK] reading your wallet '{wallets[0]['label']}' works ({len(sigs or [])} recent transactions seen)")
    except RpcError as e:
        out("[FAIL] reading a wallet: " + explain(e))
        problems.append("wallet")
    except Exception as e:  # noqa: BLE001
        out(f"(skipped wallet read: {e})")

    # 3. instant alerts (websocket)
    ws_url = config.solana_ws_url()
    if not config.use_ws():
        out("[SKIP] websocket is turned off (USE_WS=0)")
    elif not ws_url:
        out("[FAIL] no websocket address")
        problems.append("ws")
    else:
        c = WSClient(ws_url, timeout=8)
        try:
            c.connect()
            c.send_text(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                    "params": [{"mentions": [wallet or "11111111111111111111111111111111"]},
                                               {"commitment": "confirmed"}]}))
            end, ok = time.time() + 8, False
            while time.time() < end and not ok:
                raw = c.recv(timeout=2)
                if raw and isinstance(json.loads(raw).get("result"), int):
                    ok = True
            if ok:
                out("[OK] websocket works: instant alerts will be ON")
            else:
                out("[FAIL] websocket connected but did not confirm the subscription. Alerts will still work, but slowly.")
                problems.append("ws")
        except (WSError, ValueError) as e:
            out(f"[FAIL] websocket: {explain(e)} Alerts will still work, but slowly (every few minutes).")
            problems.append("ws")
        finally:
            c.close()

    out("")
    out("EVERYTHING OK. Start the app:  python server.py" if not problems else
        "Fix the [FAIL] lines above, then run  python check.py  again. If it still fails, send me this whole output.")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(run())
