// MLBB Platform service worker — Batch 17.
// Safety-first policy for a live-money app:
//  - /actions is NEVER intercepted or cached (always fresh, always network).
//  - Navigations are NETWORK-FIRST (online users always get the freshest
//    HTML/bundle); the cached shell is only an offline fallback.
//  - Same-origin static assets are CACHE-FIRST (they are content-hashed).
//  - Non-GET requests and everything else are left completely untouched.
const CACHE_NAME = "mlbb-shell-v1";

const SHELL_URLS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./assets/icon-192.png",
  "./assets/icon-512.png",
  "./assets/icon-maskable-512.png",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_URLS))
      .catch(() => {
        // Precache is best-effort: a missing shell entry must never
        // block the worker from installing.
      }),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name !== CACHE_NAME)
            .map((name) => caches.delete(name)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;

  // Ignore non-GET requests entirely.
  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch {
    return;
  }

  // NEVER intercept or cache the actions API — let it go to the network
  // untouched so money/session data is never stale.
  if (url.pathname.includes("/actions")) return;

  // Only handle same-origin requests; everything else (cross-origin,
  // /blobs/, other paths) is not intercepted.
  if (url.origin !== self.location.origin) return;

  // Navigation requests: NETWORK-FIRST. On success, refresh the cached
  // shell copy; only on network failure fall back to the cache.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches
            .open(CACHE_NAME)
            .then((cache) => cache.put("./index.html", copy))
            .catch(() => {});
          return response;
        })
        .catch(async () => {
          const cache = await caches.open(CACHE_NAME);
          const cached =
            (await cache.match("./index.html")) ||
            (await cache.match("./")) ||
            (await cache.match(request));
          if (cached) return cached;
          throw new Error("offline and no cached shell");
        }),
    );
    return;
  }

  // Static assets: CACHE-FIRST, populating the cache on a miss.
  const isStaticAsset =
    url.pathname.includes("/assets/") ||
    url.pathname.endsWith("/manifest.json") ||
    url.pathname.endsWith("/icon-192.png") ||
    url.pathname.endsWith("/icon-512.png") ||
    url.pathname.endsWith("/icon-maskable-512.png");

  if (isStaticAsset) {
    event.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const cached = await cache.match(request);
        if (cached) return cached;
        const response = await fetch(request);
        if (response && response.ok) {
          cache.put(request, response.clone()).catch(() => {});
        }
        return response;
      }),
    );
    return;
  }

  // Everything else: do not intercept.
});
