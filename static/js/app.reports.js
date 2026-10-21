/* Prism - Saved report library */
Object.assign(App, {
  _savedReports: [],
  _activeSavedReportId: '',

  _initReports: function() {
    var shell = document.getElementById('appShell');
    if (shell) shell.classList.remove('collapsed');
    this._loadReportLibrary();
  },

  _loadReportLibrary: async function() {
    var sequence = this._reportLibrarySeq = (this._reportLibrarySeq || 0) + 1;
    var selection = this._reportSelectionSeq || 0;
    var side = document.getElementById('reportsSidebarList');
    var inner = document.getElementById('reportsPanelInner');
    if (side) side.innerHTML = '<div class="reports-lib-empty">加载中...</div>';
    if (inner && !this._activeSavedReportId) {
      inner.innerHTML = '<div class="market-empty-state">加载报告库中...</div>';
    }
    var r;
    try { r = await this.api('/api/reports/library?limit=80'); }
    catch (error) { r = {ok:false}; }
    if (sequence !== this._reportLibrarySeq) return;
    if (!r.ok) {
      if (side) side.innerHTML = '<div class="reports-lib-empty">报告库加载失败</div>';
      if (inner) inner.innerHTML = '<div class="market-empty-state">报告库加载失败</div>';
      return;
    }
    var data = await r.json();
    if (sequence !== this._reportLibrarySeq) return;
    this._savedReports = data.reports || [];
    this._renderReportSidebar();
    if (selection !== (this._reportSelectionSeq || 0)) return;
    if (this._activeSavedReportId) {
      this._openSavedReport(this._activeSavedReportId);
    } else {
      this._renderReportLibraryHome();
    }
  },

  _renderReportSidebar: function() {
    var side = document.getElementById('reportsSidebarList');
    if (!side) return;
    var reports = this._savedReports || [];
    var count = document.getElementById('reportsSidebarCount');
    if (count) count.textContent = reports.length ? '(' + reports.length + ')' : '';
    if (!reports.length) {
      side.innerHTML = '<div class="reports-lib-empty">暂无保存报告</div>';
      return;
    }
    var self = this;
    side.innerHTML = reports.map(function(rep) {
      var active = rep.id === self._activeSavedReportId ? ' active' : '';
      var sub = [rep.ticker || '', self._reportText(rep.signal || ''), self._fmtDate(rep.updated_at || rep.created_at)].filter(Boolean).join(' · ');
      return '<div class="reports-lib-side-item' + active + '" onclick="App._openSavedReport(\'' + escAttr(rep.id) + '\')">' +
        '<b>' + esc(self._reportText(rep.title || rep.ticker || 'Untitled')) + '</b>' +
        '<span>' + esc(sub || 'Saved report') + '</span>' +
        '</div>';
    }).join('');
  },

  _renderReportLibraryHome: function() {
    this._reportSelectionSeq = (this._reportSelectionSeq || 0) + 1;
    var inner = document.getElementById('reportsPanelInner');
    if (!inner) return;
    var reports = this._savedReports || [];
    if (!reports.length) {
      inner.innerHTML = '<div class="market-empty-state">完成一键分析后，可将结果保存到报告库</div>';
      return;
    }
    var self = this;
    inner.innerHTML = '<div class="reports-lib">' +
      '<div class="reports-lib-head"><div><h2>报告库</h2><p>沉淀一键分析结论，便于复盘、对比和后续追踪</p></div>' +
      '<button class="admin-secondary" onclick="App._loadReportLibrary()">刷新</button></div>' +
      '<div class="reports-lib-grid">' + reports.map(function(rep) {
        var sig = self._reportText(rep.signal || '') || '未评级';
        var cls = /buy|买入/i.test(sig) ? ' up' : /sell|卖出/i.test(sig) ? ' down' : '';
        return '<div class="reports-lib-card" onclick="App._openSavedReport(\'' + escAttr(rep.id) + '\')">' +
          '<div class="reports-lib-card-hd"><span>' + esc(rep.ticker || 'REPORT') + '</span><em class="' + cls + '">' + esc(sig) + '</em></div>' +
          '<h3>' + esc(self._reportText(rep.title || rep.name || rep.ticker || 'Untitled Report')) + '</h3>' +
          '<p>' + esc(self._reportText(rep.summary || '暂无摘要')) + '</p>' +
          '<div class="reports-lib-card-ft">' + esc(self._fmtDate(rep.updated_at || rep.created_at)) + '</div>' +
          '</div>';
      }).join('') + '</div></div>';
  },

  _openSavedReport: async function(id) {
    if (!id) return;
    var sequence = this._reportSelectionSeq = (this._reportSelectionSeq || 0) + 1;
    this._activeSavedReportId = id;
    this._closeMobileSidebar();
    this._renderReportSidebar();
    var inner = document.getElementById('reportsPanelInner');
    if (inner) inner.innerHTML = '<div class="market-empty-state">加载报告...</div>';
    var r;
    try { r = await this.api('/api/reports/library/' + encodeURIComponent(id)); }
    catch (error) { r = {ok:false,status:0}; }
    if (sequence !== this._reportSelectionSeq || id !== this._activeSavedReportId) return;
    if (!r.ok) {
      if (inner) inner.innerHTML = '<div class="market-empty-state">'+(r.status === 403 || r.status === 404 ? '报告不存在或无权限访问' : '报告读取暂时失败，请重新选择该报告重试；保存内容未改变')+'</div>';
      return;
    }
    var rep = await r.json();
    if (sequence !== this._reportSelectionSeq || id !== this._activeSavedReportId) return;
    this._renderSavedReportDetail(rep);
  },

  _renderSavedReportDetail: function(rep) {
    var inner = document.getElementById('reportsPanelInner');
    if (!inner) return;
    var report = (rep.report && typeof rep.report === 'object') ? rep.report : {};
    var structured = (report.structured && typeof report.structured === 'object') ? report.structured : {};
    var sig = this._reportText(rep.signal || structured.signal || report.signal || '') || '未评级';
    sig = sig.split('\n')[0].slice(0, 32);
    var cls = /buy|买入/i.test(sig) ? ' up' : /sell|卖出/i.test(sig) ? ' down' : '';
    var title = this._reportText(rep.title || report.title || report.question || report.name || rep.ticker || '') || 'Untitled Report';
    var finalDecision = this._reportText(report.final_decision || report.decision || report.result || '');
    var deliveredDecision = this._reportDeliveredText(finalDecision);
    var incompleteChat = this._reportIsIncompleteChat(rep, report, finalDecision, deliveredDecision);
    var summary = incompleteChat
      ? '研究过程已保存，但任务在形成最终报告前中止。可返回原对话继续完成分析。'
      : this._reportSummaryText(deliveredDecision || rep.summary || structured.thesis || structured.verdict_summary || report.summary || report.question || finalDecision || '');
    var research = report.research || rep.research;
    var provenance = '<section class="reports-lib-section report-provenance"><h4>版本与研究边界</h4>' +
      '<p>保存版本：'+esc(String(rep.revision || report.revision || '历史记录'))+' · 保存于 '+esc(ChatRender.formatTime(rep.created_at))+'</p>' +
      (research ? ChatRender.renderResearch(research) : '<p>历史报告未记录可靠的研究时点；数据截至未知。</p>') +
      '<p>采集、引用匹配与事实核验是不同环节。原引擎结论保留，未提供证据的表述仍待核验。</p></section>';
    var body = '';

    var thesis = this._reportText(structured.thesis || structured.verdict_summary || report.thesis || report.summary || '');
    if (thesis || structured.metrics) {
      body += '<div class="reports-lib-section reports-lib-thesis">' +
        '<h4>核心结论</h4>' +
        (thesis ? '<div class="reports-lib-md">' + this._safeReportMarkdown(thesis) + '</div>' : '<p>暂无结构化结论。</p>') +
        '</div>';
      if (structured.metrics && typeof structured.metrics === 'object') {
        var m = structured.metrics;
        body += '<div class="reports-lib-metrics">' +
          this._reportMetric('估值', m.valuation) +
          this._reportMetric('增长', m.growth) +
          this._reportMetric('风险', m.risk_score === undefined ? '' : m.risk_score) +
          this._reportMetric('动量', m.momentum) +
          '</div>';
      }
      if (structured.trade && typeof structured.trade === 'object') {
        var tr = structured.trade;
        body += '<div class="reports-lib-section"><h4>交易方案</h4>' +
          '<div class="reports-lib-trade">' +
          this._reportMetric('目标价', tr.target_price) +
          this._reportMetric('止损', tr.stop_loss) +
          this._reportMetric('仓位', tr.position) +
          this._reportMetric('周期', tr.horizon) +
          '</div><div class="reports-lib-md">' + this._safeReportMarkdown(tr.rationale || '') + '</div></div>';
      }
    }

    if (incompleteChat) {
      body += '<div class="reports-lib-section reports-lib-incomplete">' +
        '<div><h4>研究未完成</h4><p>这次 Agent 任务在生成最终结论前结束，下面保存的是研究过程，不应作为投资结论使用。</p></div>' +
        (rep.conv_id ? '<button class="admin-secondary" onclick="App.switchMode(\'chat\');App.openConv(\'' + escAttr(rep.conv_id) + '\')">返回对话继续</button>' : '') +
        '</div>';
      if (finalDecision) {
        body += '<details class="reports-lib-section reports-lib-process"><summary>查看已保存的研究过程</summary><div class="reports-lib-md">' +
          this._safeReportMarkdown(finalDecision) + '</div></details>';
      }
    } else if (deliveredDecision || finalDecision) {
      body += '<div class="reports-lib-section"><h4>最终决策</h4><div class="reports-lib-md">' +
        this._safeReportMarkdown(deliveredDecision || finalDecision) + '</div></div>';
    }
    var traderPlan = this._reportText(report.trader_plan || report.trade_plan || report.plan || '');
    if (traderPlan) {
      body += '<div class="reports-lib-section"><h4>交易计划</h4><div class="reports-lib-md">' +
        this._safeReportMarkdown(traderPlan) + '</div></div>';
    }

    var analysts = (report.analyst_reports && typeof report.analyst_reports === 'object') ? report.analyst_reports : {};
    var keys = Object.keys(analysts);
    if (keys.length) {
      body += '<div class="reports-lib-section"><h4>分析师研报</h4>';
      for (var i = 0; i < keys.length; i++) {
        var analystText = this._reportText(analysts[keys[i]]);
        if (!analystText) continue;
        body += '<details class="reports-lib-fold"><summary>' + esc(keys[i]) + '</summary><div class="reports-lib-md">' +
          this._safeReportMarkdown(analystText) + '</div></details>';
      }
      body += '</div>';
    }

    var references = Array.isArray(report.references) ? report.references : [];
    if (references.length) {
      var self = this;
      body += '<div class="reports-lib-section"><h4>采集来源与线索</h4><div class="reports-lib-ref-list">' +
        references.map(function(ref) {
          if (!ref || typeof ref !== 'object') return '<div class="reports-lib-ref">'+esc(self._reportText(ref))+'</div>';
          return ChatRender.renderChunk(Object.assign({}, ref, {t:'ref'}));
        }).join('') + '</div></div>';
    }

    if (!body) {
      var fallback = this._reportText(report.markdown || report.text || report.content || report);
      body = '<div class="reports-lib-section"><h4>原始报告</h4>' +
        (fallback ? '<div class="reports-lib-md">' + this._safeReportMarkdown(fallback) + '</div>' :
          '<pre class="reports-lib-json">' + esc(JSON.stringify(report, null, 2)) + '</pre>') +
        '</div>';
    }

    inner.innerHTML = '<div class="reports-lib reports-lib-detail">' +
      '<div class="reports-lib-detail-head">' +
        '<button class="admin-secondary" onclick="App._activeSavedReportId=\'\';App._renderReportLibraryHome();App._renderReportSidebar()">返回列表</button>' +
        '<div class="reports-lib-detail-actions"><button class="admin-danger" onclick="App._deleteSavedReport(\'' + escAttr(rep.id) + '\')">删除</button></div>' +
      '</div>' +
      '<div class="reports-lib-hero reports-lib-hero-detail">' +
        '<div class="reports-lib-hero-main"><span class="reports-lib-kicker">' + esc(rep.ticker || rep.source || 'REPORT') + '</span>' +
        '<h2>' + esc(title) + '</h2>' +
        (summary ? '<p>' + esc(summary) + '</p>' : '') + '</div>' +
        '<div class="reports-lib-signal' + cls + '">' + esc(sig) + '</div>' +
      '</div>' + provenance + body + '</div>';
  },

  _deleteSavedReport: async function(id) {
    if (!confirm('删除这份保存的报告？')) return;
    var r = await this.api('/api/reports/library/' + encodeURIComponent(id), {method: 'DELETE'});
    if (!r.ok) { this._toast && this._toast('删除失败'); return; }
    this._activeSavedReportId = '';
    this._toast && this._toast('报告已删除');
    this._loadReportLibrary();
  },

  _saveAnalysisReport: async function() {
    if (!this._analysisId) { this._toast && this._toast('没有可保存的分析报告'); return; }
    var title = '';
    if (this._analysisTicker) title = this._analysisTicker + (this._analysisName ? ' ' + this._analysisName : '') + ' 分析报告';
    var r = await this.api('/api/analysis/' + encodeURIComponent(this._analysisId) + '/save-report', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({title: title})
    });
    if (!r.ok) {
      var msg = '保存失败';
      try { var e = await r.json(); msg = e.detail || e.error || msg; } catch(ex) {}
      this._toast && this._toast(msg);
      return;
    }
    var saved = (await r.json()).report;
    this._toast && this._toast('已保存到报告库');
    this._activeSavedReportId = saved.id;
    this._loadReportLibrary();
  },

  _reportMetric: function(k, v) {
    return '<div class="reports-lib-metric"><span>' + esc(k) + '</span><b>' + esc(this._reportText(v) || '-') + '</b></div>';
  },

  _reportText: function(value) {
    function clean(t) {
      return String(t || '')
        .replace(/\[object Object\]/g, '')
        .replace(/\r\n/g, '\n')
        .replace(/[ \t]+\n/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
    }
    function from(v) {
      if (v === null || v === undefined) return '';
      if (typeof v === 'string') return clean((window.ChatRender && ChatRender.clean) ? ChatRender.clean(v) : v);
      if (typeof v === 'number' || typeof v === 'boolean') return String(v);
      if (Array.isArray(v)) return clean(v.map(from).filter(Boolean).join('\n'));
      if (typeof v === 'object') {
        if (v.type === 'text' && v.text !== undefined) return from(v.text);
        if (v.type === 'thinking' && v.thinking !== undefined) return from(v.thinking);
        if (v.type === 'tool_result' && v.content !== undefined) return from(v.content);
        if (v.type === 'tool_use') return '';
        if (v.message && v.message.content !== undefined) return from(v.message.content);
        var preferred = ['markdown', 'text', 'content', 'summary', 'thesis', 'verdict_summary', 'final_decision', 'rationale', 'reason', 'decision', 'plan', 'value', 'description', 'output'];
        for (var i = 0; i < preferred.length; i++) {
          if (v[preferred[i]] !== undefined) {
            var picked = from(v[preferred[i]]);
            if (picked) return picked;
          }
        }
        var parts = [];
        Object.keys(v).forEach(function(key) {
          if (/^(id|type|name|role|created_at|updated_at|source_id|conv_id)$/i.test(key)) return;
          var text = from(v[key]);
          if (text) parts.push(/^[\w\u4e00-\u9fa5 -]{1,24}$/.test(key) ? key + ': ' + text : text);
        });
        if (parts.length) return clean(parts.join('\n'));
        return '';
      }
      return clean(String(v));
    }
    return clean(from(value));
  },

  _safeReportMarkdown: function(text) {
    text = this._reportText(text);
    if (!text) return '';
    var html = '';
    try {
      if (typeof ChatRender !== 'undefined' && ChatRender.renderContent) html = ChatRender.renderContent(text);
      else if (typeof render === 'function') html = render(text);
      else if (this._renderMarkdown) html = this._renderMarkdown(text);
      else html = sanitizeHtml(marked.parse(escapeAllGt(text)));
    } catch(e) {
      html = '';
    }
    html = this._reportHtmlString(html);
    return html || '<p>' + esc(text) + '</p>';
  },

  _reportHtmlString: function(value) {
    if (value && typeof value === 'object' && typeof value.html === 'string') value = value.html;
    if (typeof value !== 'string') return '';
    value = value.replace(/\[object Object\]/g, '').trim();
    return typeof sanitizeHtml === 'function' ? sanitizeHtml(value) : value;
  },

  _reportDeliveredText: function(value) {
    var text = this._reportText(value);
    if (!text) return '';
    var marker = /^#{1,4}\s*(?:报告已交付|最终报告|最终结论|投资结论|分析结论|核心发现)(?:\s.*)?$/gim;
    var hit = null, current;
    while ((current = marker.exec(text)) !== null) hit = current;
    return hit ? text.slice(hit.index).trim() : '';
  },

  _reportIsIncompleteChat: function(rep, report, finalDecision, deliveredDecision) {
    if (!rep || rep.source !== 'chat') return false;
    if (report.status === 'complete') return false;
    if (report.status === 'incomplete') return true;
    if (deliveredDecision) return false;
    if (/API Error|operation timed out|timed out|超时|中止|失败\s*$/i.test(finalDecision || '')) return true;
    return !!report.skill;
  },

  _reportSummaryText: function(value) {
    var text = this._reportText(value);
    if (!text) return '';
    return text.replace(/```[\s\S]*?```/g, ' ')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/^[#>*+\-\d.\s]+/gm, '')
      .replace(/[*_`~]/g, '')
      .replace(/\s+/g, ' ')
      .trim()
      .slice(0, 240);
  },

  _fmtDate: function(ts) {
    if (!ts) return '';
    var d = new Date(ts * 1000);
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0') + ' ' + String(d.getHours()).padStart(2, '0') + ':' +
      String(d.getMinutes()).padStart(2, '0');
  }
});
