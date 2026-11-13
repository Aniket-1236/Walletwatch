// Offline shell is deliberately NOT cached: the panel is private and only available after login.
const SHELL = "walletwatch-shell-v4";
self.addEventListener("install", (e) => { e.waitUntil(self.skipWaiting()); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", () => { /* network only */ });
