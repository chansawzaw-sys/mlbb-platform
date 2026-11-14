// Fight for Legendary service worker — Batch 20 (cache v15; Batch 17/18 fetch
// policy unchanged, plus Web Push display + click routing). v15: splash
// intro retimed to the 0.0/0.5/1.2/2.0/2.8/3.5s beat map in index.html.
// v16: persistent media cache + BGM build — blob media (/blobs/) and
// bundled /assets/ files are served cache-first from a version-free
// media cache that survives shell upgrades, so images and background
// music no longer re-download on every visit.
// v17: dispute Draw outcome + win-award penalty (fine/ban), draggable
// BGM button, and refresh restore (reload skips the splash and returns
// to the session's saved view).
// Safety-first policy for a live-money app:
//  - /actions is NEVER intercepted or cached (always fresh, always network).
//  - Navigations are NETWORK-FIRST (online users always get the freshest
//    HTML/bundle); the cached shell is only an offline fallback.
//  - Same-origin static assets are CACHE-FIRST (they are content-hashed).
//  - Non-GET requests and everything else are left completely untouched.
const CACHE_NAME = "mlbb-shell-v17";
// Media cache: deliberately NOT versioned with the shell — uploaded
// images / BGM tracks keep their timestamped URLs, so a cached copy is
// valid until the URL itself changes.
const MEDIA_CACHE = "mlbb-media-v1";

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
            // Delete ONLY stale shell caches — the media cache (and any
            // non-shell cache) must survive shell upgrades.
            .filter(
              (name) =>
                name.startsWith("mlbb-shell-") &&
                name !== CACHE_NAME &&
                name !== MEDIA_CACHE,
            )
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
  // other paths) is not intercepted.
  if (url.origin !== self.location.origin) return;

  // Navigation requests: NETWORK-FIRST. On success, refresh the cached
  // shell copy; only on network failure fall back to the cache.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Batch 29: only the app root navigation may refresh the cached
          // shell — static info pages (/about, /terms, /privacy, /contact)
          // must never be cached AS index.html.
          if (url.pathname === "/") {
            const copy = response.clone();
            caches
              .open(CACHE_NAME)
              .then((cache) => cache.put("./index.html", copy))
              .catch(() => {});
          }
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

  // Media: CACHE-FIRST from the persistent media cache. Uploaded blobs
  // (/blobs/ — avatars, slideshow + wizard images, payment QRs, BGM
  // tracks) and bundled /assets/ art carry timestamped / content-hashed
  // URLs, so a cache hit is always the right bytes and repeat visits
  // never re-download them. GET only (guaranteed above). manifest.json
  // is NOT matched here — it stays in the shell cache below.
  const isMedia =
    url.pathname.startsWith("/blobs/") ||
    (url.pathname.includes("/assets/") && !url.pathname.endsWith("/manifest.json"));

  if (isMedia) {
    event.respondWith(
      caches.open(MEDIA_CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        if (cached) return cached;
        const response = await fetch(request);
        if (response && response.ok && response.status === 200) {
          cache.put(request, response.clone()).catch(() => {});
        }
        return response;
      }),
    );
    return;
  }

  // Static assets: CACHE-FIRST, populating the cache on a miss.
  // (/assets/ is handled by the media branch above; manifest.json and
  // the app icons stay here in the shell cache.)
  const isStaticAsset =
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

// Batch 20: Web Push — the server sends an encrypted JSON payload
// ({title, body, tag, url, match_id, challenge_id, dm_partner_id});
// display it as a system notification even with the app fully closed.
self.addEventListener("push", (event) => {
  let payload = {};
  if (event.data) {
    try {
      payload = event.data.json() || {};
    } catch {
      try {
        payload = { body: event.data.text() };
      } catch {
        payload = {};
      }
    }
  }
  const title = payload.title || "Fight for Legendary";
  const options = {
    body: payload.body || "",
    icon: "./assets/icon-192.png",
    badge: "./assets/icon-192.png",
    tag: payload.tag || undefined,
    renotify: !!payload.tag,
    data: {
      url: payload.url || "/",
      match_id: payload.match_id ?? null,
      challenge_id: payload.challenge_id ?? null,
      dm_partner_id: payload.dm_partner_id ?? null,
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

// Batch 20: tapping a push notification focuses the app (navigating
// the existing window to the deep link) or opens a new window.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then(async (clientList) => {
        for (const client of clientList) {
          if ("focus" in client) {
            await client.focus();
            if (typeof client.navigate === "function") {
              try {
                await client.navigate(target);
              } catch {
                // Navigation can fail cross-path; the focused window
                // still lands the user in the app.
              }
            }
            return;
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
      }),
  );
});
