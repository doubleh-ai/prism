/* Execute the actual Work script with an event-capable DOM and controlled HTTP. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '..');
const template = fs.readFileSync(path.join(ROOT, 'templates/work.html'), 'utf8');
const script = fs.readFileSync(path.join(ROOT, 'static/js/app.work.js'), 'utf8');
const ids = new Set([...template.matchAll(/id="([^"]+)"/g)].map(match => match[1]));

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.value = ''; this.textContent = '';
    this.dataset = {}; this.attributes = {}; this.hidden = false; this.disabled = false;
    this.listeners = {}; this.validationMessage = ''; this.className = '';
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; this.textContent = ''; }
  get firstChild() { return this.children[0]; }
  set innerHTML(_) { throw new Error('Work must not interpolate private content into HTML'); }
  get innerHTML() { throw new Error('Work must not read HTML as task data'); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(event, fn) { (this.listeners[event] ||= []).push(fn); }
  async fire(event, details = {}) {
    for (const fn of this.listeners[event] || []) await fn({preventDefault() {}, ...details});
  }
  setCustomValidity(value) { this.validationMessage = value; }
  focus() { this.focused = true; }
  scrollIntoView() {}
  reset() { if (this.onReset) this.onReset(); }
  reportValidity() { return true; }
  requestSubmit() { this.fire('submit'); }
}
const success = data => ({ok: true, status: 200, json: async () => data});
const failure = status => ({ok: false, status, json: async () => ({detail: 'not echoed'})});
const space = (id = 'space-owner-A', timezone = 'Asia/Shanghai') => ({id, name: '我的工作', timezone, version: 1});
const task = (id = 1, changes = {}) => ({id, workspace_id: 'space-owner-A', title: '准备评审', description: '核对交付清单', status: 'todo', priority: 'normal', due_at: null, timezone: 'Asia/Shanghai', version: 1, created_at: 1793059200, updated_at: 1793059200, ...changes});
const overview = (tasks = [], workspace = space(), next_cursor = null) => ({workspace, tasks, next_cursor, capabilities: {tasks: true, calendar: false, reminders: false, agent_actions: false}});
const tick = () => new Promise(setImmediate);
function text(el) { return el.textContent + el.children.map(text).join(''); }
function environment() {
  const elements = new Map([...ids].map(id => [id, new Element()]));
  const filters = ['today', 'all', 'done'].map(value => { const el = new Element('button'); el.dataset.workFilter = value; return el; });
  const requests = [], windowEvents = {}, timers = new Map(); let timer = 0;
  const document = {
    readyState: 'loading', hidden: false, listeners: {},
    getElementById(id) { assert(elements.has(id), `Missing actual template element ${id}`); return elements.get(id); },
    createElement(tag) { return new Element(tag); },
    querySelectorAll(selector) { assert.equal(selector, '[data-work-filter]'); return filters; },
    addEventListener(name, fn) { this.listeners[name] = fn; }
  };
  const context = {document, Intl, Date, Number, String, Map, Set, Array, Object, JSON, Promise, Error, AbortController,
    crypto: {randomUUID}, console, encodeURIComponent,
    confirm() { return true; },
    addEventListener(name, fn) { windowEvents[name] = fn; },
    setTimeout(fn, delay) { timers.set(++timer, {fn, delay}); return timer; }, clearTimeout(id) { timers.delete(id); },
    fetch(url, options) { return new Promise((resolve, reject) => { requests.push({url, options, resolve, reject}); }); }
  };
  Object.defineProperty(context, 'localStorage', {get() { throw new Error('No private persistent cache'); }});
  Object.defineProperty(context, 'sessionStorage', {get() { throw new Error('No private persistent cache'); }});
  context.window = context; vm.createContext(context); vm.runInContext(script, context, {filename: 'app.work.js'});
  const e = id => elements.get(id);
  e('workCreateForm').onReset = () => { ['workTitle', 'workDescription', 'workDue', 'workDueChoice'].forEach(id => { e(id).value = ''; }); e('workPriority').value = 'normal'; };
  e('workDetailForm').onReset = () => { ['workEditTitle', 'workEditDescription', 'workEditStatus', 'workEditPriority'].forEach(id => { e(id).value = ''; }); };
  e('workPriority').value = 'normal';
  context.WorkTasks.init();
  return {app: context.WorkTasks, context, e, requests, filters, timers, windowEvents};
}
async function start(env, data = overview()) { env.requests[0].resolve(success(data)); await tick(); }

async function initialReadIsPrivateAndReadOnly() {
  const env = environment(); await start(env, overview([], null));
  assert.equal(env.requests.length, 1); assert.equal(env.requests[0].url, '/api/work/overview?limit=50');
  assert.equal(env.requests[0].options.method, 'GET'); assert.equal(env.requests[0].options.cache, 'no-store');
  assert.equal(env.requests[0].options.credentials, 'same-origin');
  assert.equal(env.e('workWelcome').hidden, false); assert.equal(env.e('workContent').hidden, true);
  assert.match(template, /开发预览/); assert.match(template, /日历、提醒与 Agent 操作仍在后续规划/);
  assert(!/https?:\/\//.test(template), 'no external assets');
}
async function explicitEnableAndUnknownReplay() {
  const env = environment(); await start(env, overview([], null));
  const first = env.app.enableSpace(); const request = env.requests[1];
  assert.equal(request.options.method, 'POST'); assert.equal(request.options.headers['X-Requested-With'], 'XMLHttpRequest');
  request.resolve(failure(503)); await first;
  const original = request.options.body; assert(env.app.state.pendingSpace);
  const replay = env.app.enableSpace(); assert.equal(env.requests[2].options.body, original);
  env.requests[2].resolve(success({workspace: space(), replayed: true})); await tick();
  env.requests[3].resolve(success(overview())); await replay;
  assert.equal(env.app.state.pendingSpace, null); assert.equal(env.e('workContent').hidden, false);
}
async function failedCreationKeepsIdAndPayload() {
  const env = environment(); await start(env);
  env.e('workTitle').value = '网络中断时也不要重复创建';
  env.e('workDescription').value = '原始草稿';
  const first = env.app.createTask(); assert.equal(env.requests.length, 2);
  env.requests[1].reject(new Error('unknown transport')); await first;
  const original = env.requests[1].options.body;
  assert(env.app.state.pendingCreate); assert.equal(env.e('workTitle').disabled, true);
  env.e('workTitle').value = '脚本试图替换输入';
  const second = env.app.createTask(); assert.equal(env.requests[2].options.body, original);
  env.requests[2].resolve(success({task: task(2), replayed: true})); await tick();
  assert.equal(env.requests[3].options.method, 'GET');
  env.requests[3].resolve(success({task: task(2, {version: 3, status: 'doing'}), history: []})); await second;
  assert.equal(env.app.state.pendingCreate, null); assert.equal(env.app.state.tasks.size, 1);
  assert.equal(env.app.state.tasks.get('2').version, 3); assert.equal(env.e('workTitle').value, '');
}
async function validationRejectionKeepsEditableDraft() {
  const env = environment(); await start(env); env.e('workTitle').value = '草稿保留';
  const pending = env.app.createTask(); env.requests[1].resolve(failure(422)); await pending;
  assert.equal(env.app.state.pendingCreate, null); assert.equal(env.e('workTitle').value, '草稿保留');
  assert.equal(env.e('workTitle').disabled, false);
}
async function doubleClickCannotWriteTwice() {
  const env = environment(); await start(env); env.e('workTitle').value = '单次保存';
  const first = env.app.createTask(); await env.app.createTask(); assert.equal(env.requests.length, 2);
  env.requests[1].resolve(success({task: task(8), replayed: false})); await first;
  assert.equal(env.app.state.tasks.size, 1);
}
async function dstUsesActualZoneAndRequiresChoice() {
  const env = environment(); await start(env, overview([], space('space-owner-A', 'America/Toronto')));
  assert.equal(env.app.possibleInstants('2026-03-08T02:30', 'America/Toronto').length, 0);
  const fold = env.app.possibleInstants('2026-11-01T01:30', 'America/Toronto');
  assert.deepEqual(Array.from(fold, item => item.iso), ['2026-11-01T05:30:00.000Z', '2026-11-01T06:30:00.000Z']);
  assert.equal(env.app.possibleInstants('2026-10-27T09:00', 'Asia/Kathmandu')[0].iso, '2026-10-27T03:15:00.000Z');
  assert.throws(() => env.app.possibleInstants('2026-02-30T09:00', 'Asia/Shanghai'));
  env.e('workTitle').value = '选择夏令时'; env.e('workDue').value = '2026-11-01T01:30'; env.app.updateDue();
  assert.equal(env.e('workDueChoiceLabel').hidden, false); await env.app.createTask(); assert.equal(env.requests.length, 1);
  env.e('workDueChoice').value = fold[1].iso;
  const save = env.app.createTask();
  const body = JSON.parse(env.requests[1].options.body); assert.equal(body.due_at, fold[1].iso); assert.equal(body.timezone, 'America/Toronto');
  env.requests[1].resolve(success({task: task(1, {due_at: fold[1].iso}), replayed: false})); await save;
}
async function privateContentIsAlwaysText() {
  const env = environment(); const injection = '<img src=x onerror=steal()>工作事项';
  await start(env, overview([task(1, {title: injection, description: '<script>secret()</script>'})]));
  assert(text(env.e('workTaskList')).includes(injection));
  assert.equal(env.e('workTaskList').children[0].children[1].children[0].tagName, 'SPAN');
  const opened = env.app.openTask(1);
  env.requests[1].resolve(success({task: task(1, {title: injection}), history: [{action: 'task_created', version: 1, changed_fields: ['title'], created_at: 1793059200}]}));
  await opened; assert.equal(env.e('workEditTitle').value, injection); assert.match(text(env.e('workHistory')), /创建任务/);
}
async function filtersAndBoundedCountsAreHonest() {
  const env = environment();
  const tomorrow = new Date(Date.now() + 3 * 86400000).toISOString();
  const yesterday = new Date(Date.now() - 86400000).toISOString();
  await start(env, overview([task(1), task(2, {due_at: tomorrow}), task(3, {status: 'done'}), task(4, {due_at: yesterday})], space(), 50));
  assert.equal(env.e('workTaskList').children.length, 2); assert.equal(env.e('workTodayCount').textContent, 1);
  assert.equal(env.e('workListBoundary').hidden, false); assert.match(env.e('workListSummary').textContent, /1 项今日.*1 项待安排/);
  await env.filters[2].fire('click'); assert.equal(env.e('workTaskList').children.length, 1);
  assert.equal(env.e('workTaskList').children[0].dataset.status, 'done');
  await env.filters[1].fire('click'); assert.equal(env.e('workTaskList').children.length, 4);
}
async function conflictKeepsDraftAndRequiresRead() {
  const env = environment(); await start(env, overview([task()]));
  const opened = env.app.openTask(1); env.requests[1].resolve(success({task: task(), history: []})); await opened;
  env.e('workEditTitle').value = '本地未保存内容';
  const patch = env.app.patchTask(task(), {title: '本地未保存内容'});
  assert.equal(JSON.parse(env.requests[2].options.body).expected_version, 1);
  env.requests[2].resolve(failure(409)); await patch;
  assert.equal(env.e('workEditTitle').value, '本地未保存内容'); assert(env.app.state.patchUnknown.has('1'));
  assert.equal(env.e('workSaveDetail').disabled, true);
  await env.app.patchTask(task(), {status: 'done'}); assert.equal(env.requests.length, 3);
  const reread = env.app.openTask(1); env.requests[3].resolve(success({task: task(1, {title: '另一端保存', version: 2}), history: []})); await reread;
  assert.equal(env.e('workEditTitle').value, '另一端保存'); assert.equal(env.e('workSaveDetail').disabled, false);
}
async function timeoutDoesNotClaimUpdateOrRetry() {
  const env = environment(); await start(env, overview([task()]));
  const patch = env.app.patchTask(task(), {status: 'done'}); env.requests[1].reject(new Error('transport')); await patch;
  assert.equal(env.app.state.tasks.get('1').status, 'todo'); assert.equal(env.requests.length, 2);
  assert(env.app.state.patchUnknown.has('1')); assert.match(env.e('workNotice').textContent, /结果尚未确认/);
}
async function reverseDetailsCannotChangeSelection() {
  const env = environment(); await start(env, overview([task(1), task(2)]));
  const first = env.app.openTask(1), second = env.app.openTask(2);
  env.requests[2].resolve(success({task: task(2, {title: '第二条'}), history: []})); await second;
  env.requests[1].resolve(success({task: task(1, {title: '迟到的第一条'}), history: []})); await first;
  assert.equal(env.e('workEditTitle').value, '第二条'); assert.equal(env.app.state.selected, 2);
}
async function staleListCannotUndoConfirmedMutation() {
  const env = environment(); await start(env, overview([task()]));
  const patch = env.app.patchTask(task(), {status: 'done'});
  const oldRead = env.app.loadOverview();
  env.requests[1].resolve(success({task: task(1, {status: 'done', version: 2})})); await patch;
  env.requests[2].resolve(success(overview([task()]))); await oldRead;
  assert.equal(env.app.state.tasks.get('1').status, 'done'); assert.equal(env.app.state.tasks.get('1').version, 2);
}
async function lateOwnerResponseAndAuthFailurePurgePrivateData() {
  const env = environment(); await start(env, overview([task()]));
  const detail = env.app.openTask(1); const reload = env.app.loadOverview();
  env.requests[2].resolve(success(overview([], space('space-owner-B')))); await reload;
  env.requests[1].resolve(success({task: task(), history: []})); await detail;
  assert.equal(env.e('workDetail').hidden, true); assert.equal(env.app.state.tasks.size, 0);
  env.e('workTitle').value = '私有草稿'; const failedRead = env.app.loadOverview(); env.requests[3].resolve(failure(401)); await failedRead;
  assert.equal(env.e('workTitle').value, ''); assert.equal(env.app.state.space, null); assert.equal(env.e('workContent').hidden, true);
  assert.equal(env.e('workAllCount').textContent, '');
}
async function paginationIsReadOnlyAndUsesServerCursor() {
  const env = environment(); await start(env, overview([task(51)], space(), 51));
  const more = env.app.loadMore(); assert.equal(env.requests[1].url, '/api/work/spaces/space-owner-A/tasks?limit=50&before=51');
  assert.equal(env.requests[1].options.method, 'GET'); env.requests[1].resolve(success({tasks: [task(50)], next_cursor: null})); await more;
  assert.equal(env.app.state.tasks.size, 2); assert.equal(env.e('workLoadMore').hidden, true);
}
async function invalidSuccessfulWriteKeepsId() {
  const env = environment(); await start(env); env.e('workTitle').value = '结果不能解析';
  const pending = env.app.createTask(); env.requests[1].resolve(success({})); await pending;
  assert(env.app.state.pendingCreate); assert.match(env.e('workNotice').textContent, /结果尚未确认/);
}
async function unsavedDetailCanStayOpen() {
  const env = environment(); await start(env, overview([task()]));
  const opened = env.app.openTask(1); env.requests[1].resolve(success({task: task(), history: []})); await opened;
  env.e('workEditTitle').value = '尚未保存的详情'; env.context.confirm = () => false;
  await env.e('workCloseDetail').fire('click');
  assert.equal(env.e('workDetail').hidden, false); assert.equal(env.e('workEditTitle').value, '尚未保存的详情');
  await env.app.openTask(2); assert.equal(env.requests.length, 2);
  let prevented = false;
  env.windowEvents.beforeunload({preventDefault() { prevented = true; }}); assert(prevented);
}
async function listToggleCannotDiscardDetailDraft() {
  const env = environment(); await start(env, overview([task()]));
  const opened = env.app.openTask(1); env.requests[1].resolve(success({task: task(), history: []})); await opened;
  env.e('workEditDescription').value = '仍在写的说明';
  const patch = env.app.patchTask(task(), {status: 'done'});
  assert.equal(env.e('workEditDescription').disabled, true);
  env.requests[2].resolve(success({task: task(1, {status: 'done', version: 2})})); await patch;
  assert.equal(env.app.state.tasks.get('1').status, 'done');
  assert.equal(env.e('workEditDescription').value, '仍在写的说明'); assert.equal(env.app.state.detailNeedsRead, true);
  assert.equal(env.requests.length, 3, 'a background read must not silently discard unsaved details');
}
async function foregroundAccessLossClearsPrivateWorkspace() {
  const env = environment(); await start(env, overview([task(1, {title: '账号A私有事项'})]));
  env.e('workTitle').value = '账号A尚未保存的草稿';
  const update = env.app.patchTask(task(), {status: 'done'});
  env.requests[1].resolve(failure(404)); await update;
  assert.equal(env.e('workContent').hidden, true, 'a stale account window must not keep drawing prior private data');
  assert.equal(env.app.state.space, null); assert.equal(env.app.state.tasks.size, 0);
  assert.equal(env.e('workTitle').value, ''); assert.equal(env.e('workTaskList').children.length, 0);
  assert.equal(env.e('workTodayCount').hidden, true);
  await env.app.createTask(); assert.equal(env.requests.length, 2, 'writes stay blocked until a new trusted overview');
}
async function backgroundRestoreRequiresTrustedRead() {
  const env = environment(); await start(env, overview([task()]));
  const detail = env.app.openTask(1); env.requests[1].resolve(success({task: task(), history: []})); await detail;
  env.e('workTitle').value = '本账号创建草稿'; env.e('workEditDescription').value = '本账号详情草稿';
  env.windowEvents.pagehide({});
  assert.equal(env.e('workContent').hidden, true); assert.equal(env.e('workDetail').hidden, true);
  env.windowEvents.pageshow({}); env.windowEvents.focus({}); env.context.document.listeners.visibilitychange({});
  assert.equal(env.requests.length, 3, 'concurrent resume signals share one authentication read');
  assert.equal(env.e('workContent').hidden, true, 'old content stays concealed while the server is pending');
  env.requests[2].resolve(success(overview([task()]))); await tick();
  assert.equal(env.e('workContent').hidden, false); assert.equal(env.e('workDetail').hidden, false);
  assert.equal(env.e('workTitle').value, '本账号创建草稿'); assert.equal(env.e('workEditDescription').value, '本账号详情草稿');
}
async function restoredDifferentAccountLosesOldDrafts() {
  const env = environment(); await start(env, overview([task(1, {title: '私有A'})])); env.e('workTitle').value = '私有A草稿';
  env.context.document.hidden = true; env.context.document.listeners.visibilitychange({});
  assert.equal(env.e('workContent').hidden, true);
  env.context.document.hidden = false; env.context.document.listeners.visibilitychange({});
  assert.equal(env.requests.length, 2); assert.equal(env.e('workContent').hidden, true);
  env.requests[1].resolve(success(overview([task(2, {workspace_id: 'space-owner-B', title: '私有B'})], space('space-owner-B')))); await tick();
  assert.equal(env.e('workTitle').value, ''); assert.equal(env.app.state.space.id, 'space-owner-B');
  assert(!text(env.e('workTaskList')).includes('私有A')); assert(text(env.e('workTaskList')).includes('私有B'));
}
async function failedResumeReadNeverRevealsOldContent() {
  const env = environment(); await start(env, overview([task()])); env.e('workTitle').value = '隐藏中的草稿';
  env.windowEvents.pagehide({}); env.windowEvents.pageshow({});
  env.requests[1].resolve(failure(503)); await tick();
  assert.equal(env.e('workContent').hidden, true); assert.equal(env.e('workDetail').hidden, true);
  assert.equal(env.e('workTitle').value, '隐藏中的草稿', 'same-account draft can remain only in hidden memory until verified');
  await env.app.createTask(); assert.equal(env.requests.length, 2);
  const reread = env.app.loadOverview(); env.requests[2].resolve(success(overview([task()]))); await reread;
  assert.equal(env.e('workContent').hidden, false); assert.equal(env.e('workTitle').value, '隐藏中的草稿');
}
async function preBackgroundResponseCannotVerifyNewContext() {
  const env = environment(); await start(env, overview([task(1, {title: '旧账号A'})]));
  const oldRead = env.app.loadOverview();
  env.windowEvents.pagehide({}); env.windowEvents.pageshow({});
  assert.equal(env.requests.length, 3, 'a read sent before backgrounding cannot satisfy a new context');
  env.requests[1].resolve(success(overview([task(1, {title: '迟到的旧账号A'})]))); await oldRead;
  assert.equal(env.e('workContent').hidden, true);
  env.requests[2].resolve(success(overview([task(2, {workspace_id: 'space-owner-B', title: '账号B'})], space('space-owner-B')))); await tick();
  assert.equal(env.e('workContent').hidden, false); assert(!text(env.e('workTaskList')).includes('账号A'));
}
async function hiddenInFlightWriteCannotRevealOldOwner() {
  const env = environment(); await start(env, overview([task()])); env.e('workTitle').value = '旧账号创建中';
  const oldWrite = env.app.createTask();
  env.windowEvents.pagehide({}); env.windowEvents.pageshow({});
  env.requests[2].resolve(success(overview([], space('space-owner-B')))); await tick();
  env.e('workTitle').value = '新账号创建中'; const newWrite = env.app.createTask();
  env.requests[1].resolve(success({task: task(8), replayed: false})); await oldWrite;
  assert.equal(env.app.state.createBusy, true, 'a prior account response cannot unlock a new account write');
  assert.equal(env.e('workTitle').value, '新账号创建中'); assert.equal(env.app.state.tasks.size, 0);
  env.requests[3].resolve(success({task: task(9, {workspace_id: 'space-owner-B'}), replayed: false})); await newWrite;
  assert.equal(env.app.state.tasks.size, 1); assert.equal(env.app.state.tasks.get('9').workspace_id, 'space-owner-B');
}

(async () => {
  const cases = [initialReadIsPrivateAndReadOnly, explicitEnableAndUnknownReplay, failedCreationKeepsIdAndPayload,
    validationRejectionKeepsEditableDraft, doubleClickCannotWriteTwice, dstUsesActualZoneAndRequiresChoice,
    privateContentIsAlwaysText, filtersAndBoundedCountsAreHonest, conflictKeepsDraftAndRequiresRead,
    timeoutDoesNotClaimUpdateOrRetry, reverseDetailsCannotChangeSelection, staleListCannotUndoConfirmedMutation,
    lateOwnerResponseAndAuthFailurePurgePrivateData, paginationIsReadOnlyAndUsesServerCursor, invalidSuccessfulWriteKeepsId,
    unsavedDetailCanStayOpen, listToggleCannotDiscardDetailDraft, foregroundAccessLossClearsPrivateWorkspace,
    backgroundRestoreRequiresTrustedRead, restoredDifferentAccountLosesOldDrafts, failedResumeReadNeverRevealsOldContent,
    preBackgroundResponseCannotVerifyNewContext, hiddenInFlightWriteCannotRevealOldOwner];
  for (const test of cases) await test();
  console.log('work frontend runtime ok: ' + cases.length + ' scenarios');
})().catch(error => { console.error(error); process.exitCode = 1; });
