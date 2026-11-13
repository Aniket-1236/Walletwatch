"use strict";
const $ = (s) => document.querySelector(s);
let S = null, tab = "signals", formDraft = { chain: "solana", address: "", label: "" }, armed = null;

// Safe DOM builder: text is always inserted as text, never as HTML (token names come from the chain).
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) { const e = document.createElementNS(svgNS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); return e; }

const short = (a) => a.length > 12 ? a.slice(0, 4) + "..." + a.slice(-4) : a;
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + " min ago";
  if (s < 86400) return Math.floor(s / 3600) + " h ago";
  return Math.floor(s / 86400) + " d ago";
}
function usd(n) {
  if (n == null) return "n/a";
  if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return "$" + (n / 1e3).toFixed(n >= 1e5 ? 0 : 1) + "k";
  if (n >= 1) return "$" + n.toFixed(2);
  return "$" + n.toPrecision(2);
}
const pct = (n) => n == null ? h("span", {}, "n/a") : h("span", { class: n >= 0 ? "pos" : "neg" }, (n >= 0 ? "+" : "") + n.toFixed(1) + "%");
const chainLabel = (k) => (S.chains.find((c) => c.key === k) || {}).label || k;
const safeLink = (u) => typeof u === "string" && u.startsWith("https://") ? u : null;

// The one memorable visual: wallets on the left, all pointing at the same token.
function convergence(n) {
  const shown = Math.min(n, 5), el = svg("svg", { viewBox: "0 0 78 56", width: 78, height: 56, "aria-hidden": "true" });
  for (let i = 0; i < shown; i++) {
    const y = shown === 1 ? 28 : 8 + (40 * i) / (shown - 1);
    el.append(svg("line", { x1: 8, y1: y, x2: 58, y2: 28, stroke: "var(--line)", "stroke-width": 2 }));
    el.append(svg("circle", { cx: 8, cy: y, r: 5, fill: "var(--accent)" }));
  }
  el.append(svg("circle", { cx: 60, cy: 28, r: 11, fill: "var(--good)" }));
  const label = svg("text", { x: 60, y: 33, "text-anchor": "middle", "font-size": 14, "font-weight": 700, fill: "var(--bg)" });
  label.textContent = String(n);
  el.append(label);
  return el;
}

function renderSignals() {
  const el = $("#tab-signals"), st = S.settings, sigs = S.signals;
  el.replaceChildren();
  const strong = sigs.filter((s) => s.rating === "Strong").length;
  el.append(h("h2", {}, sigs.length ? `${sigs.length} ${sigs.length === 1 ? "token" : "tokens"} bought by ${st.min_wallets}+ of your wallets` : "No overlaps right now"));
  el.append(h("p", { class: "lead" }, sigs.length
    ? (strong ? `${strong} rated Strong. Check the risks before acting.` : "None rated Strong yet. Open a row to see why.")
    : S.wallets.length < 2 ? "Add at least two wallets on the Wallets tab. Signals appear when they buy the same token."
      : `When ${st.min_wallets} or more of your wallets buy the same token within ${st.window_hours} hours, it shows up here and you get an alert.`));

  const win = h("select", { "aria-label": "Time window", onchange: (e) => saveSettings({ window_hours: +e.target.value }) },
    st.windows.map((w) => h("option", { value: w, selected: w === st.window_hours }, w + (w === 1 ? " hour" : " hours"))));
  const min = h("select", { "aria-label": "Minimum wallets", onchange: (e) => saveSettings({ min_wallets: +e.target.value }) },
    [2, 3, 4, 5].map((n) => h("option", { value: n, selected: n === st.min_wallets }, n + " wallets")));
  el.append(h("div", { class: "rules" }, "Alert when", min, "buy the same token within", win));

  for (const s of sigs) {
    const name = s.symbol || short(s.token);
    el.append(h("div", { class: "coinrow" },
      h("span", { class: "sym" }, name),
      h("button", { class: "btn ghost sm", type: "button", "aria-haspopup": "dialog", onclick: () => openSignal(s) }, "Details")));
  }
  el.append(h("p", { class: "fine" }, "Signals show what your tracked wallets did. This is not financial advice. A token many wallets bought can still fall, and wallets can be wrong, early, or exiting."));
}


const COLORS = ["#3b3bd6", "#0b7a63", "#d9480f", "#a61e4d", "#0c8599"];
function renderGraph() {
  const el = $("#tab-graph"), c = S.chart, min = c.min_wallets;
  el.replaceChildren(h("h2", {}, "Wallets per coin"),
    h("p", { class: "lead" }, `Coins that ${min} or more of your wallets bought (first buy of that coin) in the last ${c.window_hours} h. The number is how many of your wallets bought it. Updates live.`));
  if (!c.top.length) return el.append(h("div", { class: "empty" }, `No coin has been bought by ${min} or more of your wallets yet. This chart appears by itself the moment it happens.`));
  const max = Math.max(...c.top.map((t) => t.count), 2), rowH = 34, W = 360;
  const bars = svg("svg", { viewBox: `0 0 ${W} ${c.top.length * rowH + 4}`, width: "100%", role: "img", "aria-label": "Wallets per coin" });
  c.top.forEach((t, i) => {
    const y = i * rowH + 4, w = Math.max(4, (t.count / max) * 190), hit = t.count >= min;
    const name = svg("text", { x: 0, y: y + 19, "font-size": 13, fill: "var(--ink)" }); name.textContent = (t.symbol || "").slice(0, 11);
    const bar = svg("rect", { x: 96, y: y + 5, width: w, height: 20, rx: 4, fill: "var(--good)" });
    const num = svg("text", { x: 96 + w + 6, y: y + 20, "font-size": 14, "font-weight": 700, fill: "var(--ink)" }); num.textContent = String(t.count);
    bars.append(name, bar, num);
  });
  el.append(bars);
  el.append(h("h3", {}, "Growth over time"));
  if (!c.series.length) { el.append(h("p", { class: "s" }, "Lines appear once a coin has been bought by 2 or more wallets.")); }
  else {
    const t0 = Math.min(...c.series.map((x) => x.points[0][0])), t1 = Math.max(c.now, ...c.series.map((x) => x.points.at(-1)[0]));
    const ymax = Math.max(...c.series.map((x) => x.points.at(-1)[1])), L = 26, R = 350, T = 10, B = 130, span = Math.max(t1 - t0, 60);
    const X = (t) => L + ((t - t0) / span) * (R - L), Y = (n) => B - (n / ymax) * (B - T);
    const g = svg("svg", { viewBox: "0 0 360 150", width: "100%", role: "img", "aria-label": "Wallet count growth per coin" });
    for (let n = 0; n <= ymax; n++) {
      g.append(svg("line", { x1: L, x2: R, y1: Y(n), y2: Y(n), stroke: "var(--line)", "stroke-width": 1 }));
      const lb = svg("text", { x: 0, y: Y(n) + 4, "font-size": 11, fill: "var(--muted)" }); lb.textContent = String(n); g.append(lb);
    }
    c.series.forEach((sr, i) => {
      let d = ""; sr.points.forEach(([t, n], k) => { d += (k ? ` H${X(t)} V${Y(n)}` : `M${X(t)} ${Y(n)}`); }); d += ` H${X(t1)}`;
      g.append(svg("path", { d, fill: "none", stroke: COLORS[i % 5], "stroke-width": 2.5 }));
    });
    el.append(g, h("div", { class: "rules" }, c.series.map((sr, i) => h("span", {}, h("b", { style: `color:${COLORS[i % 5]}` }, "\u25A0 "), sr.symbol))));
    el.append(h("p", { class: "s" }, `${ago(t0)} to now. Each step up is another wallet buying that coin for the first time.`));
  }
  el.append(h("h3", {}, "Who bought"));
  for (const t of c.top.filter((x) => x.count >= 2)) el.append(h("div", { class: "row" }, h("div", { class: "grow" }, h("div", { class: "t" }, `${t.symbol} (${t.count})`), h("div", { class: "s" }, t.wallets.join(", ")))));
}

let openSig = null, openSigJson = "";
function sigKey(s) { return s.chain + ":" + s.token; }
function fillSignal() {
  const dlg = $("#sigdlg"), s = S.signals.find((x) => sigKey(x) === openSig);
  if (!s) { if (dlg.open) dlg.close(); return; }
  const json = JSON.stringify(s);
  if (json === openSigJson && $("#sigbody").childNodes.length) return;   // unchanged: keep scroll and open sections
  openSigJson = json;
  const name = s.symbol || short(s.token), link = safeLink(s.url);
  $("#sigbody").replaceChildren(...[
    h("h2", { id: "sigtitle" }, name),
    h("div", { class: "s" }, chainLabel(s.chain)),
    addrLine(s.token, true),
    convergence(s.wallet_count),
    h("p", {}, h("b", {}, `${s.wallet_count} of your wallets`), ` bought this coin. Last buy ${ago(s.last_ts)}.`),
    h("ul", { class: "wl" }, s.wallets.map((w) => h("li", {}, w.label))),
    h("div", { class: "score" }, h("b", {}, String(s.score)), h("span", { class: "r-" + s.rating }, " " + s.rating)),
    h("dl", { class: "m" },
      h("div", {}, h("dt", {}, "Price"), h("dd", {}, usd(s.price_usd))),
      h("div", {}, h("dt", {}, "Liquidity"), h("dd", {}, usd(s.liquidity_usd))),
      h("div", {}, h("dt", {}, "24h"), h("dd", {}, pct(s.change_24h)))),
    s.flags.map((f) => h("div", { class: "flag " + f.level }, f.text)),
    h("details", {}, h("summary", {}, "How this score works"),
      h("p", { class: "fine" }, "45 points for how many of your wallets agree, 15 for how recent the buys are, 25 for liquidity (how easily you could sell), 15 for not having already surged. It ranks agreement and risk. It does not predict price.")),
    link && h("p", {}, h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, "Open chart on DexScreener")),
    h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => $("#sigdlg").close() }, "Close")),
  ].flat().filter(Boolean));
}
function openSignal(s) {
  openSig = sigKey(s); openSigJson = "";
  fillSignal();
  const dlg = $("#sigdlg");
  if (!dlg.open) dlg.showModal();
}
document.addEventListener("DOMContentLoaded", () => {
  const dlg = $("#sigdlg");
  dlg.addEventListener("close", () => { openSig = null; openSigJson = ""; });
  dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });   // click outside = close
});

function renderAllBuys() {
  const el = $("#tab-allbuys");
  el.replaceChildren(h("h2", {}, "All buys"),
    h("p", { class: "lead" }, "The latest buy transaction of every wallet you added, newest first. Repeat buys of the same coin are included here."));
  if (!S.all_buys.length) return el.append(h("div", { class: "empty" }, "Nothing yet. Buys appear here within seconds of confirming on-chain."));
  for (const b of S.all_buys) {
    const link = safeLink(b.tx_url);
    const delay = b.delay != null ? `, seen after ${b.delay < 60 ? Math.round(b.delay) + "s" : Math.round(b.delay / 60) + " min"}` : "";
    el.append(h("div", { class: "row" },
      h("div", { class: "grow" },
        h("div", { class: "t" }, coinTitle(b), b.is_first && h("span", { class: "badge" }, "NEW COIN")),
        addrLine(b.token),
        h("div", { class: "s" }, `${b.wallet_label} on ${chainLabel(b.chain)}, ${ago(b.ts)}${delay}`)),
      h("div", { class: "amt" }, b.amount != null ? Number(b.amount).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "",
        link && h("div", {}, h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, "View tx")))));
  }
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;opacity:0;top:0;left:0";
  (document.querySelector("dialog[open]") || document.body).append(ta);   // inside an open popup too, or the browser ignores it
  ta.select(); ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}
function copyText(text, btn) {
  const label = btn.dataset.label || btn.textContent;
  btn.dataset.label = label;
  const done = (ok) => { btn.textContent = ok ? "Copied" : "Copy failed"; clearTimeout(btn._t); btn._t = setTimeout(() => { btn.textContent = label; }, 1500); };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => done(true), () => done(fallbackCopy(text)));
  else done(fallbackCopy(text));   // plain http (e.g. phone on your Wi-Fi) has no clipboard API, so use the older way
}
// Coin name (symbol + full name) and its address with a Copy button.
function coinTitle(c) {
  if (!c.symbol) return h("span", { class: "noname" }, "Name not found yet");
  return c.name && c.name !== c.symbol ? `${c.symbol} \u00b7 ${c.name}` : c.symbol;
}
function addrLine(token, full) {
  return h("div", { class: "addr" + (full ? " full" : "") },
    h("code", { title: token }, full ? token : token.slice(0, 6) + "..." + token.slice(-6)),
    h("button", { class: "copy", type: "button", "aria-label": "Copy token address " + token, onclick: (e) => copyText(token, e.currentTarget) }, "Copy"));
}

function renderActivity() {
  const el = $("#tab-activity");
  el.replaceChildren(h("h2", {}, "Recent buys"), h("p", { class: "lead" }, "Only the FIRST time each wallet ever buys a coin. Repeat buys of the same coin are not shown."));
  if (!S.feed.length) return el.append(h("div", { class: "empty" }, "Nothing yet. New buys appear here within about " + S.settings.poll_seconds + " seconds of confirming on-chain."));
  for (const b of S.feed) {
    const link = safeLink(b.tx_url);
    el.append(h("div", { class: "row" },
      h("div", { class: "grow" },
        h("div", { class: "t" }, coinTitle(b)),
        addrLine(b.token),
        h("div", { class: "s" }, `${b.wallet_label} on ${chainLabel(b.chain)}, ${ago(b.ts)}, first buy of this coin` + (b.delay != null ? `, seen after ${b.delay < 60 ? Math.round(b.delay) + "s" : Math.round(b.delay / 60) + " min"}` : ""))),
      h("div", { class: "amt" }, b.amount != null ? Number(b.amount).toLocaleString(undefined, { maximumFractionDigits: 2 }) : "", link && h("div", {}, h("a", { href: link, target: "_blank", rel: "noopener noreferrer" }, "View tx")))));
  }
}

function renderWallets() {
  const el = $("#tab-wallets");
  const scroll = window.scrollY;
  el.replaceChildren(h("h2", {}, "Tracked wallets"), h("p", { class: "lead" }, "Public addresses only. Never enter a seed phrase or private key."));

  if (S.web && S.worker && !S.worker.alive) {
    el.append(h("div", { class: "usage", id: "workerdown" },
      h("div", { class: "t" }, "Background worker is not running"),
      h("div", { class: "s err" }, "Nothing is being tracked and no alerts will come. Start worker.py as an Always-on task (see DEPLOY_PYTHONANYWHERE.md).")));
  }
  const cn = S.conn;
  if (cn && !S.mock) {
    const sw = cn.solana_wallets;
    const rpcLine = !sw ? "No Solana wallets added yet."
      : cn.ok_wallets ? `Connected to ${cn.host}. ${cn.ok_wallets} of ${sw} wallets read successfully.`
      : cn.error ? `NOT working yet: ${cn.error}` : "Starting. Reading your wallets for the first time (this can take a minute)...";
    const wsLine = !cn.ws_enabled ? "Instant alerts: turned off (USE_WS=0)."
      : cn.ws_wallets ? `Instant alerts: ON for ${cn.ws_wallets} of ${sw} wallets.`
      : `Instant alerts: OFF${cn.ws_error ? " (" + cn.ws_error + ")" : " (connecting...)"}. Alerts will be slow until this is ON.`;
    el.append(h("div", { class: "usage", id: "conn" },
      h("div", { class: "t" }, "Connection"),
      h("div", { class: "s" + (!cn.ok_wallets && cn.error ? " err" : "") }, rpcLine),
      h("div", { class: "s" + (sw && !cn.ws_wallets && cn.ws_enabled ? " err" : "") }, wsLine),
      !cn.key_loaded && !cn.host.includes("helius") && h("div", { class: "s err" }, "Helius key not found. Run: python check.py YOUR_KEY")));
  }
  const u = S.usage;
  if (u && u.enabled) {
    const pct = Math.min(100, Math.round((u.used / u.limit) * 100));
    const modeText = { normal: "Normal", lean: "Saving mode (spending faster than planned)", paused: "Paused until next cycle" }[u.mode] || u.mode;
    el.append(h("div", { class: "usage", id: "usage" },
      h("div", { class: "t" }, "Helius requests this month"),
      h("progress", { max: u.limit, value: u.used, "aria-label": "Helius credits used this month" }),
      h("div", { class: "s" }, `${u.used.toLocaleString()} of ${u.limit.toLocaleString()} used (${pct}%). Today ${u.today.toLocaleString()} of about ${u.daily_budget.toLocaleString()}.`),
      h("div", { class: "s" }, `Mode: ${modeText}. Cycle resets ${new Date(u.period_end * 1000).toLocaleDateString()}. At this pace: about ${u.projected.toLocaleString()} this cycle.`)));
  }

  const chain = h("select", { id: "f-chain", onchange: (e) => (formDraft.chain = e.target.value) },
    S.chains.map((c) => h("option", { value: c.key, selected: c.key === formDraft.chain }, c.label + (c.ready ? "" : " (RPC needed)"))));
  const addr = h("input", { type: "text", id: "f-addr", value: formDraft.address, autocomplete: "off", autocapitalize: "off", spellcheck: "false", placeholder: "Paste wallet address", oninput: (e) => (formDraft.address = e.target.value) });
  const label = h("input", { type: "text", id: "f-label", value: formDraft.label, maxlength: 40, placeholder: "e.g. Whale 1", oninput: (e) => (formDraft.label = e.target.value) });
  const msg = h("div", { class: "s err", id: "f-msg", role: "alert" });
  el.append(h("form", { class: "add", onsubmit: addWallet },
    h("label", {}, "Chain", chain), h("label", {}, "Address", addr), h("label", {}, "Name (optional)", label),
    h("button", { class: "btn", type: "submit" }, "Add wallet"), msg));

  const notReady = S.chains.filter((c) => !c.ready);
  if (notReady.length) el.append(h("p", { class: "fine" }, notReady.map((c) => `${c.label} needs the ${c.env} setting on the server before it can be read.`).join(" ")));

  el.append(h("h3", {}, `${S.wallets.length} ${S.wallets.length === 1 ? "wallet" : "wallets"}`));
  if (!S.wallets.length) el.append(h("div", { class: "empty" }, "No wallets yet. Add the first one above."));
  for (const w of S.wallets) {
    const st = w.status;
    const status = !st ? "Waiting for first check" : st.ok ? `Checked ${ago(st.checked)}, ${w.buys} buys found` : null;
    el.append(h("div", { class: "row" },
      h("div", { class: "grow" },
        h("div", { class: "t" }, w.label),
        h("div", { class: "s" }, `${chainLabel(w.chain)}, ${short(w.address)}`),
        st && !st.ok ? h("div", { class: "s err" }, "Can't read this wallet: " + st.error) : h("div", { class: "s" }, status)),
      h("button", { class: "btn danger" + (armed === w.id ? " armed" : ""), "aria-label": "Remove " + w.label, onclick: () => removeWallet(w) }, armed === w.id ? "Tap again to remove" : "Remove")));
  }

  el.append(h("h3", {}, "Alerts"));
  const perm = "Notification" in window ? Notification.permission : "unsupported";
  el.append(h("p", { class: "s" }, perm === "granted" ? "Alerts on this device are on while the app is open." : perm === "denied" ? "Alerts are blocked in your browser settings for this site." : perm === "unsupported" ? "This browser can't show alerts." : "Show an alert on this device when a signal appears while the app is open."));
  if (perm === "default") el.append(h("button", { class: "btn ghost", onclick: async () => { await Notification.requestPermission(); renderWallets(); } }, "Turn on device alerts"));
  el.append(h("p", { class: "fine" },
    `Alerts when the app is closed: ntfy ${S.notify.ntfy ? "is on" : "is off"}, Telegram ${S.notify.telegram ? "is on" : "is off"}. Turn them on with the NTFY_TOPIC or TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID settings on the server (see README).`));
  window.scrollTo(0, scroll);
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (r.status === 401) { location.reload(); throw new Error("Login required"); }   // session ended: show the login form
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || "Request failed (" + r.status + ")");
  return data;
}
async function addWallet(e) {
  e.preventDefault();
  const msg = $("#f-msg"); msg.textContent = "";
  try {
    await api("/api/wallets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(formDraft) });
    formDraft.address = ""; formDraft.label = "";
    await refresh(); api("/api/poll", { method: "POST" }).catch(() => {});
  } catch (err) { msg.textContent = err.message; }
}
async function removeWallet(w) {
  if (armed !== w.id) { armed = w.id; renderWallets(); setTimeout(() => { if (armed === w.id) { armed = null; if (tab === "wallets") renderWallets(); } }, 4000); return; }
  armed = null;
  try { await api("/api/wallets/" + w.id, { method: "DELETE" }); } catch (err) { toast("Couldn't remove", err.message); }
  refresh();
}
async function saveSettings(patch) {
  try { await api("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch) }); } catch (err) { toast("Couldn't save", err.message); }
  setTimeout(refresh, 400);
}

function render() {
  if (!S) return;
  $("#demo").hidden = !S.mock;
  if (openSig) fillSignal();
  if (tab === "signals") renderSignals(); else if (tab === "graph") renderGraph(); else if (tab === "activity") renderActivity(); else if (tab === "allbuys") renderAllBuys();
  else if (!document.activeElement || !document.activeElement.matches("form.add input, form.add select")) renderWallets();
}
async function refresh() {
  try {
    S = await api("/api/state"); render();
    if (S.web) {   // hosted with a background worker: no live push, the page polls every 5 s
      if (window._es) { window._es.close(); window._es = null; }
      setLive(S.worker.alive, S.worker.alive ? "Live" : "Worker stopped");
    }
  } catch (err) { setLive(false, "Offline"); }
}
function setLive(on, text) { $("#dot").classList.toggle("on", on); $("#livetxt").textContent = text; }

function toast(title, body) {
  const t = h("div", { class: "toast", role: "status" }, h("b", {}, title), body || "");
  document.body.append(t); setTimeout(() => t.remove(), 7000);
}
function connect() {
  const es = window._es = new EventSource("/api/stream");
  es.onopen = () => setLive(true, "Live");
  es.onerror = () => { if (S && S.web) es.close(); else setLive(false, "Reconnecting"); };
  es.addEventListener("update", refresh);
  es.addEventListener("buy", refresh);
  es.addEventListener("tx", refresh);
  es.addEventListener("alert", (ev) => {
    const a = JSON.parse(ev.data);
    toast(a.title, a.body);
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
    if ("Notification" in window && Notification.permission === "granted" && navigator.serviceWorker)
      navigator.serviceWorker.ready.then((reg) => reg.showNotification(a.title, { body: a.body, icon: "/icon-192.png" }));
    refresh();
  });
}

document.querySelectorAll("nav.bar button").forEach((b) => b.addEventListener("click", () => {
  tab = b.dataset.tab;
  document.querySelectorAll("nav.bar button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  document.querySelectorAll("section.tab").forEach((s) => s.classList.toggle("active", s.id === "tab-" + tab));
  window.scrollTo(0, 0);
  if (tab === "wallets") { const f = document.activeElement; if (f) f.blur(); }
  render();
}));

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
refresh().then(connect);
setInterval(refresh, 10000);
setInterval(() => { if (S && S.web) refresh(); }, 5000);
setInterval(() => { if (S && tab !== "wallets") render(); }, 30000);

// ---- log out button (shown only when a password is set)
async function logout() {
  try { await fetch("/api/logout", { method: "POST" }); } catch { /* ignore */ }
  location.reload();
}
function showLogout() {
  if (!S || !S.auth_enabled || $("#logout")) return;
  $(".live").after(h("button", { id: "logout", class: "copy", type: "button", onclick: logout, "aria-label": "Log out" }, "Log out"));
}
setInterval(showLogout, 1000);
