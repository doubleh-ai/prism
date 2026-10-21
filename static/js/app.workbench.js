/* Prism research workspace. Navigation never starts a second research job. */
(function() {
  'use strict';
  var viewLabels = {chat:'研究对话',market:'行情工作区',vibe:'深度研究',analysis:'个股分析',reports:'网页研报',industry:'产业关系',indicators:'指标实验室',screener:'全市场选股',admin:'工作台管理'};
  var viewDescriptions = {chat:'能力调用、研究时间与来源会随本轮对话保留。',market:'查看行情与数据时点，保留从对话进入的查询记录。',vibe:'追踪正在进行的研究，继续阅读已生成的网页研报。',analysis:'查看个股分析过程与既有结果；打开记录不会重复启动任务。',reports:'网页阅读优先，保留原始来源与研究上下文。',industry:'区分产品用途与供货关系，查看证据及尚未证明的部分。',indicators:'检查指标定义、参数和历史评估记录。',screener:'查看筛选条件、执行进度与已保存结果。',admin:'账号、运行状态与诊断信息。'};
  var originalSwitchMode = App.switchMode;
  var originalMarketWorkspace = App._showMarketWorkspace;
  Object.assign(App, {
    _capabilities: [],
    _capabilityError: '',
    _capabilitiesLoaded: false,
    _platformCalls: Object.create(null),
    _workbenchCalls: [],
    _workbenchHistoryError: '',
    _workbenchHistoryLoading: false,
    _workbenchPanel: 'capabilities',

    _workbenchView: function() {
      if (this._mode === 'market') return this._marketWorkspaceTab === 'library' ? 'indicators' : this._marketWorkspaceTab === 'screener' ? 'screener' : 'market';
      return this._mode || 'chat';
    },
    _initWorkbench: function() {
      this._syncWorkbenchHeading(); this._loadCapabilities(); this._loadWorkbenchHistory();
      var self = this;
      document.addEventListener('click', function(event) {
        var target = event.target.closest('[data-platform-call], [data-call-conversation], [data-capability-view], [data-draft-question], [data-workbench-retry]');
        if (!target) return;
        if (target.dataset.platformCall) self._openPlatformCall(target.dataset.platformCall);
        else if (target.dataset.callConversation) self._returnToCallConversation(target.dataset.callConversation);
        else if (target.dataset.capabilityView) self._navigateTarget({view:target.dataset.capabilityView});
        else if (target.dataset.draftQuestion) self._draftResearchQuestion(target.dataset.draftQuestion);
        else if (target.dataset.workbenchRetry === 'capabilities') self._loadCapabilities();
        else self._loadWorkbenchHistory();
      });
      var dialog = document.getElementById('workbenchDialog');
      if (dialog) dialog.addEventListener('click', function(event) { if (event.target === dialog) self._closeWorkbenchPanel(); });
    },
    _syncWorkbenchHeading: function() {
      var view = this._workbenchView(), title = viewLabels[view] || '研究工作台';
      var heading = document.getElementById('workbenchPageTitle'); if (heading) heading.textContent = title;
      var sidebarTitle = document.getElementById('workbenchSidebarTitle'); if (sidebarTitle) sidebarTitle.textContent = view === 'chat' ? '研究会话' : title;
      var contextTitle = document.getElementById('workbenchContextTitle'); if (contextTitle) contextTitle.textContent = view === 'chat' ? '从对话开始研究' : '来自对话的能力调用';
      var description = document.getElementById('workbenchContextDescription'); if (description) description.textContent = viewDescriptions[view] || '';
      document.querySelectorAll('.icon-rail-btn').forEach(function(button) {
        var target = button.dataset.workbenchView || button.dataset.mode;
        button.classList.toggle('active', target === view);
        if (target === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
      });
    },
    _loadCapabilities: async function() {
      this._capabilityError = '';
      try {
        var response = await this.api('/api/capabilities');
        if (!response.ok) throw new Error('capability unavailable');
        var data = await response.json();
        this._capabilities = Array.isArray(data.capabilities) ? data.capabilities : [];
        this._capabilitiesLoaded = true;
      } catch(error) { this._capabilityError = '能力清单暂时无法读取，请重试。'; }
      if (!this.msgs.length && this._mode === 'chat') this.showWelcome();
      this._renderWorkbenchPanel();
    },
    _capabilityView: function(capability) {
      if (capability.target_view && viewLabels[capability.target_view]) return capability.target_view;
      var views = {quote:'market',market_quote:'market',market_search:'market',market_kline:'market',indicator_list:'indicators',indicator_evaluate:'indicators',indicator_eval:'indicators',industry_search:'industry',industry_node:'industry',industry_chain:'industry',report_list:'reports',report_read:'reports',analysis_start:'analysis',analysis_status:'analysis',vibe_start:'vibe',vibe_status:'vibe',scan_start:'screener',scan_status:'screener'};
      return views[capability.id] || '';
    },
    showWelcome: function() {
      var list = document.getElementById('msgList'); if (!list) return;
      var cards = [
        {view:'market',mark:'↗',title:'行情与筛选',text:'查看数据时点，比较行情与指标结果。'},
        {view:'industry',mark:'◇',title:'产业与证据',text:'梳理关系，区分已知事实与研究假设。'},
        {view:'reports',mark:'▤',title:'研究与研报',text:'继续已有研究，追溯来源与调用记录。'}
      ];
      var availability = this._capabilityError ? '<button class="workbench-inline-retry" data-workbench-retry="capabilities">'+esc(this._capabilityError)+'</button>' : '<span>'+(this._capabilitiesLoaded ? this._capabilities.length+' 项平台能力可供 Agent 按需调用' : '正在读取实际可用的平台能力…')+'</span>';
      list.innerHTML = '<div class="workbench-welcome"><div class="workbench-kicker">YOUR RESEARCH WORKSPACE</div><h2>从一个问题开始，<br>把研究过程连起来。</h2><p>在对话里明确目标。查看真实能力调用、结果与来源，<br class="desktop-only">也可以进入对应工作区继续探索。</p><div class="workbench-welcome-status"><i></i>'+availability+'</div><div class="workbench-entry-grid">'+cards.map(function(card){return '<button data-capability-view="'+card.view+'"><span class="workbench-entry-mark">'+card.mark+'</span><h3>'+card.title+'</h3><p>'+card.text+'</p><small>打开工作区 <span>↗</span></small></button>';}).join('')+'</div><div class="workbench-prompts"><span>试着从这里开始</span><button data-draft-question="请先查询可用的平台能力，再帮我建立一份区分事实、推断与假设的研究计划。">建立研究计划 ↗</button><button data-draft-question="请列出我已有的研究报告，说明研究时点、来源与仍待核验的问题。">梳理已有研报 ↗</button></div><div class="workbench-evidence-principle">采集成功不等于事实已核验 · 数据时点未知时明确标注</div></div>';
    },
    _draftResearchQuestion: function(text) {
      this.switchMode('chat');
      var input = document.getElementById('input'); input.value = text; input.focus();
    },
    _receivePlatformCall: function(call) {
      if (!call || !call.call_id) return;
      this._platformCalls[call.call_id] = call;
      var index = this._workbenchCalls.findIndex(function(item) { return item.call_id === call.call_id; });
      var view = this._workbenchView();
      var matches = view === 'chat' ? call.conv_id === this.convId : call.target && call.target.view === view;
      if (index >= 0) this._workbenchCalls[index] = call;
      else if (matches) this._workbenchCalls.push(call);
      this._updateWorkbenchCount(); this._renderWorkbenchPanel();
    },
    _renderPlatformCallHTML: function(call) {
      if (!call || !call.call_id) return '';
      this._platformCalls[call.call_id] = call;
      var status = {running:'进行中',completed:'已完成',failed:'未完成'}[call.status] || '状态未知';
      var label = call.label || call.capability_id || '平台能力';
      var view = call.target && call.target.view;
      return '<article class="platform-call-card" data-call-id="'+escAttr(call.call_id)+'"><header><span class="platform-call-status '+escAttr(['running','completed','failed'].indexOf(call.status)>=0?call.status:'unknown')+'">'+status+'</span><strong>'+esc(label)+'</strong></header><p>'+esc(ChatRender.stringify(call.result_summary || call.input_summary || '等待能力返回结果'))+'</p><div class="platform-call-meta">'+esc(ChatRender.formatTime(call.created_at))+' · 平台调用记录，非原始核验证据</div><footer>'+ (view && viewLabels[view] ? '<button data-platform-call="'+escAttr(call.call_id)+'">查看'+esc(viewLabels[view])+' ↗</button>' : '') + (call.conv_id ? '<button data-call-conversation="'+escAttr(call.conv_id)+'">返回原会话</button>' : '')+'</footer></article>';
    },
    _loadConversationCalls: function(id) { if (this.convId === id) this._loadWorkbenchHistory(); },
    _loadWorkbenchHistory: async function() {
      var sequence = this._workbenchHistorySeq = (this._workbenchHistorySeq || 0) + 1;
      var view = this._workbenchView(), convId = this.convId;
      this._workbenchHistoryError = ''; this._workbenchHistoryLoading = true;
      this._workbenchCalls = []; this._updateWorkbenchCount(); this._renderWorkbenchPanel();
      if (view === 'chat' && !convId) { this._workbenchHistoryLoading = false; this._renderWorkbenchPanel(); return; }
      var url = view === 'chat' ? '/api/convs/'+encodeURIComponent(convId)+'/capability-calls' : '/api/capability-calls?view='+encodeURIComponent(view)+'&limit=30';
      try {
        var response = await this.api(url); if (!response.ok) throw new Error('history unavailable');
        var data = await response.json();
        if (sequence !== this._workbenchHistorySeq) return;
        this._workbenchCalls = Array.isArray(data.calls) ? data.calls : [];
        this._workbenchCalls.forEach(function(call) { if (call.call_id) this._platformCalls[call.call_id] = call; }, this);
      } catch(error) { if(sequence === this._workbenchHistorySeq) this._workbenchHistoryError = '调用记录暂时无法加载；请重试，不代表没有历史。'; }
      if (sequence !== this._workbenchHistorySeq) return;
      this._workbenchHistoryLoading = false; this._updateWorkbenchCount(); this._renderWorkbenchPanel();
    },
    _updateWorkbenchCount: function() {
      var count = document.getElementById('workbenchCallCount'); if(count) count.textContent = this._workbenchHistoryError ? '!' : this._workbenchHistoryLoading ? '…' : String(this._workbenchCalls.length);
    },
    _openWorkbenchPanel: function(panel) {
      this._workbenchPanel = panel === 'history' ? 'history' : 'capabilities';
      var dialog = document.getElementById('workbenchDialog');
      if (dialog && !dialog.open) dialog.showModal();
      if (this._workbenchPanel === 'history') this._loadWorkbenchHistory();
      this._renderWorkbenchPanel();
    },
    _closeWorkbenchPanel: function() { var dialog = document.getElementById('workbenchDialog'); if (dialog && dialog.open) dialog.close(); },
    _renderWorkbenchPanel: function() {
      var dialog = document.getElementById('workbenchDialog'); if(!dialog || !dialog.open) return;
      var history = this._workbenchPanel === 'history';
      document.getElementById('workbenchDialogTitle').textContent = history ? '调用记录' : '平台能力';
      document.getElementById('workbenchCapabilityTab').setAttribute('aria-selected', String(!history));
      document.getElementById('workbenchHistoryTab').setAttribute('aria-selected', String(history));
      var html;
      if (history) {
        html = '<p class="workbench-panel-intro">'+(this._workbenchView()==='chat'?'当前会话':'当前工作区：'+esc(viewLabels[this._workbenchView()]||''))+' · 仅显示你有权访问的真实调用。打开结果不会再次执行任务。</p>';
        if (this._workbenchHistoryLoading) html += '<div class="workbench-empty">正在加载调用记录…</div>';
        else if (this._workbenchHistoryError) html += '<div class="workbench-empty">'+esc(this._workbenchHistoryError)+'<button data-workbench-retry="history">重新加载</button></div>';
        else if (!this._workbenchCalls.length) html += '<div class="workbench-empty">这里还没有对话产生的能力调用。<br>已有的手动任务与报告仍保留在各自工作区。</div>';
        else html += '<div class="workbench-call-list">'+this._workbenchCalls.slice().reverse().map(this._renderPlatformCallHTML.bind(this)).join('')+'</div>';
      } else {
        html = '<p class="workbench-panel-intro">这里列出后端实际提供给 Agent 的平台能力。请在对话中说明目标；打开工作区仅导航，不会自动启动研究。</p>';
        if (this._capabilityError) html += '<div class="workbench-empty">'+esc(this._capabilityError)+'<button data-workbench-retry="capabilities">重新加载</button></div>';
        else if (!this._capabilities.length) html += '<div class="workbench-empty">尚未读取到可用能力，请稍后重试。<button data-workbench-retry="capabilities">重新加载</button></div>';
        else html += '<div class="workbench-capability-grid">'+this._capabilities.map(function(capability) {
          var view = this._capabilityView(capability);
          return '<article><span class="capability-kind">'+(capability.read_only===false?'会创建任务':'查询与读取')+'</span><h3>'+esc(capability.label || capability.id)+'</h3><p>'+esc(capability.description || '')+'</p>'+(view?'<button data-capability-view="'+escAttr(view)+'">打开'+esc(viewLabels[view])+' ↗</button>':'')+'</article>';
        },this).join('')+'</div>';
      }
      document.getElementById('workbenchDialogBody').innerHTML = html;
    },
    _openPlatformCall: async function(id) {
      var call = this._platformCalls[id];
      if (!call || !call.target) { if(this._toast)this._toast('此调用没有可打开的结果记录'); return; }
      await this._navigateTarget(call.target);
    },
    _returnToCallConversation: async function(id) {
      this._closeWorkbenchPanel(); this.switchMode('chat'); await this.openConv(id);
      this._loadConversationCalls(id);
    },
    _navigateTarget: async function(target) {
      if (!target || !viewLabels[target.view] || target.view === 'admin') return;
      this._closeWorkbenchPanel();
      var view = target.view;
      try {
        if (view === 'indicators' || view === 'screener') {
          this._showMarketWorkspace(view==='indicators'?'library':'screener');
          if (view==='screener' && target.id) await this._openScanJob(target.id);
          if (view==='indicators' && target.id) { await this._loadIndicators(); this._openIndicatorEditor(target.id); }
        } else {
          this.switchMode(view);
          if (view==='market' && target.ticker) await this._selectStock(target.ticker);
          else if (view==='vibe' && target.id) await this._openVibePlan(target.id);
          else if (view==='reports' && target.id) await this._openSavedReport(target.id);
          else if (view==='industry' && target.id) await this._showIndustryNode(target.id);
          else if (view==='industry' && target.ticker) await this._selectIndustryTicker(target.ticker);
          else if (view==='analysis' && target.id && this._openAnalysisRecord) await this._openAnalysisRecord(target.id, target.ticker);
          else if (view==='analysis' && target.ticker) { this._analysisTicker = target.ticker; document.getElementById('analysisQ').value = target.ticker; }
        }
        this._closeMobileSidebar(); this._syncWorkbenchHeading(); this._loadWorkbenchHistory();
      } catch(error) { if(this._toast)this._toast('记录暂时无法打开，请刷新调用历史后重试'); }
    }
  });
  App.switchMode = function(mode) {
    originalSwitchMode.call(this, mode);
    this._syncWorkbenchHeading(); this._loadWorkbenchHistory();
    if (this._isCompactViewport()) this._closeMobileSidebar();
  };
  App._showMarketWorkspace = function(tab) {
    originalMarketWorkspace.call(this, tab);
    this._syncWorkbenchHeading(); this._loadWorkbenchHistory();
  };
})();
