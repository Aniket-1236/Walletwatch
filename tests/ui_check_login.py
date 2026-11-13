"""Real-browser check of the login (needs playwright + chromium)."""
import json, os, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MOCK"] = "1"
import auth
auth.FAIL_DELAY = 0
PW = "my very own long password"
os.environ["APP_PASSWORD_HASH"] = auth.hash_password(PW, iterations=1000)
import server
from playwright.sync_api import sync_playwright

app = server.App(os.path.join(tempfile.mkdtemp(), "l.db"))
app.db.add_wallet("solana", "So11111111111111111111111111111111111111112", "W1")
app.poller.poll_once()
srv = server.make_server(app, "127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{port}/"
errors, urls = [], set()

with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 390, "height": 800}); pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text and "429" not in m.text else None)
    pg.on("response", lambda r: urls.add(r.url) if r.url.startswith(URL) else None)

    pg.goto(URL); pg.wait_for_selector("#pw")
    assert pg.query_selector("nav.bar") is None and "WalletWatch" in pg.title()          # login form only, no panel
    assert pg.get_attribute("#pw", "type") == "password"
    pg.screenshot(path="/tmp/ui_login.png")

    pg.fill("#pw", "wrong password here"); pg.click("#go")
    pg.wait_for_selector("#msg:has-text('Wrong')")
    assert pg.input_value("#pw") == ""                                                    # field is cleared after a failure

    pg.fill("#pw", PW); pg.click("#go")
    pg.wait_for_selector("nav.bar button", timeout=10000)                                 # whole panel appears
    pg.wait_for_selector("#tab-signals h2", timeout=10000)                                 # the Signals tab rendered
    cookies = ctx.cookies()
    ck = [c for c in cookies if c["name"] == "ww_session"][0]
    assert ck["httpOnly"] and ck["sameSite"] == "Strict" and len(ck["value"]) >= 40, ck
    assert "ww_session" not in pg.evaluate("document.cookie")                               # JavaScript cannot read the session
    # the password is nowhere in the page, scripts, storage or any response
    dom = pg.content()
    storage = pg.evaluate("JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage)])")
    bodies = [pg.request.get(u.split("?")[0]).text() for u in sorted(urls) if "/api/stream" not in u]   # every file the page loaded
    assert len(bodies) >= 5
    assert PW not in dom and PW not in storage and not any(PW in x for x in bodies), "password leaked"
    assert os.environ["APP_PASSWORD_HASH"].split("$")[3] not in "".join(bodies)

    pg.reload(); pg.wait_for_selector("nav.bar button")                                    # still logged in after reload
    pg.wait_for_selector("#logout"); pg.screenshot(path="/tmp/ui_logged_in.png")
    pg.click("#logout"); pg.wait_for_selector("#pw")                                       # logout shows the login form again
    assert pg.query_selector("nav.bar") is None
    r = pg.request.get(URL + "api/state"); assert r.status == 401
    pg.goto(URL + "app.js"); assert "renderSignals" not in pg.content()                    # panel code is not served after logout
    pg.goto(URL); pg.wait_for_selector("#pw")

    # lockout after 5 wrong tries
    for _ in range(5):
        pg.fill("#pw", "nope nope nope"); pg.click("#go"); pg.wait_for_selector("#go:not([disabled])")
    pg.fill("#pw", PW); pg.click("#go")
    pg.wait_for_selector("#msg:has-text('Too many')")
    pg.screenshot(path="/tmp/ui_locked.png")

    # authenticator step
    app.db.clear_attempts()
    secret = auth.new_totp_secret(); os.environ["TOTP_SECRET"] = secret
    pg.goto(URL); pg.wait_for_selector("#codebox:not([hidden])")
    pg.fill("#pw", PW); pg.fill("#code", "000000"); pg.click("#go")
    pg.wait_for_selector("#msg:has-text('Wrong')")
    pg.fill("#pw", PW); pg.fill("#code", auth.totp_code(secret, int(time.time() // 30))); pg.click("#go")
    pg.wait_for_selector("nav.bar button", timeout=10000)
    b.close()
assert not errors, errors
print("LOGIN UI OK")
