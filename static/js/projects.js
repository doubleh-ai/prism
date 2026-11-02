(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var state = {user:null, projects:[], selected:null, epoch:0, busy:false, locked:false, pendingTask:null, detailSeq:0};
  var labels = {active:'进行中', completed:'已完成', archived:'已归档'};
  var taskLabels = {todo:'待开始', doing:'进行中', done:'已完成', cancelled:'已取消'};
  function message(value) { $('notice').textContent = value; $('notice').hidden = !value; }
  function clear() {
    state.projects = []; state.selected = null; ++state.detailSeq; $('content').hidden = true; $('detail').hidden = true;
    $('projectList').replaceChildren(); $('taskList').replaceChildren(); $('account').textContent = '';
    $('createForm').reset(); $('editForm').reset(); $('taskForm').reset(); $('taskHint').hidden = true;
  }
  async function request(path, options) {
    var method = options && options.method || 'GET';
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 30000);
    try {
      var headers = {Accept:'application/json'};
      if (method !== 'GET') {
        headers['Content-Type'] = 'application/json';
        headers['X-Requested-With'] = 'XMLHttpRequest';
        headers['X-Prism-Account'] = state.user;
      }
      var response = await fetch(path, {method:method, credentials:'same-origin', cache:'no-store', redirect:'error', signal:controller.signal, headers:headers, body:options && options.body ? JSON.stringify(options.body) : undefined});
      if (!response.ok) { var error = new Error('request'); error.status = response.status; try { var detail = await response.json(); error.detail = typeof detail.detail === 'string' ? detail.detail : ''; } catch (_) {} throw error; }
      return await response.json();
    } finally { clearTimeout(timer); }
  }
  async function identify() {
    var me = await request('/api/me');
    if (!me || typeof me.user !== 'string' || !me.user) throw new Error('invalid account');
    if (state.user && state.user !== me.user) { state.user = null; ++state.epoch; clear(); message('账号已切换，请刷新后重新读取项目。'); throw new Error('account changed'); }
    state.user = me.user;
    return me.user;
  }
  function render() {
    var list = $('projectList'); list.replaceChildren();
    $('empty').hidden = state.projects.length > 0;
    state.projects.forEach(function (project) {
      var row = document.createElement('div'); row.className = 'project-row';
      var open = document.createElement('button'); open.type = 'button'; open.className = 'project-open';
      var title = document.createElement('strong'); title.textContent = project.name;
      var description = document.createElement('span'); description.textContent = project.description || '';
      open.append(title, description); open.addEventListener('click', function () { openProject(project.id); });
      var status = document.createElement('span'); status.className = 'project-status'; status.textContent = labels[project.status] || '状态未知';
      row.append(open, status); list.appendChild(row);
    });
  }
  async function load() {
    var epoch = ++state.epoch; clear(); message('正在核对账号并读取项目…');
    try {
      var user = await identify();
      var data = await request('/api/work/projects');
      await identify();
      if (epoch !== state.epoch || user !== state.user) return;
      if (!data || !Array.isArray(data.projects)) throw new Error('invalid response');
      state.projects = data.projects; state.locked = false; $('account').textContent = user; render(); $('content').hidden = false; message(''); return true;
    } catch (error) {
      if (epoch !== state.epoch && error.message !== 'account changed') return;
      clear(); message(error.status === 401 ? '登录已失效，请重新登录。' : error.message === 'account changed' ? '账号已切换，请刷新后重新读取项目。' : '无法读取项目，请刷新重试。');
    }
  }
  async function openProject(id) {
    var epoch = state.epoch, sequence = ++state.detailSeq; state.selected = null; $('detail').hidden = true; $('taskList').replaceChildren(); $('taskHint').hidden = true;
    try {
      await identify(); var data = await request('/api/work/projects/' + encodeURIComponent(id) + '/tasks'); await identify();
      if (epoch !== state.epoch || sequence !== state.detailSeq) return;
      if (!data || !data.project || !Array.isArray(data.tasks)) throw new Error('invalid response');
      state.selected = data.project;
      $('editName').value = data.project.name; $('editDescription').value = data.project.description || ''; $('editStatus').value = data.project.status;
      var list = $('taskList');
      if (!data.tasks.length) { var empty = document.createElement('p'); empty.className = 'project-task-message'; empty.textContent = '暂无项目任务。'; list.appendChild(empty); }
      data.tasks.forEach(function (task) { var item = document.createElement('div'); item.className = 'project-task'; var title = document.createElement('strong'); title.textContent = task.title; var status = document.createElement('span'); status.textContent = taskLabels[task.status] || '状态未知'; item.append(title,status); list.appendChild(item); });
      $('taskForm').hidden = data.project.status !== 'active'; $('detail').hidden = false; $('detail').scrollIntoView({block:'nearest'}); message('');
    } catch (error) { if (epoch === state.epoch && sequence === state.detailSeq) message('无法读取项目任务，请重新打开项目。'); }
  }
  async function save(event, editing) {
    event.preventDefault();
    if (state.busy || state.locked || (editing && !state.selected)) return;
    state.busy = true; var form = event.currentTarget; var button = form.querySelector('button[type="submit"]'); button.disabled = true;
    var epoch = state.epoch;
    try {
      if (!window.crypto || !window.crypto.randomUUID) throw new Error('secure context');
      await identify(); if (epoch !== state.epoch) return;
      var body = {request_id:crypto.randomUUID(), name:$(editing ? 'editName' : 'createName').value.trim(), description:$(editing ? 'editDescription' : 'createDescription').value.trim()};
      if (!body.name) throw new Error('empty name');
      if (editing) { body.expected_version = state.selected.version; body.status = $('editStatus').value; }
      await request(editing ? '/api/work/projects/' + encodeURIComponent(state.selected.id) : '/api/work/projects', {method:editing ? 'PATCH' : 'POST', body:body});
      await identify(); if (epoch !== state.epoch) return;
      if (await load()) message(editing ? '项目已保存。' : '项目已创建。');
    } catch (error) {
      if (epoch !== state.epoch) { clear(); message('账号已切换，请刷新后重新读取项目。'); return; }
      if (error.message === 'empty name') message('请输入项目名称。');
      else if (error.message === 'secure context') message('当前环境无法安全创建请求，请使用安全连接。');
      else if (error.status === 409) { state.locked = true; message('项目已发生变化，请刷新读取最新内容后再保存。'); }
      else if (!error.status || error.status >= 500 || error.status === 408) { state.locked = true; message('保存结果未知。请刷新核对后再操作，避免重复提交。'); }
      else message('保存未完成，请检查账号和输入后重试。');
    } finally { state.busy = false; button.disabled = false; }
  }
  async function createTask(event) {
    event.preventDefault();
    if (state.busy || state.locked || !state.selected || state.selected.status !== 'active') return;
    var projectId = state.selected.id, epoch = state.epoch, sequence = state.detailSeq;
    var button = $('taskForm').querySelector('button[type="submit"]');
    state.busy = true; button.disabled = true; $('taskHint').hidden = true;
    try {
      if (!window.crypto || !window.crypto.randomUUID) throw new Error('secure context');
      var title = $('taskTitle').value.trim(); if (!title) throw new Error('empty name');
      var timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      if (!timezone) throw new Error('timezone');
      await identify();
      if (epoch !== state.epoch || sequence !== state.detailSeq || !state.selected || state.selected.id !== projectId) return;
      state.pendingTask = {request_id:crypto.randomUUID(), title:title, description:'', status:'todo', priority:$('taskPriority').value, due_at:null, timezone:timezone};
      await request('/api/work/projects/' + encodeURIComponent(projectId) + '/tasks', {method:'POST', body:state.pendingTask});
      await identify();
      if (epoch !== state.epoch || sequence !== state.detailSeq || !state.selected || state.selected.id !== projectId) return;
      state.pendingTask = null; $('taskForm').reset(); await openProject(projectId);
    } catch (error) {
      if (epoch !== state.epoch || sequence !== state.detailSeq) return;
      if (error.status === 409) {
        state.pendingTask = null; state.locked = true;
        $('taskHint').replaceChildren(document.createTextNode((error.detail || '项目状态已变化，或尚未启用我的工作台。') + ' '));
        var link = document.createElement('a'); link.href = '/work'; link.textContent = '前往我的工作台'; $('taskHint').appendChild(link); $('taskHint').hidden = false;
        message('请刷新项目后再操作。');
      } else if (error.message === 'empty name') message('请输入任务名称。');
      else if (error.message === 'secure context' || error.message === 'timezone') message('无法安全创建任务，请检查连接和设备时区。');
      else if (!error.status || error.status >= 500 || error.status === 408) { state.locked = true; message('任务保存结果未知。请刷新核对后再操作，避免重复提交。'); }
      else { state.pendingTask = null; message('任务未创建，请检查账号和输入后重试。'); }
    } finally { state.busy = false; button.disabled = false; }
  }
  $('taskForm').addEventListener('submit', createTask);
  $('createForm').addEventListener('submit', function (event) { save(event, false); });
  $('editForm').addEventListener('submit', function (event) { save(event, true); });
  $('closeDetail').addEventListener('click', function () { $('detail').hidden = true; state.selected = null; ++state.detailSeq; });
  $('refresh').addEventListener('click', load);
  document.addEventListener('visibilitychange', function () { if (document.hidden) { ++state.epoch; clear(); message('请刷新以核对当前账号。'); } });
  load();
}());
