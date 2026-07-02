/* Prism Service Worker — offline cache for static assets.
   Cache-first with network update for /static/*, network-only for /api/*.
   Versioned by build ID so deploys bust the cache atomically. */
const CACHE_NAME = 'prism-v1';
const STATIC_PREFIX = '/static/';

// Assets to pre-cache on install
const PRE_CACHE = [
  '/',
  '/static/css/app.css',
  '/static/js/app.util.js',
  '/static/js/app.core.js',
  '/static/js/app.chat.js',
  '/static/js/app.market.js',
  '/static/js/app.analysis.js',
  '/static/lib/marked.min.js',
  '/static/lib/echarts.min.js',
];

self.addEventListener('install', function(ev) {
  ev.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRE_CACHE).catch(function() {
        // Some assets may 307 redirect to /login — that's fine, skip them
      });
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', function(ev) {
  ev.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(keys.filter(function(k) { return k !== CACHE_NAME; }).map(function(k) {
        return caches.delete(k);
      }));
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', function(ev) {
  var url = new URL(ev.request.url);
  // Never cache API calls
  if (url.pathname.startsWith('/api/')) return;
  // Static assets: cache-first, network-update in background
  if (url.pathname.startsWith(STATIC_PREFIX) || url.pathname === '/' || url.pathname === '/login') {
    ev.respondWith(
      caches.open(CACHE_NAME).then(function(cache) {
        return cache.match(ev.request).then(function(cached) {
          var fetchPromise = fetch(ev.request).then(function(response) {
            if (response.ok && response.type !== 'opaque') {
              cache.put(ev.request, response.clone());
            }
            return response;
          }).catch(function() {
            return cached || new Response('Offline', {status: 503});
          });
          return cached || fetchPromise;
        });
      })
    );
  }
});
