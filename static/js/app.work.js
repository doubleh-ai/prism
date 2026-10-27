/* Work personal tasks. No model calls, external requests or private browser cache. */
(function () {
  'use strict';
  var STATUS = {todo: '待开始', doing: '进行中', done: '已完成', cancelled: '已取消'};
  var PRIORITY = {low: '较低', normal: '普通', high: '重要'};
  var FIELDS = {title: '名称', description: '说明', status: '状态', priority: '优先级', due_at: '截止时间', timezone: '时区'};
  var state = {
    space: null, tasks: new Map(), cursor: null, filter: 'today', loading: false,
    loadSeq: 0, detailSeq: 0, selected: null, detail: null,
    pendingCreate: null, pendingSpace: null, createBusy: false, spaceBusy: false,
    patchBusy: new Set(), patchUnknown: new Set(), detailNeedsRead: false, dueCandidates: [], writeEpoch: 0, changedAt: new Map(),
    verified: false, concealed: true, contextEpoch: 0, accountEpoch: 0
  };
  var elements = {};
  var activeOverview = null;
  var CHECKING = '正在核对当前账号的工作区…';

  function byId(id) { return document.getElementById(id); }
  function node(tag, className, text) {
    var value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = text;
    return value;
  }
  function notice(message, kind) {
    elements.notice.textContent = message;
    elements.notice.dataset.kind = kind || 'warning';
    elements.notice.hidden = !message;
  }
  function validZone(zone) {
    try { new Intl.DateTimeFormat('en', {timeZone: zone}).format(0); return true; }
    catch (_) { return false; }
  }
  function browserZone() {
    var zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone || !validZone(zone)) throw new Error('timezone_unavailable');
    return zone;
  }
  function zone() { return state.space ? state.space.timezone : browserZone(); }
  function stampParts(stamp, timeZone) {
    var parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'
    }).formatToParts(new Date(stamp));
    var result = {};
    parts.forEach(function (part) { if (part.type !== 'literal') result[part.type] = Number(part.value); });
    return result;
  }
  function dateKey(stamp, timeZone) {
    var parts = stampParts(stamp, timeZone);
    return String(parts.year).padStart(4, '0') + '-' + String(parts.month).padStart(2, '0') + '-' + String(parts.day).padStart(2, '0');
  }
  function milliseconds(value) {
    if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
    return Date.parse(value);
  }
  function formatTime(value, includeDate) {
    if (value === null || value === undefined) return '未设截止时间';
    var stamp = milliseconds(value);
    if (!Number.isFinite(stamp)) return '时间不可用';
    var options = {timeZone: zone(), hour: '2-digit', minute: '2-digit', hourCycle: 'h23'};
    if (includeDate) { options.year = 'numeric'; options.month = 'short'; options.day = 'numeric'; }
    return new Intl.DateTimeFormat('zh-CN', options).format(new Date(stamp));
  }
  function offsetLabel(minutes) {
    var absolute = Math.abs(minutes);
    return 'UTC' + (minutes >= 0 ? '+' : '−') + String(Math.floor(absolute / 60)).padStart(2, '0') + ':' + String(absolute % 60).padStart(2, '0');
  }
  function possibleInstants(local, timeZone) {
    if (!validZone(timeZone)) throw new Error('timezone_unavailable');
    var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local);
    if (!match) throw new Error('invalid_local_time');
    var target = match.slice(1).map(Number);
    if (target[0] < 1900 || target[0] > 9999 || target[1] < 1 || target[1] > 12 || target[2] < 1 || target[2] > 31 || target[3] > 23 || target[4] > 59) throw new Error('invalid_local_time');
    var wall = Date.UTC(target[0], target[1] - 1, target[2], target[3], target[4]);
    var check = new Date(wall);
    if (check.getUTCFullYear() !== target[0] || check.getUTCMonth() + 1 !== target[1] || check.getUTCDate() !== target[2]) throw new Error('invalid_local_time');
    var offsets = new Set();
    // Derive nearby IANA offsets from actual zone data, never Date's local-time guessing.
    for (var hour = -48; hour <= 48; hour += 6) {
      var sample = wall + hour * 3600000;
      var parts = stampParts(sample, timeZone);
      offsets.add(Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second) - sample);
    }
    var found = [];
    offsets.forEach(function (offset) {
      var instant = wall - offset;
      var parts = stampParts(instant, timeZone);
      if (parts.year === target[0] && parts.month === target[1] && parts.day === target[2] && parts.hour === target[3] && parts.minute === target[4] && parts.second === 0) {
        found.push({iso: new Date(instant).toISOString(), offset: offset / 60000});
      }
    });
    return found.sort(function (a, b) { return a.iso.localeCompare(b.iso); });
  }
  function updateDue() {
    var previousChoice = elements.dueChoice.value;
    elements.dueChoice.replaceChildren(node('option', '', '请选择一次'));
    elements.dueChoice.firstChild.value = '';
    elements.dueChoiceLabel.hidden = true;
    state.dueCandidates = [];
    elements.due.setCustomValidity('');
    if (!elements.due.value) {
      elements.dueHint.textContent = '截止时间按 ' + zone() + ' 解释，不设置则留空。';
      return;
    }
    try {
      state.dueCandidates = possibleInstants(elements.due.value, zone());
      if (!state.dueCandidates.length) throw new Error('nonexistent_local_time');
      if (state.dueCandidates.length > 2) throw new Error('ambiguous_local_time');
      if (state.dueCandidates.length === 2) {
        state.dueCandidates.forEach(function (candidate, index) {
          var option = node('option', '', (index ? '再次出现' : '首次出现') + '（' + offsetLabel(candidate.offset) + '）');
          option.value = candidate.iso;
          elements.dueChoice.appendChild(option);
        });
        if (state.dueCandidates.some(function (candidate) { return candidate.iso === previousChoice; })) elements.dueChoice.value = previousChoice;
        elements.dueChoiceLabel.hidden = false;
        elements.dueHint.textContent = zone() + ' 在这一天调整时钟，请明确选择第一次或第二次出现的时间。';
      } else {
        elements.dueHint.textContent = '将保存为 ' + elements.due.value.replace('T', ' ') + ' · ' + zone() + '（' + offsetLabel(state.dueCandidates[0].offset) + '）。';
      }
    } catch (_) {
      elements.due.setCustomValidity('这个时间不存在或无法可靠解释，请选择另一个时间。');
      elements.dueHint.textContent = '该时区的这个时间不存在或无法可靠解释，请重新选择。';
    }
  }
  function dueValue() {
    if (!elements.due.value) return null;
    if (state.dueCandidates.length === 1) return state.dueCandidates[0].iso;
    var selected = state.dueCandidates.find(function (candidate) { return candidate.iso === elements.dueChoice.value; });
    if (selected) return selected.iso;
    throw new Error('choose_due_occurrence');
  }
  function requestId() {
    if (!window.crypto || typeof window.crypto.randomUUID !== 'function') throw new Error('secure_context_required');
    return window.crypto.randomUUID();
  }
  function apiError(status, code, unknown) {
    var error = new Error(code);
    error.status = status;
    error.code = code;
    error.unknown = !!unknown;
    return error;
  }
  async function api(path, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 30000);
    var method = options && options.method || 'GET';
    try {
      var response = await fetch(path, {
        method: method, credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: method === 'GET' ? {Accept: 'application/json'} : {'Content-Type': 'application/json', Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
        body: options ? JSON.stringify(options.body) : undefined
      });
      if (!response.ok) throw apiError(response.status, response.status === 409 ? 'conflict' : 'request_rejected', method !== 'GET' && (response.status >= 500 || response.status === 408));
      try { return await response.json(); }
      catch (_) { throw apiError(response.status, 'invalid_response', method !== 'GET'); }
    } catch (error) {
      if (error.status) throw error;
      throw apiError(0, 'connection_unknown', method !== 'GET');
    } finally { clearTimeout(timer); }
  }
  function spacePath(id) { return '/api/work/spaces/' + encodeURIComponent(id); }
  function taskPath(spaceId, taskId) { return spacePath(spaceId) + '/tasks/' + encodeURIComponent(taskId); }
  function sameSpace(id) { return state.space && state.space.id === id; }
  function fail(error, action) {
    if (error.status === 401 || error.status === 403) {
      clearPrivateState();
      notice('登录已失效或当前账号无权访问，请重新登录后打开工作台。');
      return;
    }
    if (error.status === 404) {
      clearPrivateState();
      notice('当前工作区访问已失效，旧内容已清除。请点击“核对工作区”只读核对当前账号。');
    }
    else if (error.status === 409) notice('记录已变化或请求发生冲突，请读取最新版本后再决定。原输入暂时保留。');
    else if (error.unknown) notice(action + '的结果尚未确认。请核对后再操作，系统不会自动重试。');
    else if (error.status === 400 || error.status === 422) notice('输入未通过检查，请检查名称、长度和截止时间后再保存。');
    else notice('暂时无法' + action + '，请稍后手动刷新。当前未保存的内容仍保留在此页面。');
  }
  function clearPrivateState() {
    state.loadSeq++; state.detailSeq++;
    state.contextEpoch++; state.accountEpoch++;
    state.verified = false; state.concealed = true;
    state.space = null; state.tasks.clear(); state.cursor = null; state.selected = null; state.detail = null;
    state.loading = false;
    state.pendingCreate = null; state.pendingSpace = null; state.patchUnknown.clear();
    state.patchBusy.clear(); state.createBusy = false; state.spaceBusy = false;
    state.dueCandidates = [];
    state.changedAt.clear();
    elements.createForm.reset(); elements.detailForm.reset(); elements.list.replaceChildren(); elements.history.replaceChildren();
    elements.content.hidden = true; elements.welcome.hidden = true; elements.detail.hidden = true;
    elements.detailState.textContent = ''; elements.detailDue.textContent = '';
    elements.dueHint.textContent = ''; elements.dueChoice.replaceChildren();
    elements.todayCount.textContent = ''; elements.allCount.textContent = ''; elements.doneCount.textContent = '';
    elements.app.dataset.detailOpen = 'false';
    notice('');
    updateControls();
  }
  function concealPrivateUI(invalidate) {
    state.verified = false; state.concealed = true;
    if (invalidate) {
      state.contextEpoch++; state.loadSeq++; state.detailSeq++;
      state.loading = false;
    }
    updateControls();
  }
  function adoptTask(task, changed) {
    var existing = state.tasks.get(String(task.id));
    if (!existing || existing.version <= task.version) state.tasks.set(String(task.id), task);
    if (changed === true) state.changedAt.set(String(task.id), ++state.writeEpoch);
  }
  function updateControls() {
    var visible = state.verified && !state.concealed;
    elements.content.hidden = !visible || !state.space;
    elements.welcome.hidden = !visible || !!state.space;
    elements.detail.hidden = !visible || !state.space || state.selected === null;
    elements.app.dataset.detailOpen = String(!elements.detail.hidden);
    elements.timezone.hidden = !visible;
    ['todayCount', 'allCount', 'doneCount'].forEach(function (name) { elements[name].hidden = !visible; });
    var locked = state.createBusy || !!state.pendingCreate;
    ['title', 'description', 'priority', 'due', 'dueChoice'].forEach(function (name) { elements[name].disabled = locked; });
    elements.create.disabled = !visible || state.createBusy;
    elements.create.textContent = state.createBusy ? '正在保存…' : state.pendingCreate ? '核对并重试同一任务' : '添加任务 ↗';
    elements.enable.disabled = !visible || state.spaceBusy;
    elements.enable.textContent = state.spaceBusy ? '正在启用…' : state.pendingSpace ? '核对并重试启用' : '启用我的工作台';
    elements.refresh.disabled = state.loading;
    elements.refresh.textContent = visible ? '刷新' : '核对工作区';
    elements.loadMore.disabled = !visible || state.loading;
    var selectedBusy = state.selected !== null && state.patchBusy.has(String(state.selected));
    ['editTitle', 'editDescription', 'editStatus', 'editPriority'].forEach(function (name) { elements[name].disabled = selectedBusy; });
    elements.saveDetail.disabled = !visible || selectedBusy || state.detailNeedsRead;
    elements.reloadDetail.hidden = !state.detailNeedsRead;
  }
  function taskActive(task) { return task.status === 'todo' || task.status === 'doing'; }
  function todayTask(task) {
    return taskActive(task) && (task.status === 'doing' || (task.due_at && dateKey(milliseconds(task.due_at), zone()) <= dateKey(Date.now(), zone())));
  }
  function visibleTasks() {
    return Array.from(state.tasks.values()).filter(function (task) {
      if (state.filter === 'done') return task.status === 'done';
      if (state.filter === 'all') return true;
      return todayTask(task) || (taskActive(task) && !task.due_at);
    });
  }
  function renderTasks() {
    var all = Array.from(state.tasks.values());
    var today = all.filter(todayTask).length;
    var unscheduled = all.filter(function (task) { return taskActive(task) && !task.due_at && !todayTask(task); }).length;
    elements.todayCount.textContent = today || '';
    elements.allCount.textContent = all.length || '';
    elements.doneCount.textContent = all.filter(function (task) { return task.status === 'done'; }).length || '';
    elements.list.replaceChildren();
    var visible = visibleTasks();
    var heading = state.filter === 'today' ? '今天，专注重要的事。' : state.filter === 'done' ? '每一步，都有记录。' : '把事情，逐一理清。';
    elements.viewTitle.textContent = heading;
    elements.listTitle.textContent = state.filter === 'today' ? '今日与待安排' : state.filter === 'done' ? '已完成的任务' : '全部任务';
    elements.listSummary.textContent = state.filter === 'today' ? today + ' 项今日／逾期 · ' + unscheduled + ' 项待安排' : '已加载 ' + all.length + ' 项';
    visible.forEach(function (task) {
      var row = node('article', 'task-row');
      row.dataset.status = task.status;
      var toggle = node('button', 'task-toggle', task.status === 'done' ? '✓' : '');
      toggle.type = 'button'; toggle.dataset.done = String(task.status === 'done');
      toggle.setAttribute('aria-label', (task.status === 'done' ? '重新打开任务：' : '完成任务：') + task.title);
      toggle.disabled = state.patchBusy.has(String(task.id)) || state.patchUnknown.has(String(task.id)) || task.status === 'cancelled';
      toggle.addEventListener('click', function () { patchTask(task, {status: task.status === 'done' ? 'todo' : 'done'}); });
      var open = node('button', 'task-open'); open.type = 'button';
      open.appendChild(node('span', 'task-name', task.title));
      if (task.description) open.appendChild(node('span', 'task-description', task.description));
      var meta = node('span', 'task-meta');
      meta.appendChild(node('span', '', STATUS[task.status] || '状态未知'));
      var date = node('span', '', task.due_at ? formatTime(task.due_at, true) : '待安排 · 未设截止');
      date.dataset.overdue = String(taskActive(task) && task.due_at && milliseconds(task.due_at) < Date.now());
      meta.appendChild(date);
      if (state.patchUnknown.has(String(task.id))) meta.appendChild(node('span', '', '上次保存待核对，打开详情读取最新状态'));
      open.appendChild(meta);
      open.addEventListener('click', function () { openTask(task.id); });
      row.appendChild(toggle); row.appendChild(open);
      var priority = node('span', 'task-priority', PRIORITY[task.priority] || ''); priority.dataset.priority = task.priority;
      row.appendChild(priority); elements.list.appendChild(row);
    });
    elements.empty.hidden = visible.length !== 0;
    elements.empty.textContent = state.filter === 'done' ? '还没有已完成的任务。完成后会保留在这里。' : state.filter === 'today' ? '今天没有到期或待安排任务。你可以记录一件事，或到全部任务查看后续安排。' : '还没有任务。从上面记下一件事开始。';
    elements.loadMore.hidden = state.cursor === null;
    elements.listBoundary.hidden = state.cursor === null;
    document.querySelectorAll('[data-work-filter]').forEach(function (button) {
      if (button.dataset.workFilter === state.filter) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    updateControls();
  }
  function validateOverview(data) {
    if (!data || !Array.isArray(data.tasks) || !Object.prototype.hasOwnProperty.call(data, 'workspace')) throw apiError(0, 'invalid_response', false);
    if (data.workspace && (!data.workspace.id || !validZone(data.workspace.timezone))) throw apiError(0, 'invalid_response', false);
    if (data.tasks.length > 50 || (!data.workspace && data.tasks.length) || data.tasks.some(function (task) { return !task || task.workspace_id !== data.workspace.id; })) throw apiError(0, 'invalid_response', false);
    return data;
  }
  function loadOverview() {
    concealPrivateUI(false);
    if (document.hidden) return Promise.resolve();
    if (activeOverview && activeOverview.contextEpoch === state.contextEpoch) return activeOverview.promise;
    var seq = ++state.loadSeq, writeEpoch = state.writeEpoch;
    state.loading = true; updateControls();
    notice(CHECKING, 'success');
    var marker = {contextEpoch: state.contextEpoch, promise: null};
    activeOverview = marker;
    marker.promise = readOverview(seq, writeEpoch).finally(function () {
      if (activeOverview === marker) activeOverview = null;
    });
    return marker.promise;
  }
  async function readOverview(seq, writeEpoch) {
    try {
      var data = validateOverview(await api('/api/work/overview?limit=50'));
      if (seq !== state.loadSeq) return;
      if (document.hidden) { concealPrivateUI(true); return; }
      if (state.space && (!data.workspace || data.workspace.id !== state.space.id)) {
        clearPrivateState(); seq = state.loadSeq;
      }
      state.space = data.workspace;
      var previous = state.tasks;
      state.tasks = new Map(data.tasks.map(function (task) {
        var existing = previous.get(String(task.id));
        return [String(task.id), existing && existing.version > task.version ? existing : task];
      }));
      previous.forEach(function (task, id) {
        if (!state.tasks.has(id) && state.changedAt.get(id) > writeEpoch) state.tasks.set(id, task);
      });
      state.cursor = data.next_cursor === undefined ? null : data.next_cursor;
      state.verified = true; state.concealed = false;
      elements.timezone.textContent = '工作区时区：' + zone();
      elements.date.textContent = new Intl.DateTimeFormat('zh-CN', {timeZone: zone(), month: 'long', day: 'numeric', weekday: 'long'}).format(new Date());
      updateDue(); renderTasks();
      if (elements.notice.textContent === CHECKING) notice('');
    } catch (error) { if (seq === state.loadSeq) fail(error, '读取任务'); }
    finally { if (seq === state.loadSeq) { state.loading = false; updateControls(); } }
  }
  async function loadMore() {
    if (!state.verified || state.concealed || state.loading || !state.space || state.cursor === null) return;
    var sid = state.space.id, seq = ++state.loadSeq;
    state.loading = true; updateControls();
    try {
      var data = await api(spacePath(sid) + '/tasks?limit=50&before=' + encodeURIComponent(state.cursor));
      if (seq !== state.loadSeq || !sameSpace(sid)) return;
      data.tasks.forEach(adoptTask);
      state.cursor = data.next_cursor === undefined ? null : data.next_cursor;
      renderTasks();
    } catch (error) { if (seq === state.loadSeq && sameSpace(sid)) fail(error, '加载更多任务'); }
    finally { if (seq === state.loadSeq) { state.loading = false; updateControls(); } }
  }
  async function enableSpace() {
    if (!state.verified || state.concealed || state.spaceBusy || state.space) return;
    var epoch = state.accountEpoch;
    try {
      if (!state.pendingSpace) state.pendingSpace = {request_id: requestId(), name: '我的工作', timezone: browserZone()};
    } catch (_) { notice('当前浏览器无法安全创建请求或识别时区，请使用支持 HTTPS 的现代浏览器。'); return; }
    state.spaceBusy = true; updateControls();
    try {
      var data = await api('/api/work/spaces', {method: 'POST', body: state.pendingSpace});
      if (epoch !== state.accountEpoch) return;
      if (!data.workspace) throw apiError(200, 'invalid_response', true);
      state.pendingSpace = null;
      await loadOverview();
      if (epoch === state.accountEpoch && state.space && state.space.id === data.workspace.id) { notice('工作台已启用。记下第一件需要完成的事吧。', 'success'); elements.title.focus(); }
    } catch (error) {
      if (epoch !== state.accountEpoch) return;
      if (!error.unknown && error.status !== 409) state.pendingSpace = null;
      fail(error, '启用工作台');
    } finally { if (epoch === state.accountEpoch) { state.spaceBusy = false; updateControls(); } }
  }
  async function createTask(event) {
    if (event) event.preventDefault();
    if (!state.verified || state.concealed || state.createBusy || !state.space) return;
    var sid = state.space.id, epoch = state.accountEpoch;
    try {
      if (!state.pendingCreate) {
        if (!elements.createForm.reportValidity()) return;
        var title = elements.title.value.trim();
        if (!title) { notice('请先写下任务名称。'); elements.title.focus(); return; }
        state.pendingCreate = {request_id: requestId(), title: title, description: elements.description.value.trim(), status: 'todo', priority: elements.priority.value, due_at: dueValue(), timezone: zone()};
      }
    } catch (_) { notice('请检查截止时间；遇到时钟调整时，需要明确选择第一次或第二次出现的时间。'); return; }
    state.createBusy = true; updateControls();
    try {
      var data = await api(spacePath(sid) + '/tasks', {method: 'POST', body: state.pendingCreate});
      if (!sameSpace(sid)) return;
      if (!data.task) throw apiError(200, 'invalid_response', true);
      adoptTask(data.task, true);
      state.pendingCreate = null; elements.createForm.reset(); updateDue(); renderTasks();
      notice(data.replayed ? '已核对：同一任务已保存，没有重复创建。' : '任务已保存到服务器。', 'success');
      elements.title.focus();
      if (data.replayed) {
        try {
          var current = await api(taskPath(sid, data.task.id));
          if (sameSpace(sid) && current.task) { adoptTask(current.task); renderTasks(); }
        } catch (readError) {
          if (sameSpace(sid)) {
            if (readError.status === 401 || readError.status === 403) fail(readError, '核对任务');
            else notice('任务创建已确认；当前进度还需手动刷新核对，没有重复创建。');
          }
        }
      }
    } catch (error) {
      if (!sameSpace(sid)) return;
      if (!error.unknown && error.status !== 409) state.pendingCreate = null;
      fail(error, '创建任务');
    } finally { if (epoch === state.accountEpoch) { state.createBusy = false; updateControls(); } }
  }
  function fillDetail(task, history) {
    state.detail = task; state.detailNeedsRead = false;
    elements.editTitle.value = task.title;
    elements.editDescription.value = task.description;
    elements.editStatus.value = task.status; elements.editPriority.value = task.priority;
    elements.detailState.textContent = '服务器版本 ' + task.version + ' · 更新于 ' + formatTime(task.updated_at, true);
    elements.detailDue.textContent = task.due_at ? '截止：' + formatTime(task.due_at, true) + ' · ' + zone() : '未设置截止时间';
    elements.history.replaceChildren();
    history.forEach(function (item) {
      var row = node('li');
      row.appendChild(node('time', '', formatTime(item.created_at, true)));
      var changed = Array.isArray(item.changed_fields) ? item.changed_fields.map(function (field) { return FIELDS[field]; }).filter(Boolean).join('、') : '';
      row.appendChild(node('span', '', (item.action === 'task_created' ? '创建任务' : '更新' + (changed ? '：' + changed : '任务')) + ' · 版本 ' + item.version));
      elements.history.appendChild(row);
    });
    updateControls();
  }
  function detailDirty() {
    return !!state.detail && (elements.editTitle.value !== state.detail.title || elements.editDescription.value !== state.detail.description || elements.editStatus.value !== state.detail.status || elements.editPriority.value !== state.detail.priority);
  }
  async function openTask(id, discardDraft) {
    if (!state.verified || state.concealed || !state.space) return;
    if (!discardDraft && detailDirty() && !window.confirm('打开任务会替换详情中的未保存输入。继续吗？')) return;
    var sid = state.space.id, seq = ++state.detailSeq;
    state.selected = id; state.detail = null; state.detailNeedsRead = true;
    elements.detail.hidden = false; elements.app.dataset.detailOpen = 'true';
    elements.detailState.textContent = '正在读取服务器记录…'; elements.detailForm.reset(); elements.history.replaceChildren(); updateControls();
    try {
      var data = await api(taskPath(sid, id));
      if (seq !== state.detailSeq || !sameSpace(sid) || String(state.selected) !== String(id)) return;
      if (!data.task || !Array.isArray(data.history)) throw apiError(0, 'invalid_response', false);
      state.patchUnknown.delete(String(id)); adoptTask(data.task);
      fillDetail(data.task, data.history); renderTasks();
      elements.detail.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    } catch (error) {
      if (seq !== state.detailSeq || !sameSpace(sid)) return;
      elements.detailState.textContent = '尚未读取到最新记录。请手动重试。'; fail(error, '读取详情'); updateControls();
    }
  }
  async function patchTask(task, changes, fromDetail) {
    if (!state.verified || state.concealed || !state.space || state.patchBusy.has(String(task.id)) || state.patchUnknown.has(String(task.id))) return;
    var sid = state.space.id, key = String(task.id), selectedSeq = state.detailSeq, epoch = state.accountEpoch;
    state.patchBusy.add(key); renderTasks();
    try {
      var data = await api(taskPath(sid, task.id), {method: 'PATCH', body: Object.assign({expected_version: task.version}, changes)});
      if (!sameSpace(sid)) return;
      if (!data.task) throw apiError(200, 'invalid_response', true);
      adoptTask(data.task, true); renderTasks();
      if (String(state.selected) === key && selectedSeq === state.detailSeq) {
        if (!fromDetail && detailDirty()) {
          state.detailNeedsRead = true;
          elements.detailState.textContent = '任务状态已保存；详情中的其他输入尚未保存。请先读取最新版本再继续编辑。';
        } else await openTask(task.id, true);
      }
      if (sameSpace(sid) && epoch === state.accountEpoch) notice('更改已保存到服务器。', 'success');
    } catch (error) {
      if (!sameSpace(sid)) return;
      if (error.unknown || error.status === 409) {
        state.patchUnknown.add(key);
        if (String(state.selected) === key) {
          state.detailNeedsRead = true;
          elements.detailState.textContent = '更改结果或版本待核对。原输入保留；读取最新版本将替换此处输入。';
        }
      }
      fail(error, '保存更改');
    } finally { if (epoch === state.accountEpoch) { state.patchBusy.delete(key); renderTasks(); } }
  }
  function saveDetail(event) {
    event.preventDefault();
    if (!state.verified || state.concealed || !state.detail || state.detailNeedsRead || !elements.detailForm.reportValidity()) return;
    var title = elements.editTitle.value.trim();
    if (!title) { notice('任务名称不能为空。'); return; }
    var values = {title: title, description: elements.editDescription.value.trim(), status: elements.editStatus.value, priority: elements.editPriority.value};
    var changes = {};
    Object.keys(values).forEach(function (key) { if (values[key] !== state.detail[key]) changes[key] = values[key]; });
    if (!Object.keys(changes).length) { notice('没有需要保存的更改。', 'success'); return; }
    return patchTask(state.detail, changes, true);
  }
  function init() {
    var ids = {
      app: 'workApp', notice: 'workNotice', welcome: 'workWelcome', enable: 'workEnable', content: 'workContent',
      date: 'workDate', timezone: 'workTimezone', viewTitle: 'workViewTitle', refresh: 'workRefresh',
      title: 'workTitle', description: 'workDescription', priority: 'workPriority', due: 'workDue', dueChoice: 'workDueChoice', dueChoiceLabel: 'workDueChoiceLabel', dueHint: 'workDueHint',
      createForm: 'workCreateForm', create: 'workCreate', list: 'workTaskList', empty: 'workEmpty', listTitle: 'workListTitle', listSummary: 'workListSummary', listBoundary: 'workListBoundary', loadMore: 'workLoadMore',
      todayCount: 'workTodayCount', allCount: 'workAllCount', doneCount: 'workDoneCount', detail: 'workDetail', detailState: 'workDetailState', closeDetail: 'workCloseDetail', detailForm: 'workDetailForm',
      editTitle: 'workEditTitle', editDescription: 'workEditDescription', editStatus: 'workEditStatus', editPriority: 'workEditPriority', detailDue: 'workDetailDue', saveDetail: 'workSaveDetail', reloadDetail: 'workReloadDetail', history: 'workHistory'
    };
    Object.keys(ids).forEach(function (key) { elements[key] = byId(ids[key]); });
    elements.refresh.addEventListener('click', function () { loadOverview(); });
    elements.enable.addEventListener('click', enableSpace);
    elements.createForm.addEventListener('submit', createTask);
    elements.due.addEventListener('input', updateDue);
    elements.loadMore.addEventListener('click', loadMore);
    elements.detailForm.addEventListener('submit', saveDetail);
    elements.reloadDetail.addEventListener('click', function () {
      if (state.selected !== null && window.confirm('读取最新版本会替换详情中的未保存输入。继续吗？')) openTask(state.selected, true);
    });
    elements.closeDetail.addEventListener('click', function () {
      if (detailDirty() && !window.confirm('详情中还有未保存输入。关闭并放弃这些输入吗？')) return;
      state.detailSeq++; state.selected = null; state.detail = null; elements.detail.hidden = true; elements.app.dataset.detailOpen = 'false';
    });
    document.querySelectorAll('[data-work-filter]').forEach(function (button) {
      button.addEventListener('click', function () { state.filter = button.dataset.workFilter; renderTasks(); });
    });
    elements.createForm.addEventListener('keydown', function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); elements.createForm.requestSubmit(); }
    });
    window.addEventListener('beforeunload', function (event) {
      var draft = elements.title.value || elements.description.value || elements.due.value || detailDirty() || state.pendingCreate || state.pendingSpace || state.patchUnknown.size;
      if (draft) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('pagehide', function () { concealPrivateUI(true); });
    window.addEventListener('blur', function () { concealPrivateUI(true); });
    window.addEventListener('pageshow', function () { loadOverview(); });
    window.addEventListener('focus', function () { loadOverview(); });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) concealPrivateUI(true);
      else loadOverview();
    });
    loadOverview();
  }
  // Pure helpers are also used by the offline shipped-script regression harness.
  window.WorkTasks = {init: init, possibleInstants: possibleInstants, dateKey: dateKey, state: state, loadOverview: loadOverview, createTask: createTask, enableSpace: enableSpace, openTask: openTask, patchTask: patchTask, loadMore: loadMore, updateDue: updateDue, renderTasks: renderTasks};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, {once: true});
  else init();
}());
