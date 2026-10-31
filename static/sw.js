/* Prism Service Worker - offline cache for static assets.
   Network-first for app-shell navigations so deploys cannot get stuck behind
   an old cached / page; cache-first with background update for /static/*. */
const CACHE_NAME = 'prism-workbench-v94';
const STATIC_PREFIX = '/static/';

const PRE_CACHE = [
  '/static/prism-mark.svg',
  '/static/css/app.css',
  '/static/css/desk.css',
  '/static/css/work.css',
  '/static/js/app.work.js',
  '/static/js/app.hub.js',
  '/static/js/app.handoff.js',
  '/static/js/app.util.js',
  '/static/js/app.core.js',
  '/static/js/app.diagnostics.js',
  '/static/js/app.feedback.js',
  '/static/js/app.chat-render.js',
  '/static/js/app.chat.js',
  '/static/js/app.market.js',
  '/static/js/app.vibe.js',
  '/static/js/app.analysis.js',
  '/static/js/app.reports.js',
  '/static/js/app.screener.js',
  '/static/js/app.indicator-chart.js',
  '/static/js/app.admin.js',
  '/static/js/app.release.js',
  '/static/js/app.tour.js',
  '/static/js/app.industry.js',
  '/static/js/app.indicators.js',
  '/static/js/app.workbench.js',
  '/static/js/app.operations.js',
  '/static/js/app.research.js',
  '/static/lib/marked.min.js',
  '/static/lib/echarts.min.js',
];

self.addEventListener('install', function(ev) {
  ev.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(PRE_CACHE.map(function(path) { return path + '?v=94'; })).catch(function() {});
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
    }).then(function() {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function(ev) {
  var url = new URL(ev.request.url);
  if (url.pathname.startsWith('/api/')) return;

  if (ev.request.mode === 'navigate' || url.pathname === '/' || url.pathname === '/login') {
    ev.respondWith(
      fetch(ev.request, {cache: 'no-store'}).catch(function() {
        return new Response('Offline', {status: 503});
      })
    );
    return;
  }

  if (url.pathname.startsWith(STATIC_PREFIX)) {
    ev.respondWith(
      caches.open(CACHE_NAME).then(function(cache) {
        return cache.match(ev.request).then(function(cached) {
          var fetchPromise = fetch(ev.request).then(function(response) {
            if (response.ok && response.type !== 'opaque') cache.put(ev.request, response.clone());
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
