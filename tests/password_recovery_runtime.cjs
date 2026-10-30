'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/password-recovery.js', 'utf8');
async function setup(mode) {
  const elements = {};
  for (const id of ['recoveryStatus', 'sendCode', 'resetForm', 'resetPassword', 'code', 'newPassword', 'confirmPassword']) {
    elements[id] = {value: '', disabled: id === 'sendCode', hidden: id === 'resetForm', textContent: '',
      events: {}, focus() {}, addEventListener(event, fn) { this.events[event] = fn; }};
  }
  const calls = [], timers = [];
  const fetch = async (path, options) => {
    calls.push({path, options});
    if (path.endsWith('/status')) return {ok: true, json: async () => ({configured: mode !== 'disabled'})};
    if (mode === 'network') throw new TypeError('synthetic network failure');
    if (path.endsWith('/request')) return {ok: true, json: async () => ({challenge_id: 'synthetic', detail: 'accepted'})};
    return {ok: mode !== 'invalid', json: async () => ({detail: mode === 'invalid' ? 'invalid code' : 'reset complete'})};
  };
  vm.runInNewContext(source, {document: {getElementById: id => elements[id]}, fetch,
    setTimeout: fn => {timers.push(fn); return timers.length;}, clearTimeout() {}, TextEncoder, TypeError, Error});
  await new Promise(resolve => setImmediate(resolve));
  return {elements, calls, timers};
}
(async function () {
  const disabled = await setup('disabled');
  assert.equal(disabled.elements.sendCode.disabled, true);
  assert.match(disabled.elements.recoveryStatus.textContent, /尚未启用/);
  assert.equal(disabled.calls.length, 1);
  for (const mode of ['success', 'invalid', 'network']) {
    const {elements: e, calls, timers} = await setup(mode);
    await e.sendCode.events.click();
    assert.equal(calls[1].options.headers['X-Requested-With'], 'XMLHttpRequest');
    assert.equal(calls[1].options.body, '{}');
    assert.equal(calls[1].options.cache, 'no-store');
    await e.sendCode.events.click(); // Disabled button cannot double-send.
    assert.equal(calls.length, 2);
    if (mode === 'network') {
      assert.equal(e.resetForm.hidden, true);
      assert.match(e.recoveryStatus.textContent, /未确认/);
      timers[0]();
      assert.equal(calls.length, 2); // Timer enables manual retry, never sends.
      continue;
    }
    assert.equal(e.resetForm.hidden, false);
    e.newPassword.value = 'synthetic-long-password'; e.confirmPassword.value = 'does-not-match';
    await e.resetForm.events.submit({preventDefault() {}});
    assert.equal(calls.length, 2);
    e.confirmPassword.value = e.newPassword.value; e.code.value = '12345678';
    await e.resetForm.events.submit({preventDefault() {}});
    assert.equal(calls.length, 3);
    assert.equal(e.newPassword.value, ''); assert.equal(e.confirmPassword.value, ''); assert.equal(e.code.value, '');
    assert.equal(e.resetForm.hidden, mode === 'success');
    assert.equal(e.recoveryStatus.textContent, mode === 'success' ? 'reset complete' : 'invalid code');
  }
  console.log('4 password recovery frontend scenarios passed');
}()).catch(error => {console.error(error); process.exitCode = 1;});
