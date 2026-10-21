/* Prism - official Vibe-Trading research workbench (v0.19.7) */
Object.assign(App, {
  _vibePlans: [],
  _activeVibePlanId: '',
  _activeVibePlan: null,
  _vibePollTimers: {},
  _vibeEngine: null,

  _initVibe: function() {
    var shell = document.getElementById('appShell');
    if (shell) shell.classList.remove('collapsed');
    this._loadVibePlans();
    this._loadVibeEngine();
    var input = document.getElementById('vibePrompt');
    if (input) setTimeout(function() { input.focus(); }, 50);
    if (!this._activeVibePlanId) this._renderVibeEmpty();
  },

  _vibeIsActive: function(plan) {
    return !!plan && ['queued', 'running', 'cancelling'].indexOf(plan.status) >= 0;
  },

  _loadVibeEngine: async function() {
    try { var response = await this.api('/api/vibe/engine');
      if (response.ok) this._vibeEngine = await response.json();
      else this._vibeEngine = {status:'unavailable'};
    } catch (error) { this._vibeEngine = {status:'unavailable'}; }
    if (!this._activeVibePlanId) this._renderVibeEmpty();
  },

  _renderVibeEmpty: function() {
    var inner = document.getElementById('vibePanelInner');
    if (!inner) return;
    var engine = this._vibeEngine || {};
    var meta = engine.engine || {name: 'HKUDS/Vibe-Trading', version: '0.1.11', revision: 'd456025'};
    var online = engine.status === 'healthy';
    inner.innerHTML = '<div class="vibe-home">' +
      '<div class="vibe-home-head">' +
        '<span>OFFICIAL RESEARCH ENGINE</span>' +
        '<h2>从一句市场直觉，走到可核验的完整研究</h2>' +
        '<p>研究会先澄清范围，再调用行情、财务、新闻与公开证据工具。最终结论会保留反方观点、失效条件和数据缺口。</p>' +
        '<div class="vibe-engine-line"><i class="' + (online ? 'online' : '') + '"></i><b>' + esc(meta.name || 'HKUDS/Vibe-Trading') + '</b><span>v' + esc(meta.version || '') + ' · ' + esc(meta.revision || '') + '</span><em>' + (online ? '引擎在线' : (engine.status === 'unavailable' ? '引擎暂不可用' : '正在检查')) + '</em></div>' +
      '</div>' +
      '<div class="vibe-home-grid">' +
        '<button onclick="App._vibeUseExample(\'存储产业帮我分析一下：先区分存储芯片、企业数据存储和储能，再找 A 股真正的卡点与证据。\')"><b>存储产业</b><span>先消歧，再比较三条产业链</span></button>' +
        '<button onclick="App._vibeUseExample(\'AI 数据中心液冷供应链，上游冷板、泵、温控设备谁被忽略？请核验订单和竞争格局。\')"><b>AI 液冷</b><span>从需求到瓶颈逐层验证</span></button>' +
        '<button onclick="App._vibeUseExample(\'人形机器人执行器与灵巧手有哪些尚未被充分定价的上游卡点？请同时给出反方证据。\')"><b>人形机器人</b><span>候选、反证与失效条件</span></button>' +
      '</div>' +
      '<div class="vibe-depth-notes"><span><b>聚焦研究</b> 更快回答一个关键问题</span><span><b>深度研究</b> 多工具交叉核验</span><span><b>多 Agent</b> 专业角色并行审查</span></div>' +
    '</div>';
  },

  _vibeUseExample: function(text) {
    var input = document.getElementById('vibePrompt');
    if (input) input.value = text;
    this._runVibePlan();
  },

  _runVibePlan: async function() {
    if (this._vibeCreateBusy) return;
    var promptEl = document.getElementById('vibePrompt');
    var prompt = promptEl ? promptEl.value.trim() : '';
    if (!prompt) {
      this._toast('先输入一个研究问题');
      return;
    }
    var sequence = this._vibeSelectionSeq = (this._vibeSelectionSeq || 0) + 1;
    this._vibeCreateBusy = true;
    var inner = document.getElementById('vibePanelInner');
    if (inner) inner.innerHTML = '<div class="vibe-requesting"><i></i><b>正在创建正式研究任务</b><span>记录会立即出现在历史中</span></div>';
    var payload = {
      prompt: prompt,
      market: (document.getElementById('vibeMarket') || {}).value || 'A股',
      horizon: (document.getElementById('vibeHorizon') || {}).value || 'swing',
      risk_level: (document.getElementById('vibeRisk') || {}).value || 'balanced',
      research_depth: (document.getElementById('vibeDepth') || {}).value || 'deep',
      agent: true
    };
    var response;
    try { response = await this.api('/api/vibe/plan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    }); } catch (error) { response = {ok:false}; }
    this._vibeCreateBusy = false;
    if (!response.ok) {
      if (inner && sequence === this._vibeSelectionSeq) inner.innerHTML = '<div class="market-empty-state">创建请求未确认，请先刷新历史核对是否已有任务，再决定重试。</div>';
      return;
    }
    var plan = (await response.json()).plan;
    this._upsertVibePlan(plan);
    this._renderVibeHistory();
    if (sequence !== this._vibeSelectionSeq) return;
    this._activeVibePlanId = plan.id;
    this._renderVibePlan(plan);
    this._pollVibePlan(plan.id);
  },

  _loadVibePlans: async function() {
    var sequence = this._vibeListSeq = (this._vibeListSeq || 0) + 1;
    var side = document.getElementById('vibeHistoryList');
    if (side) side.innerHTML = '<div class="reports-lib-empty">加载中...</div>';
    var response;
    try { response = await this.api('/api/vibe/plans?limit=30'); }
    catch (error) { response = {ok:false}; }
    if (sequence !== this._vibeListSeq) return;
    if (!response.ok) {
      if (side) side.innerHTML = '<div class="reports-lib-empty">加载失败</div>';
      return;
    }
    var plans = (await response.json()).plans || [];
    if (sequence !== this._vibeListSeq) return;
    plans.forEach(this._upsertVibePlan.bind(this));
    this._renderVibeHistory();
    for (var i = 0; i < this._vibePlans.length; i++) {
      if (this._vibeIsActive(this._vibePlans[i])) this._pollVibePlan(this._vibePlans[i].id);
    }
  },

  _renderVibeHistory: function() {
    var side = document.getElementById('vibeHistoryList');
    var count = document.getElementById('vibeHistoryCount');
    if (count) count.textContent = this._vibePlans.length ? '(' + this._vibePlans.length + ')' : '';
    if (!side) return;
    if (!this._vibePlans.length) {
      side.innerHTML = '<div class="reports-lib-empty">暂无研究记录</div>';
      return;
    }
    var self = this;
    side.innerHTML = this._vibePlans.map(function(plan) {
      var active = plan.id === self._activeVibePlanId ? ' active' : '';
      var running = self._vibeIsActive(plan);
      var state = running ? (plan.status === 'queued' ? '排队' : plan.status === 'cancelling' ? '等待停止确认' : '研究中') : ({failed:'失败', cancelled:'已停止', interrupted:'已中断', complete:'完成'})[plan.status] || '状态未知';
      var sub = [plan.market, self._vibeHorizonLabel(plan.horizon), plan.depth_label].filter(Boolean).join(' · ');
      return '<div class="vibe-side-item' + active + (running ? ' running' : '') + '" onclick="App._openVibePlan(\'' + escAttr(plan.id) + '\')">' +
        '<div class="vibe-side-main"><b>' + esc(plan.title || plan.prompt || 'Vibe 研究') + '</b><span>' + esc(sub) + '</span></div>' +
        (state ? '<em class="vibe-status ' + escAttr(plan.status || '') + '">' + esc(state) + '</em>' : '') +
        '<button class="vibe-side-del" title="删除" onclick="event.stopPropagation();App._deleteVibePlan(\'' + escAttr(plan.id) + '\')">×</button>' +
      '</div>';
    }).join('');
  },

  _openVibePlan: async function(id) {
    var sequence = this._vibeSelectionSeq = (this._vibeSelectionSeq || 0) + 1;
    this._activeVibePlanId = id;
    this._activeVibePlan = null;
    this._closeMobileSidebar();
    var found = this._vibePlans.find(function(plan) { return plan.id === id; });
    if (found) this._renderVibePlan(found);
    else { var inner = document.getElementById('vibePanelInner'); if (inner) inner.innerHTML = '<div class="market-empty-state">正在读取研究记录…</div>'; }
    var response;
    try { response = await this.api('/api/vibe/plans/' + encodeURIComponent(id)); }
    catch (error) { response = {ok:false}; }
    if (sequence !== this._vibeSelectionSeq || id !== this._activeVibePlanId) return;
    if (response.ok) {
      var plan = (await response.json()).plan;
      if (sequence !== this._vibeSelectionSeq || id !== this._activeVibePlanId) return;
      this._upsertVibePlan(plan);
      this._renderVibePlan(plan);
      if (this._vibeIsActive(plan)) this._pollVibePlan(plan.id);
    } else if (response.status !== 403 && response.status !== 404) {
      this._toast('研究记录暂时无法读取，正在重连原任务'); this._pollVibePlan(id);
    } else { this._toast('研究记录不存在或无权限'); }
    this._renderVibeHistory();
  },

  _upsertVibePlan: function(plan) {
    if (!plan || !plan.id) return;
    for (var i = 0; i < this._vibePlans.length; i++) {
      if (this._vibePlans[i].id === plan.id) {
        if (Number(this._vibePlans[i].updated_at) > Number(plan.updated_at)) return;
        this._vibePlans[i] = plan;
        return;
      }
    }
    this._vibePlans.unshift(plan);
  },

  _pollVibePlan: function(id) {
    var self = this;
    if (!id || this._vibePollTimers[id]) return;
    var state = {tries:0, timer:null, busy:false, failures:0};
    this._vibePollTimers[id] = state;
    var schedule = function() {
      if (self._vibePollTimers[id] !== state) return;
      var delay = state.failures ? Math.min(30000, 2000 * Math.pow(2, Math.min(state.failures - 1, 4))) : document.hidden ? 10000 : (state.tries < 15 ? 2000 : 5000);
      state.timer = setTimeout(tick, delay);
    };
    var tick = async function() {
      if (self._vibePollTimers[id] !== state || state.busy) return;
      state.busy = true;
      state.tries += 1;
      try {
        var response = await self.api('/api/vibe/plans/' + encodeURIComponent(id));
        if (self._vibePollTimers[id] !== state) return;
        if (!response.ok) {
          if (response.status === 403 || response.status === 404) self._stopVibePolling(id);
          else { state.failures += 1; schedule(); }
          return;
        }
        var plan = (await response.json()).plan;
        if (self._vibePollTimers[id] !== state) return;
        state.failures = 0;
        self._upsertVibePlan(plan);
        if (self._activeVibePlanId === id) self._renderVibePlan(plan);
        self._renderVibeHistory();
        if (!self._vibeIsActive(plan)) self._stopVibePolling(id);
        else schedule();
      } catch (error) {
        state.failures += 1;
        if (state.failures === 1 && self._toast) self._toast('研究进度连接中断，正在自动重试；后台任务不会重复启动');
        schedule();
      } finally {
        state.busy = false;
      }
    };
    tick();
  },

  _stopVibePolling: function(id) {
    if (!this._vibePollTimers[id]) return;
    var state = this._vibePollTimers[id];
    if (state.timer) clearTimeout(state.timer);
    delete this._vibePollTimers[id];
  },

  _cancelVibePlan: async function() {
    var plan = this._activeVibePlan || {};
    if (!plan.id || plan.id !== this._activeVibePlanId) return;
    var response;
    try { response = await this.api('/api/vibe/plans/' + encodeURIComponent(plan.id) + '/cancel', {method:'POST'}); }
    catch (error) { response = {ok:false}; }
    if (!response.ok) {
      this._toast('停止失败，请稍后再试');
      return;
    }
    var updated = (await response.json()).plan;
    this._stopVibePolling(plan.id);
    this._upsertVibePlan(updated);
    if (plan.id === this._activeVibePlanId) this._renderVibePlan(updated);
    this._renderVibeHistory();
    if (this._vibeIsActive(updated)) this._pollVibePlan(plan.id);
  },

  _deleteVibePlan: async function(id) {
    if (!id) return;
    var response = await this.api('/api/vibe/plans/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!response.ok) {
      this._toast('删除失败');
      return;
    }
    this._stopVibePolling(id);
    this._vibePlans = this._vibePlans.filter(function(plan) { return plan.id !== id; });
    if (this._activeVibePlanId === id) {
      this._activeVibePlanId = '';
      this._activeVibePlan = null;
      this._renderVibeEmpty();
    }
    this._renderVibeHistory();
    this._toast('已删除研究记录');
  },

  _renderVibePlan: function(plan) {
    if (!plan || plan.id !== this._activeVibePlanId) return;
    var inner = document.getElementById('vibePanelInner');
    if (!inner) return;
    var active = this._vibeIsActive(plan);
    var engine = plan.engine || {};
    var tags = (plan.tags || []).map(function(tag) { return '<span>' + esc(tag) + '</span>'; }).join('');
    var quality = plan.quality || {};
    var report = plan.report_markdown || '';
    var actions = active
      ? (plan.status === 'cancelling' ? '<button class="danger" disabled>等待停止确认</button>' : '<button class="danger" onclick="App._cancelVibePlan()">停止研究</button>')
      : (report ? '<button onclick="App._vibeToChat()">转到对话</button>' + ((plan.candidates || []).length ? '<button onclick="App._vibeAddAll()">候选入自选</button>' : '') : '');
    var stateClass = plan.status === 'complete' ? 'ok' : (plan.status === 'failed' ? 'error' : (active ? 'running' : 'muted'));
    var body = report ? this._vibeReportHtml(plan) : this._vibeResearchStateHtml(plan);

    inner.innerHTML = '<div class="vibe-workbench">' +
      '<header class="vibe-plan-head">' +
        '<div class="vibe-plan-copy"><div class="vibe-kicker">OFFICIAL VIBE RESEARCH</div><h2>' + esc(plan.title || 'Vibe 深度研究') + '</h2><p>' + esc(plan.prompt || '') + '</p>' +
          '<div class="vibe-engine-line"><i></i><b>' + esc(engine.name || 'HKUDS/Vibe-Trading') + '</b><span>v' + esc(engine.version || '') + ' · ' + esc(engine.revision || '') + '</span><em class="' + stateClass + '">' + esc(plan.stage_label || plan.status || '') + '</em></div>' +
          '<div class="vibe-tags">' + tags + '</div>' +
        '</div>' +
        '<div class="vibe-head-actions">' + actions + '</div>' +
      '</header>' +
      (active ? '<div class="research-notice">当前阶段：'+esc(plan.stage_label || (plan.status === 'cancelling' ? '已请求停止，尚待引擎确认' : '研究进行中'))+'；不估算完成百分比。</div>' : '') +
      '<div class="vibe-workbench-grid">' +
        '<aside class="vibe-research-rail">' + this._vibeRunMetricsHtml(plan, quality) + this._vibeActivityHtml(plan.activity || []) + this._vibeCandidateRailHtml(plan.candidates || []) + '</aside>' +
        '<main class="vibe-report-area">' + body + '</main>' +
      '</div>' +
    '</div>';
    this._activeVibePlan = plan;
  },

  _vibeRunMetricsHtml: function(plan, quality) {
    var budget = Number(plan.iteration_budget || 0);
    var estimate = plan.estimated_duration_label || '';
    var calls = Number(quality.tool_calls || 0);
    var success = Number(quality.tool_success || 0);
    var errors = Number(quality.tool_errors || 0);
    var degraded = Number(quality.tool_degraded || 0);
    var successPct = calls ? Math.max(0, Math.min(100, Math.round(success * 100 / calls))) : 0;
    var errorPct = calls ? Math.max(0, Math.min(100, errors * 100 / calls)) : 0;
    var degradedPct = calls ? Math.max(0, Math.min(100, degraded * 100 / calls)) : 0;
    return '<section class="vibe-run-summary"><h3>研究运行</h3>' +
      '<div class="vibe-run-state"><i class="' + escAttr(plan.status || '') + '"></i><div><b>' + esc(plan.stage_label || '等待研究') + '</b><span>' + this._vibeElapsed(plan.elapsed_seconds || 0) + '</span></div></div>' +
      '<dl><div><dt>轮次上限</dt><dd>' + (budget || '-') + '</dd></div><div><dt>预计时长</dt><dd>' + esc(estimate || '-') + '</dd></div><div><dt>工具调用</dt><dd>' + calls + '</dd></div><div><dt>有效采集</dt><dd>' + success + '</dd></div></dl>' +
      '<div class="vibe-run-health"><div class="vibe-run-health-head"><span>采集有效率</span><b>' + successPct + '%</b></div>' +
        '<div class="vibe-run-health-bar" aria-label="成功 ' + success + '，未命中 ' + degraded + '，接口异常 ' + errors + '"><i class="ok" style="width:' + successPct + '%"></i><i class="warn" style="width:' + degradedPct + '%"></i><i class="bad" style="width:' + errorPct + '%"></i></div>' +
        '<div class="vibe-run-health-legend"><span class="ok">有效 ' + success + '</span><span class="warn">未命中 ' + degraded + '</span><span class="bad">接口异常 ' + errors + '</span></div>' +
      '</div>' +
      '<div class="vibe-run-evidence"><span>报告缺口 <b class="' + (quality.reported_gaps ? 'bad' : '') + '">' + Number(quality.reported_gaps || 0) + '</b></span><span>报告链接（待核验） <b>' + Number(quality.source_links || 0) + '</b></span></div>' +
    '</section>';
  },

  _vibeActivityCopy: function(item) {
    var raw = String(item.result || item.summary || '');
    var lower = raw.toLowerCase();
    if (item.status === 'error') {
      if (/httpconnectionpool|max retries exceeded|connection refused|name resolution/.test(lower)) return '外部数据源暂时不可达，已记录为数据缺口';
      if (/timed out|timeout/.test(lower)) return '外部数据源响应超时，已记录为数据缺口';
      return raw && raw.charAt(0) !== '{' ? raw : '工具调用失败，已记录为数据缺口';
    }
    if (item.status === 'warning') {
      if (/no results found|"results"\s*:\s*\[\s*\]/.test(lower)) return '本次检索未找到可用公开证据';
      if (/timed out|timeout/.test(lower)) return '数据源响应超时，本次结果未采用';
      return raw && raw.charAt(0) !== '{' ? raw : '工具返回空结果或降级数据，本次结果未采用';
    }
    if (!raw) return item.status === 'complete' ? '已完成' : '等待执行';
    if (item.status === 'complete' && /^[\[{]/.test(raw.trim())) return '已返回有效数据';
    return raw.length > 120 ? raw.slice(0, 117) + '...' : raw;
  },

  _vibeActivityIssueGroups: function(items) {
    var groups = {};
    var order = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var copy = this._vibeActivityCopy(item);
      var key = (item.status || '') + '|' + (item.tool || item.label || '') + '|' + copy;
      if (!groups[key]) {
        groups[key] = {status:item.status || 'warning', label:item.label || item.tool || '研究工具', copy:copy, items:[]};
        order.push(key);
      }
      groups[key].items.push(item);
    }
    return order.map(function(key) { return groups[key]; });
  },

  _vibeActivityHtml: function(activity) {
    var current = activity.filter(function(item) { return item.status === 'running' || item.status === 'queued' || (item.kind === 'turn' && item.status !== 'complete'); });
    var issues = activity.filter(function(item) { return item.status === 'error' || item.status === 'warning'; });
    var done = activity.filter(function(item) { return item.status === 'complete'; });
    var visible = current.slice(-5);
    var issueGroups = this._vibeActivityIssueGroups(issues);
    var self = this;
    var render = function(item) {
      var result = self._vibeActivityCopy(item);
      var elapsed = item.elapsed_ms ? ' · ' + (item.elapsed_ms / 1000).toFixed(1) + 's' : '';
      return '<div class="vibe-activity-item ' + escAttr(item.status || '') + '"><i></i><div><b>' + esc(item.label || item.tool || '研究步骤') + '</b><span>' + esc(result) + elapsed + '</span></div></div>';
    };
    var issueHtml = issueGroups.map(function(group) {
      var detail = group.items.map(function(item) {
        var context = item.summary || item.label || item.tool || '研究调用';
        var elapsed = item.elapsed_ms ? (item.elapsed_ms / 1000).toFixed(1) + 's' : '';
        return '<li><span>' + esc(context) + '</span>' + (elapsed ? '<em>' + elapsed + '</em>' : '') + '</li>';
      }).join('');
      return '<div class="vibe-activity-issue-group ' + escAttr(group.status) + '"><i></i><div><b>' + esc(group.label) + '</b><span>' + esc(group.copy) + '</span>' +
        (group.items.length > 1 ? '<details><summary>' + group.items.length + ' 次调用</summary><ul>' + detail + '</ul></details>' : '') + '</div></div>';
    }).join('');
    return '<section class="vibe-activity"><h3>研究过程</h3>' +
      (visible.length ? visible.map(render).join('') : ((!issues.length && !done.length) ? '<div class="vibe-rail-empty">等待第一项工具调用</div>' : '')) +
      (issues.length ? '<details class="vibe-activity-issues"><summary><span>数据缺口</span><b>' + issues.length + '</b><em>展开查看</em></summary><div>' + issueHtml + '</div></details>' : '') +
      (done.length ? '<details class="vibe-activity-done"><summary>已完成 ' + done.length + ' 项</summary>' + done.slice(-12).reverse().map(render).join('') + '</details>' : '') +
    '</section>';
  },

  _vibeCandidateRailHtml: function(candidates) {
    if (!candidates.length) return '';
    return '<section class="vibe-rail-candidates"><h3>报告候选 <span>' + candidates.length + '</span></h3>' + candidates.slice(0, 12).map(function(item) {
      return '<button onclick="App._vibeOpenMarket(\'' + escAttr(item.ticker) + '\',\'' + escAttr(item.name || item.ticker) + '\')"><b>' + esc(item.ticker) + '</b><span>' + esc(item.name || item.ticker) + '</span></button>';
    }).join('') + '</section>';
  },

  _vibeResearchStateHtml: function(plan) {
    var failed = plan.status === 'failed';
    var cancelled = plan.status === 'cancelled';
    var interrupted = plan.status === 'interrupted';
    var title = failed ? '研究没有生成最终报告' : interrupted ? '研究已中断' : (cancelled ? '研究已停止' : plan.status === 'cancelling' ? '等待引擎确认停止' : '官方引擎正在研究');
    var note = failed ? (plan.agent_summary || '请核对已保存过程与错误，再决定是否创建新研究。') : interrupted ? '无法恢复原执行上下文，已保留现有过程。查看记录不会重复启动研究。' : (cancelled ? '已经保留此前的工具活动，可以从新任务重新开始。' : '最终报告完成前，这里只显示真实阶段，不提前展示未经核验的候选结论。');
    return '<div class="vibe-live-state ' + (failed ? 'failed' : '') + '"><i></i><h3>' + esc(title) + '</h3><p>' + esc(note) + '</p><strong>' + esc(plan.stage_label || '') + '</strong></div>';
  },

  _vibeReportHtml: function(plan) {
    var quality = plan.report_quality || plan.quality || {};
    var warnings = quality.warnings || [];
    var followups = plan.followups || [];
    var research = plan.report_research || (!(plan.followups || []).length ? plan.research : null);
    return (research ? ChatRender.renderResearch(research) : '<div class="research-context">原始研报的研究时点未保留；来源数据截至未知。</div>') +
      '<section class="vibe-report-quality"><div><span>来源边界</span><b>' + esc(quality.grade || '来源待核验') + '</b></div>' +
      '<p>' + (warnings.length ? warnings.map(function(item) { return esc(item); }).join(' · ') : '工具调用、正文来源与失败项已记录。') + '</p></section>' +
      '<article class="vibe-report-markdown">' + this._renderVibeMarkdown(plan.report_markdown || '') + '</article>' +
      (plan.report_markdown ? '<button class="vibe-save-version" onclick="App._saveVibeVersion()">保存原始报告版本</button>' : '') +
      this._vibeFollowupsHtml(followups) +
      (this._vibeIsActive(plan) ? '' : '<section class="vibe-followup-compose"><label for="vibeFollowupInput">继续追问这份研究</label><textarea id="vibeFollowupInput" rows="3" placeholder="例如：只保留存储芯片方向，重新核验江波龙和佰维存储的财务数字与来源。"></textarea><button onclick="App._submitVibeFollowup()">发送追问</button></section>');
  },

  _vibeFollowupsHtml: function(followups) {
    if (!followups.length) return '';
    return '<section class="vibe-followups"><h3>上下文追问</h3>' + followups.map(function(item, index) {
      var answer = item.answer_markdown ? App._renderVibeMarkdown(item.answer_markdown) : '<div class="vibe-followup-wait">' + (item.status === 'failed' ? esc(item.error || '追问失败') : '正在研究这个追问...') + '</div>';
      var save = item.id && item.status === 'complete' && item.answer_markdown ? '<button class="vibe-save-version" onclick="App._saveVibeFollowup(' + index + ')">保存此追问版本</button>' : '';
      return '<div class="vibe-followup-turn"><div class="vibe-followup-q"><span>' + (index + 1) + '</span><b>' + esc(item.question || '') + '</b></div><div class="vibe-followup-a">' + ChatRender.renderResearch(item.research) + answer + save + '</div></div>';
    }).join('') + '</section>';
  },

  _saveVibeFollowup: function(index) {
    var turn = ((this._activeVibePlan || {}).followups || [])[index];
    if (turn) return this._saveVibeVersion(turn.id);
  },

  _saveVibeVersion: async function(turnId) {
    var plan = this._activeVibePlan || {}, planId = this._activeVibePlanId;
    if (!planId || plan.id !== planId || this._vibeSaveBusy) return;
    var turn = turnId ? (plan.followups || []).find(function(item) { return item.id === turnId; }) : null;
    if (turnId ? !turn || turn.status !== 'complete' || !turn.answer_markdown : !plan.report_markdown) return;
    this._vibeSaveBusy = true;
    var selection = this._vibeSelectionSeq;
    try {
      var response = await this.api('/api/vibe/plans/' + encodeURIComponent(planId) + '/save-report', {
        method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(turnId ? {turn_id:turnId} : {})
      });
      if (!response.ok) throw new Error('save unavailable');
      var data = await response.json();
      if (planId !== this._activeVibePlanId || selection !== this._vibeSelectionSeq) return;
      if (!data.ok || !data.report || !data.report.id) throw new Error('missing saved version');
      this._toast('此版本已保存，原始研究时间保持不变');
      this.switchMode('reports');
      await this._openSavedReport(data.report.id);
    } catch (error) {
      if (planId === this._activeVibePlanId && selection === this._vibeSelectionSeq) this._toast('保存失败，报告仍保留在原研究记录中');
    } finally { this._vibeSaveBusy = false; }
  },

  _submitVibeFollowup: async function() {
    var plan = this._activeVibePlan || {};
    var input = document.getElementById('vibeFollowupInput');
    var question = input ? input.value.trim() : '';
    if (!plan.id || plan.id !== this._activeVibePlanId || !question || this._vibeFollowupBusy) return;
    this._vibeFollowupBusy = true;
    if (input) input.disabled = true;
    var response;
    try { response = await this.api('/api/vibe/plans/' + encodeURIComponent(plan.id) + '/follow-up', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({question:question})
    }); } catch (error) { response = {ok:false}; }
    this._vibeFollowupBusy = false;
    if (!response.ok) {
      if (input) input.disabled = false;
      this._toast('追问发送失败');
      return;
    }
    var updated = (await response.json()).plan;
    this._upsertVibePlan(updated);
    if (plan.id === this._activeVibePlanId) this._renderVibePlan(updated);
    this._renderVibeHistory();
    this._pollVibePlan(updated.id);
  },

  _renderVibeMarkdown: function(text) {
    if (!text) return '';
    var source = (window.ChatRender && ChatRender.stringify) ? ChatRender.stringify(text) : String(text || '');
    try {
      var html = (window.ChatRender && ChatRender.renderContent)
        ? ChatRender.renderContent(source)
        : sanitizeHtml(marked.parse(escapeAllGt(source)));
      return typeof html === 'string' ? html : '<pre>' + esc(source) + '</pre>';
    } catch (error) { return '<pre>' + esc(source) + '</pre>'; }
  },

  _vibeElapsed: function(seconds) {
    seconds = Math.max(0, Number(seconds || 0));
    if (seconds < 60) return Math.round(seconds) + ' 秒';
    return Math.floor(seconds / 60) + ' 分 ' + Math.round(seconds % 60) + ' 秒';
  },

  _vibeHorizonLabel: function(horizon) {
    return ({intraday:'日内', swing:'波段', position:'中线', long:'长期'})[horizon] || horizon;
  },

  _vibeAddWatch: async function(ticker, name) {
    var didAdd = await this._addToWatchlist(ticker, name);
    if (didAdd === null) return;
    var code = this._normalizeTicker ? this._normalizeTicker(ticker) : ticker;
    this._toast((didAdd ? '已加入自选：' : '已在自选：') + code);
  },

  _vibeAddAll: async function() {
    var list = (this._activeVibePlan || {}).candidates || [];
    var added = 0;
    for (var i = 0; i < list.length; i++) { var saved = await this._addToWatchlist(list[i].ticker, list[i].name); if (saved === null) return; if (saved) added++; }
    this._toast(added ? ('候选池已加入自选：新增 ' + added + ' 只') : '候选池已全部在自选中');
  },

  _vibeOpenMarket: function(ticker, name) {
    this.switchMode('market');
    this._selectStock(ticker, name);
  },

  _vibeToChat: function() {
    var plan = this._activeVibePlan || {};
    this.switchMode('chat');
    var input = document.getElementById('input');
    if (input) {
      input.value = '继续深入讨论这份 Vibe 研究：' + (plan.title || plan.prompt || '') + '\n重点结论：' + (plan.agent_summary || '');
      input.focus();
    }
  }
});
