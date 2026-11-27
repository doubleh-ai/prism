const assert = require('node:assert/strict');
const test = require('node:test');

test('activation removes only obsolete Prism caches', async () => {
  const handlers = {};
  const names = new Set(['prism-workbench-v0', 'other-product-cache']);
  let installed;
  global.self = {
    addEventListener: (name, callback) => { handlers[name] = callback; },
    skipWaiting() {},
    clients: { claim() {} },
  };
  global.caches = {
    async open(name) {
      installed = name;
      names.add(name);
      return { async addAll() {} };
    },
    async keys() { return [...names]; },
    async delete(name) { return names.delete(name); },
  };
  require('../static/sw.js');
  function dispatch(name) {
    let pending;
    handlers[name]({ waitUntil(promise) { pending = promise; } });
    return pending;
  }
  await dispatch('install');
  await dispatch('activate');
  assert.deepEqual(names, new Set(['other-product-cache', installed]));
});
