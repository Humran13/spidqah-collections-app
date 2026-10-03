/*
 * SPIDQAH service worker - deliberately conservative for a financial app.
 *
 * Cached:   versioned static assets (/static/*) and generated app icons.
 * Never:    pages, POST/any non-GET requests, JSON/API responses, reports,
 *           exports or the manifest. Those always go to the network.
 * Offline:  a navigation that cannot reach the server shows a static
 *           "reconnect" page. Stale financial data is never displayed.
 *
 * The file is served by Flask with the build configuration injected
 * (see app/blueprints/pwa/routes.py). Every change here bumps the build, so
 * browsers install this worker and delete old static caches.
 */
const CONFIG = __SW_CONFIG__;
const STATIC_CACHE = "spidqah-static-" + CONFIG.build;
const CACHE_PREFIX = "spidqah-static-";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.add(CONFIG.offlineUrl)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key.startsWith(CACHE_PREFIX) && key !== STATIC_CACHE).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") {
    return; // POST, PUT, DELETE... are never intercepted.
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  if (url.pathname.startsWith(CONFIG.staticPrefix)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  if (url.pathname.startsWith(CONFIG.iconPrefix)) {
    // Icon URLs carry ?v=<version>, so a cached copy is only ever used for the same version.
    event.respondWith(cacheFirst(request));
    return;
  }
  if (request.mode === "navigate") {
    event.respondWith(fetch(request, { cache: "no-store" }).catch(() => offlinePage()));
  }
  // Anything else falls through to the browser's normal network handling.
});

async function staleWhileRevalidate(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response.ok && response.type === "basic") {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

async function cacheFirst(request) {
  const cache = await caches.open(STATIC_CACHE);
  const cached = await cache.match(request);
  if (cached) {
    return cached;
  }
  const response = await fetch(request);
  if (response.ok) {
    cache.put(request, response.clone());
  }
  return response;
}

async function offlinePage() {
  const cached = await caches.match(CONFIG.offlineUrl);
  return cached || new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } });
}
