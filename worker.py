"""Background worker: does ALL the tracking (websocket, wallet checks, alerts) with no web server.

Local PC:   not needed, `python server.py` already does everything.
PythonAnywhere: run this as an Always-on task, and let the website (wsgi_app.py) just show the results.
The two share the database file; the worker publishes a fresh snapshot of the dashboard every few seconds."""
import json
import threading
import time

import config
import live
import rpc
from server import App


def publish(app, last):
    """Save the dashboard data for the website process. Heartbeat every call, full data when it changed."""
    st = app.state()
    st.pop("now", None)
    blob = json.dumps(st, sort_keys=True)
    if blob != last.get("blob") or time.time() - last.get("t", 0) > 20:
        app.db.set_setting("snapshot", blob)
        last["blob"], last["t"] = blob, time.time()
    app.db.set_setting("snapshot_ts", time.time())


def process_kicks(app, seen):
    """Requests from the website: 'check now' (poll_kick) and 'recalculate' (kick)."""
    for key, action in (("poll_kick", app.poller.trigger), ("kick", app.poller.evaluate)):
        value = app.db.get_setting(key)
        if key not in seen:           # first look: remember where things stand, don't act on old requests
            seen[key] = value
        elif value != seen[key]:
            seen[key] = value
            threading.Thread(target=action, daemon=True).start()


def main():
    app = App()
    p = app.poller
    threading.Thread(target=p.loop, daemon=True, name="poller").start()
    if not config.mock():
        host = config.chain_cfg("solana")["rpc"].split("//")[-1].split("/")[0].split("?")[0]
        print(f"Worker started. Solana RPC: {host}", flush=True)
        try:
            print("Connection test OK: Solana slot", rpc.rpc_call(config.chain_cfg("solana")["rpc"], "getSlot", []), flush=True)
        except Exception as e:  # noqa: BLE001
            print("CONNECTION PROBLEM:", e, flush=True)
        if config.use_ws():
            p.watcher = live.SolanaWatcher(p)
            p.watcher.start()
    last, seen = {}, {}
    while True:
        try:
            process_kicks(app, seen)
            publish(app, last)
        except Exception as e:  # noqa: BLE001 - keep the worker alive whatever happens
            print("worker error:", e, flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
