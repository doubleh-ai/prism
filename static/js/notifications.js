(function () {
  'use strict';
  var dialog = document.getElementById('workNotifications');
  if (!dialog) return;
  var trigger = null;
  var $ = function (id) { return document.getElementById('notification' + id.charAt(0).toUpperCase() + id.slice(1)); };
  var state = {user:null, epoch:0, busy:false, locked:false};
  function message(text) { $('notice').textContent = text; $('notice').hidden = !text; }
  function clear() { $('content').hidden = true; $('list').replaceChildren(); }
  async function request(path, method) {
    var controller = new AbortController(), timer = setTimeout(function () { controller.abort(); }, 30000);
    var headers = {Accept:'application/json'};
    if (method === 'POST') { headers['Content-Type'] = 'application/json'; headers['X-Requested-With'] = 'XMLHttpRequest'; headers['X-Prism-Account'] = state.user; }
    try {
      var response = await fetch(path, {method:method || 'GET', credentials:'same-origin', cache:'no-store', redirect:'error', signal:controller.signal, headers:headers, body:method === 'POST' ? '{}' : undefined});
      if (!response.ok) { var error = new Error('request'); error.status = response.status; throw error; }
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  async function identify() {
    var me = await request('/api/me');
    if (!me || typeof me.user !== 'string' || !me.user) throw new Error('invalid account');
    if (state.user && state.user !== me.user) { state.user = null; ++state.epoch; clear(); message('账号已切换，请刷新后重新读取提醒。'); throw new Error('account changed'); }
    state.user = me.user; return me.user;
  }
  function formatTime(value) {
    if (value === null || value === undefined || value === '') return '时间不可用';
    var date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '时间不可用';
    return new Intl.DateTimeFormat('zh-CN', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(date);
  }
  function render(items) {
    var list = $('list'); list.replaceChildren(); $('empty').hidden = items.length > 0;
    items.forEach(function (item) {
      var row = document.createElement('article'); row.className = 'project-row';
      var copy = document.createElement('div'); copy.className = 'project-open';
      var title = document.createElement('strong'); title.textContent = item.title;
      var labels={task:'任务截止',calendar:'日程开始',project_task:'项目任务截止',approval:'待你审批',approval_result:'审批结果'};
      var detail = document.createElement('span'); detail.textContent = (labels[item.kind] || '提醒') + (['task','calendar','project_task'].includes(item.kind) ? ' · ' + formatTime(item.due_at) : '') + (item.read_at == null ? ' · 未读' : ' · 已读');
      copy.append(title,detail);
      var destinations={task:['/work','查看任务'],calendar:['/calendar','查看日历'],project_task:['/projects','查看项目'],approval:['/projects','查看项目'],approval_result:['/projects','查看项目']},destination=destinations[item.kind];
      if (destination && item.href === destination[0]) { var link = document.createElement('a'); link.href = destination[0]; link.textContent = destination[1]; link.className = 'fine-print'; copy.appendChild(link); }
      row.appendChild(copy);
      if (item.read_at == null) { var button = document.createElement('button'); button.type = 'button'; button.className = 'button button-secondary'; button.textContent = '标为已读'; button.addEventListener('click', function () { markRead(item.id, button); }); row.appendChild(button); }
      list.appendChild(row);
    });
  }
  async function load() {
    var epoch = ++state.epoch; clear(); $('loginPrompt').hidden = true; message('正在核对账号并读取提醒…');
    try {
      var user = await identify(), data = await request('/api/work/notifications'); await identify();
      if (epoch !== state.epoch || user !== state.user) return;
      if (!data || !Array.isArray(data.notifications)) throw new Error('invalid response');
      state.locked = false; render(data.notifications); $('content').hidden = false; message('');
    } catch (error) {
      if (epoch !== state.epoch && error.message !== 'account changed') return;
      clear();
      if (error.status === 401) { message(''); $('loginPrompt').hidden = false; }
      else message(error.message === 'account changed' ? '账号已切换，请刷新后重新读取提醒。' : '无法读取提醒，请刷新重试。');
    }
  }
  async function markRead(id, button) {
    if (state.busy || state.locked) return;
    var epoch = state.epoch; state.busy = true; button.disabled = true;
    try {
      await identify(); if (epoch !== state.epoch) return;
      await request('/api/work/notifications/' + encodeURIComponent(id) + '/read', 'POST');
      await identify(); if (epoch !== state.epoch) return;
      await load();
    } catch (error) {
      if (epoch !== state.epoch) return;
      if (error.status === 401) { clear(); message(''); $('loginPrompt').hidden = false; }
      else if (!error.status || error.status >= 500 || error.status === 408) { state.locked = true; message('操作结果未知，请刷新核对后再操作。'); }
      else if (error.status === 409) { state.locked = true; message('提醒状态已变化，请刷新后再操作。'); }
      else message('未能标记已读，请刷新后重试。');
    } finally { state.busy = false; button.disabled = false; }
  }
  $('refresh').addEventListener('click', load);
  function open(button) {
    trigger = button || trigger;
    if (!dialog.open) dialog.showModal();
    load();
  }
  document.querySelectorAll('[data-work-notifications]').forEach(function (button) {
    button.addEventListener('click', function () { open(button); });
  });
  $('close').addEventListener('click', function () { dialog.close(); });
  dialog.addEventListener('close', function () {
    ++state.epoch; clear();
    if (trigger && trigger.isConnected) trigger.focus();
    var url = new URL(location.href);
    if (url.searchParams.get('panel') === 'notifications') {
      url.searchParams.delete('panel'); history.replaceState(null, '', url.pathname + url.search + url.hash);
    }
  });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { ++state.epoch; clear(); message('请刷新以核对当前账号。'); }
    else if (dialog.open) load();
  });
  if (new URLSearchParams(location.search).get('panel') === 'notifications') {
    open(document.querySelector('[data-work-notifications]'));
  }
}());
