// ---- Monetag (site verification + push). Their code, unchanged. A site can have only ONE service worker per scope,
// ---- so it lives in this same /sw.js file together with our own app-shell caching below.
self.options = {
    "domain": "3nbf4.com",
    "zoneId": 12369137
}
self.lary = ""
try { importScripts('https://3nbf4.com/act/files/service-worker.min.js?r=sw') } catch (e) { /* blocked or offline: keep the app working */ }

// Service worker: caches the app shell only. Odds/picks are never cached (stale odds would mislead).
const CACHE = "edge-shell-v5";
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
  if (url.pathname.startsWith("/pay/") || url.pathname.startsWith("/admin") || url.pathname.startsWith("/ad-frame")) return;   // never cache payment or admin pages
  if (req.mode === "navigate") {   // network first, fall back to the cached shell when offline
    e.respondWith(fetch(req).then(r => { if (url.pathname === "/" && r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put("/", copy)); } return r; })
      .catch(() => caches.match("/")));
    return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req)));
});
