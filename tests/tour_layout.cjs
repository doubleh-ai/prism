/* Execute the actual tour against layout doubles; no network or credentials. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function eventSource() {
  const listeners = new Map();
  return {
    listeners,
    addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(fn); },
    removeEventListener(name, fn) { listeners.get(name)?.delete(fn); },
    fire(name) { for (const fn of listeners.get(name) || []) fn(); },
    count() { return [...listeners.values()].reduce((n, set) => n + set.size, 0); }
  };
}

function setup(width = 1388, height = 668) {
  const nodes = new Map(), frames = new Map();
  let nextFrame = 0, contentHeight = 180;
  const target = {isConnected: true, classList: {add(){}, remove(){}},
    rect: {top: height - 110, bottom: height - 70, left: width - 250},
    getBoundingClientRect() { return this.rect; }};
  const viewport = Object.assign(eventSource(), {width, height, offsetLeft: 0, offsetTop: 0});
  const win = Object.assign(eventSource(), {visualViewport: viewport, innerHeight: height,
    requestAnimationFrame(fn) { frames.set(++nextFrame, fn); return nextFrame; },
    cancelAnimationFrame(id) { frames.delete(id); }});
  const doc = {documentElement: {clientWidth: width},
    body: {appendChild(node) { nodes.set(node.id, node); }},
    querySelector() { return target; }, querySelectorAll() { return [target]; },
    getElementById(id) { return nodes.get(id); },
    createElement() { return {style: {}, innerHTML: '',
      remove() { nodes.delete(this.id); },
      getBoundingClientRect() {
        // Reproduce the reported failure: hidden cards measure as zero.
        if (!this.style.display || this.style.display === 'none') return {height: 0, width: 0};
        return {height: Math.min(contentHeight, parseFloat(this.style.maxHeight) || Infinity),
          width: Math.min(340, parseFloat(this.style.width) || 340)};
      }}; }
  };
  const ctx = {App: {}, window: win, document: doc, esc: s => s};
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync('static/js/app.tour.js', 'utf8'), ctx);
  const app = ctx.App;
  app._tourStepsActive = [{sel: '#modelSelect', title: '模型选择', text: 'Choose a model'}, {sel: null, title: '完成', text: 'Done'}];
  const render = () => app._renderTourStep();
  const flush = () => { const queued = [...frames.values()]; frames.clear(); queued.forEach(fn => fn()); };
  function fits() {
    const tip = nodes.get('tourTip'), rect = tip.getBoundingClientRect();
    const left = parseFloat(tip.style.left), top = parseFloat(tip.style.top);
    assert(left >= viewport.offsetLeft + 12);
    assert(top >= viewport.offsetTop + 12);
    assert(left + rect.width <= viewport.offsetLeft + viewport.width - 12);
    assert(top + rect.height <= viewport.offsetTop + viewport.height - 12);
    assert.equal(tip.style.visibility, 'visible');
    return {top, left, ...rect};
  }
  return {app, viewport, win, target, nodes, frames, render, flush, fits, long() { contentHeight = 1600; }};
}

// Bottom-aligned model selector: measure before deciding to flip above it.
{
  const e = setup(); e.render();
  assert(e.fits().top + e.fits().height < e.target.rect.top);
}
for (const [width, height] of [[320, 568], [390, 240], [768, 320], [1388, 668]]) {
  const e = setup(width, height); e.render(); e.fits();
  e.long(); e.win.fire('resize'); e.flush(); e.fits();
  e.app._nextTourStep(); e.fits(); // Centred last step is also constrained.
}
// Zoom/pan or keyboard: account for visual viewport offset and reduced area.
{
  const e = setup(); e.render();
  Object.assign(e.viewport, {width: 360, height: 280, offsetLeft: 90, offsetTop: 130});
  e.viewport.fire('resize'); e.viewport.fire('scroll');
  assert.equal(e.frames.size, 1); e.flush(); e.fits();
  e.target.rect = {top: 170, bottom: 200, left: 110};
  e.win.fire('scroll'); e.flush();
  assert(e.fits().top >= 142);
}
// Repeated navigation must not accumulate listeners or revive a closed card.
{
  const e = setup(); e.render(); e.app._nextTourStep(); e.app._prevTourStep();
  assert.equal(e.win.count(), 2); assert.equal(e.viewport.count(), 2);
  e.win.fire('resize'); e.app._closeTour(); e.flush();
  assert.equal(e.win.count(), 0); assert.equal(e.viewport.count(), 0);
  assert.equal(e.frames.size, 0); assert.equal(e.nodes.size, 0);
}
// Older desktop browser without VisualViewport still constrains the card.
{
  const e = setup(); e.win.visualViewport = null; e.render(); e.fits(); e.app._closeTour();
}
console.log('tour layout ok: hidden measurement, edges, compact screens, zoom, scrolling, cleanup');
