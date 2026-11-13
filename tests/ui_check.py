"""Real-browser check of the page (needs playwright + chromium). Run: python tests/ui_check.py"""
import json, os, sys, tempfile, threading, time, http.client
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MOCK"] = "1"
import server
from playwright.sync_api import sync_playwright

addrs = ["So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
         "Es9vMFrzaCERmJfrF4H2FYD4KCoNyFpyNwYbaXeS4WbG", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
         "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs", "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"]
app = server.App(os.path.join(tempfile.mkdtemp(), "ui.db"))
for i, a in enumerate(addrs):
    app.db.add_wallet("solana", a, f"Wallet {i+1}")
srv = server.make_server(app, "127.0.0.1", 0)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
app.poller.poll_once(); app.poller.poll_once()
errors = []
with sync_playwright() as p:
    b = p.chromium.launch(); ctx = b.new_context(viewport={"width": 390, "height": 800}, permissions=["clipboard-read", "clipboard-write"], bypass_csp=True); pg = ctx.new_page()
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.goto(f"http://127.0.0.1:{port}/"); pg.wait_for_selector("nav.bar button")
    tabs = pg.eval_on_selector_all("nav.bar button", "e=>e.map(x=>x.textContent.trim())")
    assert tabs == ["Signals", "Graph", "Activity", "All Buys", "Wallets"], tabs
    # Signals: only coin names + Details button
    pg.wait_for_selector(".coinrow", timeout=8000)
    rows = pg.eval_on_selector_all(".coinrow", "e=>e.map(x=>[x.querySelector('.sym').textContent, x.querySelector('button').textContent, x.children.length])")
    assert rows and all(r[1] == "Details" and r[2] == 2 for r in rows), rows
    pg.click(".coinrow button")
    pg.wait_for_selector("dialog#sigdlg[open]")
    body = pg.inner_text("#sigbody")
    assert "of your wallets" in body and "Close" in body, body
    assert "null" not in body and "[object" not in body and "undefined" not in body, body
    name = pg.inner_text("#sigtitle"); assert name == rows[0][0], (name, rows[0])
    full = pg.inner_text("#sigbody .addr code"); assert full and "..." not in full, full   # popup shows the full address, not the shortened one
    pg.click("#sigdlg button.copy"); assert pg.evaluate("navigator.clipboard.readText()") == full
    time.sleep(0.2); pg.screenshot(path="/tmp/ui_popup.png")
    pg.click("dialog#sigdlg .actions button"); assert not pg.is_visible("dialog#sigdlg")
    pg.click(".coinrow button"); pg.keyboard.press("Escape"); assert not pg.is_visible("dialog#sigdlg")
    # Graph: bars only for coins with 2+ wallets
    pg.click("nav.bar button[data-tab=graph]")
    pg.wait_for_selector("#tab-graph svg")
    counts = pg.eval_on_selector_all("#tab-graph svg:first-of-type text", "e=>e.map(x=>x.textContent)")
    nums = [int(x) for x in counts if x.isdigit()]
    assert nums and min(nums) >= 2, counts
    pg.screenshot(path="/tmp/ui_graph.png")
    # Activity (first buys) and All Buys are separate lists
    pg.click("nav.bar button[data-tab=activity]"); pg.wait_for_selector("#tab-activity .row")
    act = pg.eval_on_selector_all("#tab-activity .row", "e=>e.length"); assert "FIRST time" in pg.inner_text("#tab-activity")
    # coin name + address + Copy button in Activity
    assert pg.eval_on_selector_all("#tab-activity .row .addr button.copy", "e=>e.length") == act
    first = json.loads(pg.evaluate("fetch('/api/state').then(r=>r.json()).then(s=>JSON.stringify(s.feed[0]))"))
    row = pg.inner_text("#tab-activity .row >> nth=0")
    if first["symbol"]:
        assert first["symbol"] in row and (first["name"] or first["symbol"]) in row, row   # name shown, not only the address
    else:
        assert "Name not found yet" in row, row                                 # honest label, address + Copy still there
    assert first["token"][:6] in row, row
    named = pg.eval_on_selector_all("#tab-activity .row", "e=>e.filter(x=>!x.innerText.includes('Name not found yet')).length")
    assert named >= 1, "no coin in the demo shows a name"
    pg.click("#tab-activity .row >> nth=0 >> button.copy")
    pg.wait_for_selector("#tab-activity .row >> nth=0 >> button.copy:has-text('Copied')", timeout=4000)
    assert pg.evaluate("navigator.clipboard.readText()") == first["token"]    # the FULL address is copied, not the short one
    pg.click("nav.bar button[data-tab=allbuys]"); pg.wait_for_selector("#tab-allbuys .row")
    assert pg.eval_on_selector_all("#tab-allbuys .row .addr button.copy", "e=>e.length") >= 1
    pg.click("#tab-allbuys .row >> nth=0 >> button.copy")
    assert pg.evaluate("navigator.clipboard.readText()") in {first["token"]} | {x["token"] for x in json.loads(pg.evaluate("fetch('/api/state').then(r=>r.json()).then(s=>JSON.stringify(s.all_buys))"))}
    allb = pg.eval_on_selector_all("#tab-allbuys .row", "e=>e.length"); assert "Repeat buys" in pg.inner_text("#tab-allbuys")
    assert allb >= act, (allb, act)
    assert pg.inner_text("#tab-allbuys .badge") == "NEW COIN"
    pg.screenshot(path="/tmp/ui_allbuys.png")
    # live: a repeat buy shows up in All Buys but not in Activity without reload
    w = app.db.list_wallets()[0]
    tok = app.db.recent_buys(1)[0]
    app.db.add_tx_buy(w["id"], tok["chain"], tok["token"], None, 7, "repeat-tx-1", time.time(), time.time())
    app.hub.publish("tx", {})
    pg.wait_for_function("document.querySelectorAll('#tab-allbuys .row').length > %d" % allb, timeout=8000)
    pg.click("nav.bar button[data-tab=activity]")
    assert pg.eval_on_selector_all("#tab-activity .row", "e=>e.length") == act
    # usage card on the Wallets tab (only shown when a request budget is active)
    app.poller.budget.enabled = True; app.poller.budget.add(1234)
    app.hub.publish("update", {}); pg.click("nav.bar button[data-tab=wallets]"); pg.wait_for_selector("#usage", timeout=15000)
    txt = pg.inner_text("#usage"); assert "of 1,000,000 used" in txt and "Mode: Normal" in txt, txt
    pg.screenshot(path="/tmp/ui_usage.png")
    app.poller.budget.enabled = False
    # plain-http phones have no clipboard API: the fallback must still say "Copied"
    pg2 = ctx.new_page(); pg2.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: undefined})")
    pg2.goto(f"http://127.0.0.1:{port}/"); pg2.click("nav.bar button[data-tab=activity]"); pg2.wait_for_selector("#tab-activity button.copy")
    pg2.click("#tab-activity .row >> nth=0 >> button.copy"); pg2.wait_for_selector("#tab-activity .row >> nth=0 >> button.copy:has-text('Copied')", timeout=4000)
    pg.click("nav.bar button[data-tab=activity]"); pg.wait_for_selector("#tab-activity .row"); pg.screenshot(path="/tmp/ui_activity.png")
    b.close()
assert not errors, errors
print("UI OK  signals:", len(rows), " activity rows:", act, " all-buys rows:", allb)
