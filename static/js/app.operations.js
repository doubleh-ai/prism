/* Read-only delivery observation. Production actions remain outside this console. */
(function() {
  'use strict';
  var originalSwitchMode = App.switchMode;
  var runLabels = {queued:'排队中',requested:'等待调度',waiting:'等待批准',pending:'等待中',in_progress:'运行中',completed:'已结束'};
  var conclusionLabels = {success:'通过',failure:'失败',startup_failure:'启动失败',cancelled:'已取消',timed_out:'超时',skipped:'已跳过',neutral:'无结论',action_required:'需要处理',stale:'已失效'};
  Object.assign(App, {
    _operationsData: null,
    _operationsError: '',
    _operationsForbidden: false,
    _operationsLoading: false,
    _operationsRequest: null,
    _operationsTimer: null,
    _operationsDeadline: null,
    _operationsSequence: 0,

    _operationsVisible: function() {
      return !!this.isAdmin && !this._operationsForbidden && this._mode === 'admin' && this._adminTab === 'operations' && document.visibilityState !== 'hidden';
    },
    _pauseOperations: function() {
      this._operationsSequence++;
      if (this._operationsTimer !== null) clearTimeout(this._operationsTimer);
      this._operationsTimer = null;
      if (this._operationsDeadline !== null) clearTimeout(this._operationsDeadline);
      this._operationsDeadline = null;
      if (this._operationsRequest) this._operationsRequest.abort();
      this._operationsRequest = null;
      this._operationsLoading = false;
      if (!this.isAdmin) this._operationsData = null;
    },
    _activateOperations: function() {
      if (this._operationsVisible()) this._loadOperations();
      else this._pauseOperations();
    },
    _loadOperations: async function() {
      if (!this._operationsVisible() || this._operationsRequest) return;
      if (this._operationsTimer !== null) clearTimeout(this._operationsTimer);
      this._operationsTimer = null;
      var sequence = ++this._operationsSequence;
      var request = new AbortController();
      this._operationsRequest = request;
      this._operationsDeadline = setTimeout(function() { request.abort(); }, 20000);
      this._operationsLoading = true;
      this._operationsError = '';
      this._paintOperations();
      try {
        var response = await this.api('/api/admin/operations', {signal:request.signal, cache:'no-store'});
        if (sequence !== this._operationsSequence || !this._operationsVisible()) return;
        if (response.status === 401 || response.status === 403) {
          this._operationsData = null;
          this._operationsForbidden = true;
          this._operationsError = '当前账号无权查看发布控制台，请重新登录确认权限。';
          return;
        }
        if (!response.ok) throw new Error('operations unavailable');
        var data = await response.json();
        if (sequence !== this._operationsSequence || !this._operationsVisible()) return;
        if (!data || !data.github || !data.runtime || !data.delivery) throw new Error('invalid snapshot');
        this._operationsData = data;
      } catch(error) {
        if (sequence === this._operationsSequence && this._operationsVisible()) {
          this._operationsError = '本次刷新失败，当前状态未知。'+(this._operationsData ? '下方保留上次观测，不代表最新状态。' : '请稍后重试。');
        }
      } finally {
        if (sequence === this._operationsSequence) {
          if (this._operationsDeadline !== null) clearTimeout(this._operationsDeadline);
          this._operationsDeadline = null;
          this._operationsRequest = null;
          this._operationsLoading = false;
          this._paintOperations();
          if (this._operationsVisible()) {
            var self = this;
            this._operationsTimer = setTimeout(function() { self._operationsTimer = null; self._loadOperations(); }, 30000);
          }
        }
      }
    },
    _paintOperations: function() {
      if (this._mode !== 'admin' || this._adminTab !== 'operations') return;
      var body = document.getElementById('adminOperationsContent');
      if (body) body.innerHTML = this._renderOperationsContent();
    },
    _operationsTime: function(value) { return ChatRender.formatTime(value); },
    _operationsLink: function(url, label, type) {
      var accepted = '';
      try {
        var value = safeUrl(url), parsed = new URL(value);
        var prefix = '/doubleh-ai/prism';
        var pathOK = type === 'run' ? /^\/doubleh-ai\/prism\/actions\/runs\/\d+\/?$/.test(parsed.pathname) :
          type === 'pr' ? /^\/doubleh-ai\/prism\/pull\/\d+\/?$/.test(parsed.pathname) :
          [prefix,prefix+'/',prefix+'/actions',prefix+'/pulls'].indexOf(parsed.pathname) >= 0;
        if (parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.port && pathOK) accepted = value;
      } catch(error) {}
      return accepted ? '<a href="'+escAttr(accepted)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+' ↗</a>' : '<span class="operations-muted">链接不可用</span>';
    },
    _renderOperationsPanel: function() {
      return '<section class="operations-console" aria-label="发布控制台"><div id="adminOperationsContent">'+this._renderOperationsContent()+'</div></section>';
    },
    _renderOperationsRuns: function(runs, kind, title, description, unavailable) {
      var self = this;
      var rows = runs.filter(function(run) { return run.kind === kind; }).map(function(run) {
        var active = ['queued','requested','waiting','pending','in_progress'].indexOf(run.status) >= 0;
        var tone = run.conclusion === 'success' ? 'ok' : ['failure','startup_failure','timed_out','action_required'].indexOf(run.conclusion) >= 0 ? 'attention' : active ? 'active' : 'neutral';
        var state = conclusionLabels[run.conclusion] || runLabels[run.status] || '状态未知';
        return '<article class="operations-run"><header><strong>'+esc(run.name || '未命名流程')+'</strong><span class="operations-state '+tone+'">'+esc(state)+'</span></header>'+ 
          '<p>'+esc(run.branch || '分支未提供')+' · <code>'+esc(String(run.head_sha || '版本未提供').slice(0,12))+'</code> · '+esc(run.event || '触发原因未知')+'</p>'+ 
          '<div class="operations-muted">创建 '+esc(self._operationsTime(run.created_at))+'<br>更新 '+esc(self._operationsTime(run.updated_at))+'</div>'+ 
          '<footer>'+self._operationsLink(run.url, active ? '打开流程 / 在 GitHub 取消' : '打开流程', 'run')+'</footer></article>';
      }).join('');
      return '<section class="operations-group"><div class="operations-section-head"><h3>'+title+'</h3><p>'+description+'</p></div>'+ 
        (rows || '<div class="operations-empty">'+(unavailable ? '当前数据不完整，不能据此判断没有流程。' : '本次返回的近期记录中没有此类流程。')+'</div>')+'</section>';
    },
    _renderOperationsContent: function() {
      if (!this.isAdmin || this._operationsForbidden) return '<div class="operations-notice attention" role="alert">'+esc(this._operationsError || '无管理权限')+'</div>';
      var data = this._operationsData;
      var html = '<div class="operations-heading"><div><span class="operations-eyebrow">DELIVERY OBSERVATION</span><h2>发布控制台</h2><p>只读观测公开 GitHub 流程与本实例信息；页面激活时每 30 秒刷新。</p></div><span class="operations-state neutral">只读</span></div>'+ 
        '<div class="operations-notice"><strong>生产未接入 · 当前仅构建候选版本</strong><p>此页不能发布、回滚或停止生产服务。取消 GitHub 流程不等于回滚，已经执行的步骤不会自动撤销。</p><p>GitHub 快照最多约 3 分钟更新一次；实时日志与取消入口在 GitHub 流程页。</p></div>';
      if (this._operationsError) html += '<div class="operations-notice attention" role="alert">'+esc(this._operationsError)+'</div>';
      if (!data) return html+'<div class="operations-empty" role="status">'+(this._operationsLoading ? '正在读取真实流程与实例信息…' : '尚未取得观测数据。')+'</div>';
      var github = data.github, runtime = data.runtime, links = data.links || {};
      var unavailable = github.status !== 'ok' || !!github.stale || !!this._operationsError;
      var env = {local:'本地',staging:'预发布',production:'生产（实例自报，未核验）',unknown:'未知'}[runtime.environment] || '未知';
      html += '<div class="operations-observation"><span>本次观测 '+esc(this._operationsTime(data.observed_at))+'</span><span>GitHub 检查 '+esc(this._operationsTime(github.checked_at))+'</span><span class="operations-state '+(unavailable?'attention':'ok')+'">'+(unavailable?'数据不可用或已过期':'已取得本次数据')+'</span>'+(this._operationsLoading?'<span>刷新中…</span>':'')+'</div>';
      if (github.status !== 'ok' || github.stale) html += '<div class="operations-notice attention" role="status"><strong>'+(github.stale?'GitHub 数据已过期':'GitHub 数据暂不可用')+'</strong><p>'+esc(github.error || '请在 GitHub 查看最新状态；这里的历史记录不代表当前执行情况。')+'</p></div>';
      html += '<div class="operations-summary"><article><span>当前查看的实例</span><strong>'+esc(runtime.version || '版本未知')+'</strong><p>'+esc(env)+' · 本实例版本，非生产核验</p><code>'+esc(runtime.deployed_revision || '部署版本未记录')+'</code></article><article><span>发布边界</span><strong>候选构建</strong><p>'+esc(data.delivery.reason || '生产环境尚未接入发布控制。')+'</p><div class="operations-disabled-actions"><button disabled>部署不可用</button><button disabled>回滚不可用</button></div></article></div>'+ 
        '<div class="operations-links"><span>'+esc(data.repository || '仓库未提供')+'</span>'+this._operationsLink(links.actions,'全部流程')+this._operationsLink(links.pull_requests,'Pull requests')+this._operationsLink(links.repository,'仓库')+'</div>';
      var runs = Array.isArray(github.runs) ? github.runs : [];
      html += '<div class="operations-run-groups">'+this._renderOperationsRuns(runs,'ci','CI 检查','测试与静态检查；通过不代表已经发布。',unavailable)+this._renderOperationsRuns(runs,'candidate','候选构建','候选产物生成；构建成功不代表生产已更新。',unavailable)+this._renderOperationsRuns(runs,'deployment','部署流程','仅列出 GitHub 部署流程记录；不据此确认线上版本。',unavailable)+'</div>';
      var self = this, prs = Array.isArray(github.pull_requests) ? github.pull_requests : [];
      html += '<section class="operations-group"><div class="operations-section-head"><h3>当前开放 PR</h3><p>展示当前开放的草稿与待合并 PR；已关闭或已合并的记录请到 GitHub 查看。</p></div><div class="operations-pr-list">'+(prs.map(function(pr) {
        return '<article class="operations-pr"><div><strong>#'+esc(pr.number)+' '+esc(pr.title)+'</strong><p>'+esc(pr.head || '未知分支')+' → '+esc(pr.base || '未知分支')+' · <code>'+esc(String(pr.head_sha || '').slice(0,12))+'</code></p></div><span class="operations-state neutral">'+esc(pr.draft?'草稿':{open:'待合并',closed:'已关闭',merged:'已合并'}[pr.state] || '状态未知')+'</span>'+self._operationsLink(pr.url,'查看 PR','pr')+'</article>';
      }).join('') || '<div class="operations-empty">'+(unavailable?'PR 数据不可用，请到 GitHub 查看。':'本次快照没有开放 PR；这不代表没有已关闭或已合并的记录。')+'</div>')+'</div></section>';
      return html;
    }
  });
  App.switchMode = function(mode) {
    originalSwitchMode.call(this, mode);
    this._activateOperations();
  };
  document.addEventListener('visibilitychange', function() { App._activateOperations(); });
})();
