// Service worker — enables PWA install. Network-first so the app
// is never stale; the cache is only an offline fallback for the
// shell.
//
// Build 193: the fallback used to be whatever the INSTALL-time
// fetch cached, so a phone whose network stalled at reopen showed
// that old build's shell until the network recovered. Now every
// successful shell response is written back into the cache, so
// the offline fallback is always the most recent build the device
// actually loaded. (The page also polls /api/version and reloads
// once when the server has moved on — see index.html.)
const CACHE = "vt-shell-v3";
const SHELL = ["/", "/static/manifest.webmanifest", "/static/icon-192.png", "/static/icon-512.png"];

function isShellRequest(req) {
  if (req.mode === "navigate") return true;
  try {
    const p = new URL(req.url).pathname;
    return p === "/" || SHELL.indexOf(p) !== -1;
  } catch (err) {
    return false;
  }
}

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => {}))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
      for (const c of cs) {
        if ("focus" in c) return c.focus();
      }
      return clients.openWindow("/");
    })
  );
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  e.respondWith(
    fetch(e.request)
      .then((resp) => {
        // Runtime-cache the fresh shell: the NEXT offline open
        // must fall back to this build, not the install-time one.
        if (resp && resp.ok && isShellRequest(e.request)) {
          const copy = resp.clone();
          e.waitUntil(
            caches.open(CACHE)
              .then((c) => c.put(e.request, copy))
              .catch(() => {})
          );
        }
        return resp;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
