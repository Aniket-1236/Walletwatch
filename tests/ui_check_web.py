"""Browser check of website-process mode (what runs on PythonAnywhere). Needs playwright + chromium."""
import os, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MOCK"] = "1"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "shared.db")
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
import server, wsgi_app, worker
from playwright.sync_api import sync_playwright


class Threaded(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *a): pass


wapp = server.App()                                   # the worker's app
for i, a in enumerate(["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                       "Es9vMFrzaCERmJfrF4H2FYD4KCoNyFpyNwYbaXeS4WbG", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
                       "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"]):
    wapp.db.add_wallet("solana", a, f"Wallet {i+1}")
wapp.poller.poll_once(); wapp.poller.poll_once()
last = {}
worker.publish(wapp, last)
stop = threading.Event()
threading.Thread(target=lambda: [(worker.publish(wapp, last), time.sleep(1)) for _ in iter(stop.is_set, True)], daemon=True).start()
srv = make_server("127.0.0.1", 0, wsgi_app.application, server_class=Threaded, handler_class=Quiet)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

errors = []
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_context(viewport={"width": 390, "height": 800}, bypass_csp=True).new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(f"http://127.0.0.1:{port}/")
    pg.wait_for_selector(".coinrow", timeout=10000)                 # signals come from the worker's snapshot
    pg.wait_for_function("document.querySelector('#livetxt').textContent === 'Live'", timeout=8000)
    pg.click("nav.bar button[data-tab=activity]"); pg.wait_for_selector("#tab-activity .row button.copy")
    pg.click("nav.bar button[data-tab=allbuys]"); pg.wait_for_selector("#tab-allbuys .row")
    # add a wallet from the website: it must show up (database shared with the worker)
    pg.click("nav.bar button[data-tab=wallets]"); pg.wait_for_selector("#f-addr, input[placeholder^='Paste']")
    before = pg.eval_on_selector_all("#tab-wallets .row", "e=>e.length")
    pg.fill("input[placeholder^='Paste']", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So")
    pg.fill("input[placeholder^='e.g.']", "Added on web")
    pg.click("text=Add wallet")
    pg.wait_for_function("document.querySelectorAll('#tab-wallets .row').length > %d" % before, timeout=8000)
    # worker stops -> the page says so
    stop.set(); wapp.db.set_setting("snapshot_ts", time.time() - 300)
    pg.wait_for_selector("#workerdown", timeout=12000)
    assert pg.inner_text("#livetxt") == "Worker stopped"
    pg.screenshot(path="/tmp/ui_web_down.png")
    b.close()
assert not errors, errors
print("WEB-MODE UI OK, wallets before/after:", before, before + 1)
