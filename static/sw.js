// Service worker: caches the app shell only. Odds/picks are never cached (stale odds would mislead).
const CACHE = "edge-shell-v2";
const SHELL = ["/", "/static/icon-192.png", "/static/icon-512.png", "/static/logo-wide.png", "/manifest.webmanifest"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  if (req.mode === "navigate") {   // network first, fall back to the cached shell when offline
    e.respondWith(fetch(req).then(r => { caches.open(CACHE).then(c => c.put("/", r.clone())); return r; })
      .catch(() => caches.match("/")));
    return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
});
