/* Actual worker/client scripts; no browser network, credentials or production. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

async function workerChecks() {
  const listeners = {}, events = [], cached = [], removed = [];
  const context = {
    URL, Response, Promise,
    self: {
      location: {origin: 'https://example.test'},
      addEventListener(type, handler) { listeners[type] = handler; },
      skipWaiting() { throw Error('must not interrupt an active editor'); },
      clients: {
        claim: async () => events.push('claim'),
        matchAll: async () => [{navigate() { throw Error('draft lost'); }}]
      }
    },
    caches: {
      open: async () => ({addAll: async paths => cached.push(...paths)}),
      keys: async () => ['prism-workbench-v87', 'prism-workbench-v88'],
      delete: async key => removed.push(key),
      match: async () => { throw Error('private navigation must never read old caches'); }
    },
    fetch: async () => { throw Error('offline'); }
  };
  vm.runInNewContext(fs.readFileSync('static/sw.js', 'utf8'), context);
  let promise;
  listeners.install({waitUntil(value) { promise = value; }}); await promise;
  assert(cached.length > 10 && cached.every(path => path.startsWith('/static/') && path.endsWith('?v=88')));
  listeners.activate({waitUntil(value) { promise = value; }}); await promise;
  assert.deepEqual(events, ['claim']); assert.deepEqual(removed, ['prism-workbench-v87']);
  const pass = request => {
    let intercepted = false;
    listeners.fetch({request, respondWith() { intercepted = true; }});
    assert.equal(intercepted, false);
  };
  pass({url: 'https://example.test/api/work/overview', method: 'GET'});
  pass({url: 'https://other.test/static/private.js', method: 'GET'});
  pass({url: 'https://example.test/static/upload', method: 'POST'});
  let response;
  listeners.fetch({request: {url: 'https://example.test/work', method: 'GET', mode: 'navigate'},
    respondWith(value) { response = value; }});
  const offline = await response;
  assert.equal(offline.status, 503); assert.equal(offline.headers.get('cache-control'), 'no-store');
}

async function clientChecks() {
  const handlers = {}, elements = new Map(), notes = [];
  const element = () => ({textContent: '', style: {}, classList: {add() {}}, addEventListener() {}});
  const context = {
    console, Set, Map, URLSearchParams,
    navigator: {serviceWorker: {
      controller: {}, addEventListener(type, cb) { handlers[type] = cb; },
      register: async () => ({waiting: {}, update() {}})
    }},
    document: {addEventListener() {}, documentElement: element(), getElementById(id) {
      if (!elements.has(id)) elements.set(id, element()); return elements.get(id);
    }},
    localStorage: {getItem() { return null; }}, sessionStorage: {getItem() { return null; }},
    window: {addEventListener() {}, location: {reload() { throw Error('must not discard editor'); }}},
    setTimeout, clearTimeout
  };
  vm.runInNewContext(fs.readFileSync('static/js/app.core.js', 'utf8'), context);
  const app = context.App;
  app.api = async () => ({ok: true, json: async () => ({user: 'synthetic'})});
  ['loadSkills', 'loadModels', 'loadConvs', 'showWelcome', 'renderSkillsBar', '_bindScroll',
    '_bindGlossClicks', '_bindMarketModal', '_loadWatchlist', '_bindMarketPanel',
    '_bindAnalysisSearch', '_trackUiEvent'].forEach(name => { app[name] = () => {}; });
  app._toast = text => notes.push(text);
  const input = element(); input.value = 'unsaved synthetic draft'; elements.set('input', input);
  await app.init(); await Promise.resolve();
  assert(notes.some(text => text.includes('保存')));
  handlers.controllerchange();
  assert.equal(input.value, 'unsaved synthetic draft'); assert.equal(notes.length, 2);
}

(async () => {
  await workerChecks(); await clientChecks();
  console.log('service worker checks ok: updates preserve drafts and private responses');
})().catch(error => { console.error(error); process.exitCode = 1; });
