"""Phone alerts that work even when the website is closed: ntfy.sh app and/or a Telegram bot."""
import os
import threading

from rpc import RpcError, http_json
import urllib.request


def configured():
    return {"ntfy": bool(os.getenv("NTFY_TOPIC")),
            "telegram": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))}


def _ascii(s):
    return s.encode("ascii", "ignore").decode() or "Wallet alert"


def send(title, body):
    errors = []
    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            req = urllib.request.Request(f"{server}/{topic}", data=body.encode("utf-8"),
                                         headers={"Title": _ascii(title), "Tags": "chart_with_upwards_trend"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001 - notification failure must never crash the poller
            errors.append(f"ntfy: {e}")
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            http_json(f"https://api.telegram.org/bot{token}/sendMessage",
                      {"chat_id": chat, "text": f"{title}\n{body}"}, timeout=10, retries=1)
        except RpcError as e:
            errors.append(f"telegram: {e}")
    for e in errors:
        print("notify error:", e, flush=True)
    return errors


def send_async(title, body):
    threading.Thread(target=send, args=(title, body), daemon=True).start()
