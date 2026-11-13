/* Share the feedback widget without loading Prism or leaving the Work draft. */
(function () {
  'use strict';
  var dialog = document.getElementById('feedbackDialog');
  var entries = document.querySelectorAll('[data-work-feedback]');
  if (!dialog || !entries.length) return;
  var page = { '/work':'work', '/projects':'projects', '/calendar':'calendar' }[location.pathname];
  if (!page) return;
  var owner = null, locked = false, opening = false, trigger = null;
  var app = window.App = {
    isAdmin:false, _mode:page === 'projects' ? 'work' : page, _feedbackOrigin:'',
    _feedbackPageLabel:{work:'我的任务',projects:'项目',calendar:'日历安排'}[page]
  };

  function lock(message) {
    locked = true;
    app._feedbackReadSeq++;
    app._feedbackItems = [];
    app._feedbackSelected = null;
    app._feedbackNext = null;
    app._feedbackDraft = null;
    document.getElementById('feedbackSummary').value = '';
    document.getElementById('feedbackDescription').value = '';
    document.getElementById('feedbackForm').hidden = true;
    document.getElementById('feedbackContent').replaceChildren();
    document.getElementById('feedbackMessage').textContent = message;
  }
  async function request(path, options) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, 30000);
    try {
      return await fetch(path, Object.assign({}, options || {}, {
        credentials:'same-origin', cache:'no-store', redirect:'error', signal:controller.signal
      }));
    } finally { clearTimeout(timer); }
  }
  async function identify() {
    if (locked) throw new Error('账号状态已变化，请刷新页面后继续。');
    var response = await request('/api/me');
    if (response.status === 401) {
      lock('登录已失效，请重新登录后再打开反馈。');
      var expired = new Error('登录已失效'); expired.status = 401; throw expired;
    }
    if (!response.ok) throw new Error('暂时无法核对账号，请重新打开反馈。');
    var me = await response.json();
    if (!me || typeof me.user !== 'string' || !me.user) throw new Error('账号信息不完整');
    if (locked) throw new Error('账号状态已变化');
    if (owner && owner !== me.user) {
      lock('账号已切换，请刷新页面后再打开反馈。');
      throw new Error('账号已切换');
    }
    owner = me.user;
    app.user = owner;
  }
  app.api = async function (path, options) {
    var method = options && options.method || 'GET';
    // Work can submit/read its owner's feedback; reviews stay in admin.
    if (!/^\/api\/feedback(?:\/[1-9]\d*)?(?:\?limit=50(?:&before_id=[1-9]\d*)?)?$/.test(path) ||
        !['GET','POST'].includes(method) || (method === 'POST' && path !== '/api/feedback')) {
      throw new Error('此入口只处理本人的反馈。');
    }
    await identify();
    var headers = Object.assign({}, options && options.headers || {}, {
      'X-Requested-With':'XMLHttpRequest', 'X-Prism-Account':owner
    });
    var response = await request(path, Object.assign({}, options || {}, {headers:headers}));
    await identify();
    return response;
  };
  async function open(event) {
    if (opening) return;
    opening = true;
    trigger = event.currentTarget;
    try {
      await identify();
      app._openFeedback();
    } catch (error) {
      if (!dialog.open) dialog.showModal();
      if (!locked) document.getElementById('feedbackMessage').textContent = '暂时无法打开反馈，请关闭后重试。';
      document.getElementById('feedbackForm').hidden = true;
      document.getElementById('feedbackContent').hidden = true;
    } finally { opening = false; }
  }
  entries.forEach(function (entry) { entry.addEventListener('click', open); });
  dialog.addEventListener('close', function () { if (trigger) trigger.focus(); });
  document.addEventListener('visibilitychange', async function () {
    if (!dialog.open) return;
    if (document.hidden) { dialog.style.visibility = 'hidden'; return; }
    try { await identify(); }
    catch (error) {
      if (!locked) lock('无法核对账号，请刷新页面后再打开反馈。');
    } finally { dialog.style.visibility = ''; }
  });
  window.addEventListener('beforeunload', function (event) {
    if (app._feedbackBusy || document.getElementById('feedbackSummary').value || document.getElementById('feedbackDescription').value) {
      event.preventDefault(); event.returnValue = '';
    }
  });
}());
