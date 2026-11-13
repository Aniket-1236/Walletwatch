const $ = (s) => document.querySelector(s);
const msg = $("#msg"), btn = $("#go");
let totp = false;

fetch("/api/auth", { cache: "no-store" }).then((r) => r.json()).then((a) => {
  if (a.authed && a.mode === "login") { location.replace("/"); return; }
  totp = !!a.totp;
  $("#codebox").hidden = !totp;
  if (totp) $("#code").required = true;
}).catch(() => { msg.textContent = "Cannot reach the server."; });

$("#f").addEventListener("submit", async (e) => {
  e.preventDefault();
  msg.textContent = "";
  btn.disabled = true; btn.textContent = "Checking...";
  try {
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("#pw").value, code: totp ? $("#code").value : undefined }),
    });
    const data = await r.json().catch(() => ({}));
    if (r.ok) { $("#pw").value = ""; location.replace("/"); return; }
    msg.textContent = data.error || "Login failed (" + r.status + ")";
    $("#pw").value = ""; $("#pw").focus();
  } catch {
    msg.textContent = "Cannot reach the server.";
  }
  btn.disabled = false; btn.textContent = "Log in";
});
