/* ═══════════════════════════════════════
   Prism — Analysis Mode (v0.19.7 astock)
   ═══════════════════════════════════════ */
Object.assign(App, {

  _analysisId: null,
  _analysisStreaming: false,
  _analysisReader: null,
  _analysisEvents: [],
  _analysisStartTime: 0,
  _analysisCurrentStage: "",
  _analysisProgressPct: 0,
  _analysisAttachAttempted: false,
  _analysisMode: "fast",
  _analysisSelectedAgents: ["market", "fundamentals"],

  _analysisStageLabels: {
    analysts: "分析师研报", debate: "多空辩论", research: "研判整合",
    trader: "交易方案", risk: "风险评估", decision: "最终决策"
  },
  _analysisStageIcons: {
    analysts: "🔬", debate: "⚔️", research: "📋",
    trader: "📊", risk: "🛡️", decision: "🎯"
  },
  _analysisStageOrder: ["analysts", "debate", "research", "trader", "risk", "decision"],
  _analysisAgentLabels: {
    market: "市场分析师", social: "舆情分析师", news: "新闻分析师",
    fundamentals: "基本面分析师", policy: "政策分析师",
    hot_money: "游资追踪师", lockup: "解禁监控师"
  },
  _analysisModeLabels: {
    fast: "快速分析",
    deep: "深度分析"
  },
  _analysisAgentDone: {},

  _setAnalysisMode: function(mode) {
    mode = mode === "deep" ? "deep" : "fast";
    this._analysisMode = mode;
    var input = document.getElementById("analysisMode");
    if (input) input.value = mode;
    var buttons = document.querySelectorAll(".analysis-mode-btn");
    for (var i = 0; i < buttons.length; i++) {
      var active = buttons[i].getAttribute("data-mode") === mode;
      buttons[i].classList.toggle("active", active);
      buttons[i].setAttribute("aria-pressed", active ? "true" : "false");
    }
  },

  /* ── 一键分析 (astock) ── */
  _runAnalysis: async function() {
    if (!this._analysisTicker) { alert("请先搜索并选择一只股票"); return; }
    if (this._analysisStreaming) { this._stopAnalysis(); return; }

    var ticker = this._analysisTicker, name = this._analysisName;
    var modeEl = document.getElementById("analysisMode");
    var forceEl = document.getElementById("analysisForceRefresh");
    var mode = (modeEl && modeEl.value) || "fast";
    var forceRefresh = !!(forceEl && forceEl.checked);
    this._analysisMode = mode;
    this._analysisSelectedAgents = this._analysisAgentsForMode(mode);
    this._analysisAgentDone = {};
    this._analysisEvents = [];
    this._analysisCurrentStage = "";
    this._analysisProgressPct = 0;
    this._analysisStartTime = Date.now();

    document.getElementById("analysisPanelInner").innerHTML =
      '<div class="ap-container">' +
        this._buildProgressBar() +
        '<div class="ap-status" id="apStatus">正在启动' + esc(this._analysisModeLabels[mode] || "分析") + '…</div>' +
        '<div class="ap-agent-grid" id="apAgentGrid"></div>' +
        '<div class="ap-stream" id="apStream"><div class="ap-stream-hd" onclick="App._toggleStream()">🧠 分析过程 <span class="ap-stream-cnt"></span></div><div class="ap-stream-body" id="apStreamBody"></div></div>' +
      '</div>' +
      '<div class="ap-report" id="apReport" style="display:none"></div>';

    this._renderAgentGrid();

    var r = await this.api("/api/analysis/start", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ticker: ticker, name: name, mode: mode, force_refresh: forceRefresh})
    });
    if (!r.ok) {
      document.getElementById("analysisPanelInner").innerHTML =
        '<div class="market-empty-state">启动分析失败，请重试</div>';
      return;
    }
    var data = await r.json();
    this._analysisId = data.analysis_id;
    this._analysisMode = data.mode || mode;
    this._setAnalysisMode(this._analysisMode);
    this._analysisSelectedAgents = (data.selected_analysts && data.selected_analysts.length)
      ? data.selected_analysts
      : this._analysisAgentsForMode(this._analysisMode);
    this._renderAgentGrid();
    this._analysisStreaming = true;
    this._saveAnalysisState();
    this._connectAnalysisStream();
  },

  _stopAnalysis: function() {
    this._analysisStreaming = false;
    if (this._analysisReader) { this._analysisReader.abort(); this._analysisReader = null; }
    this._analysisId = null;
    try { sessionStorage.removeItem('prism_analysis'); } catch(e) {}
    document.getElementById("analysisPanelInner").innerHTML =
      '<div class="market-empty-state">分析已取消</div>';
  },

  /* ── Progress bar ── */
  _buildProgressBar: function() {
    var stages = "";
    for (var si = 0; si < this._analysisStageOrder.length; si++) {
      var s = this._analysisStageOrder[si];
      stages += '<div class="ap-stage" data-stage="' + s + '">' +
        '<span class="ap-stage-dot">' + (this._analysisStageIcons[s] || '○') + '</span>' +
        '<span class="ap-stage-label">' + (this._analysisStageLabels[s] || s) + '</span>' +
        '</div>';
    }
    return '<div class="ap-progress-wrap">' +
      '<div class="ap-progress-hd"><div class="ap-progress-bar"><div class="ap-progress-fill" id="apProgressFill" style="width:0%"></div></div>' +
      '<div class="ap-progress-pct" id="apProgressPct">0%</div></div>' +
      '<div class="ap-stage-row">' + stages + '</div>' +
      '<div class="ap-elapsed" id="apElapsed">准备中…</div>' +
      '</div>';
  },

  _renderAgentGrid: function() {
    var grid = document.getElementById("apAgentGrid");
    if (!grid) return;
    var agents = (this._analysisSelectedAgents && this._analysisSelectedAgents.length)
      ? this._analysisSelectedAgents
      : this._analysisAgentsForMode(this._analysisMode);
    var html = "";
    for (var i = 0; i < agents.length; i++) {
      var a = agents[i], done = this._analysisAgentDone[a];
      html += '<div class="ap-agent' + (done ? ' done' : '') + '" data-agent="' + a + '">' +
        '<span class="ap-agent-icon">' + (done ? '<span class="ap-check">✓</span>' : '<span class="ap-spinner"></span>') + '</span>' +
        '<span class="ap-agent-label">' + (this._analysisAgentLabels[a] || a) + '</span>' +
        '</div>';
    }
    grid.innerHTML = html;
  },

  _analysisAgentsForMode: function(mode) {
    return mode === "deep"
      ? ["market","social","news","fundamentals","policy","hot_money","lockup"]
      : ["market","fundamentals"];
  },

  _analysisEventLabel: function(agent, node, label) {
    var title = label || this._analysisAgentLabels[agent] || node || agent || "";
    if (/^messages$/i.test(title)) return "";
    return title;
  },

  _updateProgress: function(pct) {
    this._analysisProgressPct = Math.max(this._analysisProgressPct, pct);
    var fill = document.getElementById("apProgressFill");
    if (fill) fill.style.width = this._analysisProgressPct + "%";
    var pctEl = document.getElementById("apProgressPct");
    if (pctEl) pctEl.textContent = this._analysisProgressPct + "%";
    // Highlight active stage
    var stageEls = document.querySelectorAll(".ap-stage");
    var found = false;
    for (var si = stageEls.length - 1; si >= 0; si--) {
      var s = stageEls[si].dataset.stage;
      if (!found && this._analysisStageOrder.indexOf(s) <= this._analysisStageOrder.indexOf(this._analysisCurrentStage)) {
        stageEls[si].classList.add("active");
      }
      if (s === this._analysisCurrentStage) found = true;
    }
    // Update elapsed
    this._tickElapsed();
  },

  _tickElapsed: function() {
    var el = document.getElementById("apElapsed");
    if (el && this._analysisStartTime) {
      var secs = Math.round((Date.now() - this._analysisStartTime) / 1000);
      el.textContent = "已用 " + (secs >= 60 ? Math.floor(secs/60) + "分" + (secs%60) + "秒" : secs + "秒");
    }
  },

  /* ── Live stream of agent outputs ── */
  _appendStreamItem: function(agent, node, text, label) {
    var body = document.getElementById("apStreamBody");
    if (!body || !text) return;
    var title = this._analysisEventLabel(agent, node, label);
    if (!title) return;
    var item = document.createElement("div");
    item.className = "ap-stream-item";
    item.innerHTML = '<div class="ap-stream-item-hd"><b>' + esc(title) + '</b></div>' +
      '<div class="ap-stream-item-txt">' + esc((text || "").slice(0, 450)) + '</div>';
    body.appendChild(item);
    while (body.children.length > 12) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
    var cnt = document.querySelector(".ap-stream-cnt");
    if (cnt) cnt.textContent = (body.children.length) + " 条";
    var s = document.getElementById("apStream");
    if (s) s.classList.remove("folded");
  },

  _appendTimingItem: function(ev) {
    var body = document.getElementById("apStreamBody");
    if (!body || !ev || !ev.node) return;
    var title = this._analysisEventLabel(ev.agent, ev.node, ev.label);
    if (!title) return;
    var item = document.createElement("div");
    item.className = "ap-stream-item ap-stream-item--timing";
    item.innerHTML = '<div class="ap-stream-item-hd"><b>' + esc(title) + '</b><span>' +
      esc((ev.duration || 0) + "s") + '</span></div>';
    body.appendChild(item);
    while (body.children.length > 12) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
    var cnt = document.querySelector(".ap-stream-cnt");
    if (cnt) cnt.textContent = (body.children.length) + " 条";
  },

  _toggleStream: function() {
    var s = document.getElementById("apStream");
    if (s) s.classList.toggle("folded");
  },

  /* ── SSE ── */
  _connectAnalysisStream: function() {
    var self = this, aid = this._analysisId;
    if (!aid) return;
    fetch("/api/analysis/" + aid + "/stream").then(function(response) {
      var reader = response.body.getReader();
      self._analysisReader = {abort: function() { reader.cancel(); }};
      var dec = new TextDecoder(), buf = "";
      function pump() {
        if (!self._analysisStreaming) return;
        reader.read().then(function(r) {
          if (r.done) return;
          buf += dec.decode(r.value, {stream: true});
          var parts = buf.split("\n\n"); buf = parts.pop();
          for (var i = 0; i < parts.length; i++) {
            var line = parts[i].trim();
            if (!line || line.indexOf("data: ") !== 0) continue;
            try { self._handleAnalysisEvent(JSON.parse(line.substring(6))); } catch(e) {}
          }
          pump();
        }).catch(function() { self._analysisStreaming = false; });
      }
      pump();
    }).catch(function() { self._analysisStreaming = false; });
  },

  _handleAnalysisEvent: function(ev) {
    if (ev && ev.type !== "heartbeat") this._analysisEvents.push(ev);
    switch (ev.type) {
      case "heartbeat":
        // Backend keep-alive — tick the elapsed timer so the UI shows the
        // analysis is alive even between node completions.
        this._tickElapsed();
        break;
      case "status":
        // Free-form status line (e.g. "正在拉取行情数据…")
        var stEl = document.getElementById("apStatus");
        if (stEl && ev.message) stEl.textContent = ev.message;
        this._tickElapsed();
        break;
      case "slow":
        // Watchdog: data source is stalling. Reassure the user it's not dead.
        var swEl = document.getElementById("apStatus");
        if (swEl && ev.message) swEl.textContent = "⏳ " + ev.message;
        this._tickElapsed();
        break;
      case "stage":
        this._analysisCurrentStage = ev.stage || "";
        this._updateProgress(ev.progress_pct || 0);
        var status = document.getElementById("apStatus");
        if (status) status.textContent = (this._analysisStageIcons[ev.stage] || "") + " " +
          (this._analysisStageLabels[ev.stage] || ev.stage) + " — 分析中…";
        this._saveAnalysisState();
        break;
      case "agent_done":
        if (ev.agent && ev.agent !== "messages") this._analysisAgentDone[ev.agent] = true;
        this._renderAgentGrid();
        this._saveAnalysisState();
        break;
      case "agent_output":
        this._appendStreamItem(ev.agent, ev.node, ev.text, ev.label);
        break;
      case "timing":
        this._appendTimingItem(ev);
        break;
      case "done":
        this._analysisStreaming = false;
        this._updateProgress(100);
        var s = document.getElementById("apStatus");
        if (s) {
          var total = ev.timings && ev.timings.total_seconds ? "，耗时 " + ev.timings.total_seconds + "s" : "";
          s.textContent = ev.cached ? "使用缓存报告" : ("分析完成" + total);
        }
        // Fold the live-stream panel — the final dashboard now shows the synthesized result.
        var stream = document.getElementById("apStream");
        if (stream) stream.classList.add("folded");
        if (ev.mode) this._setAnalysisMode(ev.mode);
        if (ev.report && ev.report._analysis_meta && ev.report._analysis_meta.selected_analysts) {
          this._analysisSelectedAgents = ev.report._analysis_meta.selected_analysts;
        }
        this._lastReport = ev.report;
        this._lastSignal = ev.signal;
        this._renderFinalReport(ev.report, ev.signal);
        try { sessionStorage.removeItem('prism_analysis'); } catch(e) {}
        break;
      case "structured":
        // Dashboard upgrade — structured extraction arrived after done.
        // Re-render the report area as a full dashboard.
        if (ev.structured) {
          if (this._lastReport) this._lastReport.structured = ev.structured;
          else this._lastReport = {structured: ev.structured};
          this._renderFinalReport(this._lastReport, this._lastSignal);
        }
        break;
      case "error":
        this._analysisStreaming = false;
        try { sessionStorage.removeItem('prism_analysis'); } catch(e) {}
        document.getElementById("analysisPanelInner").innerHTML =
          '<div class="market-empty-state">分析失败: ' + esc(ev.message || "未知错误") + '</div>';
        break;
    }
  },

  /* ── Final report ── */
  _analysisTextValue: function(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return value.replace(/^\s*\[object Object\]\s*$/gm, "").trim();
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (Array.isArray(value)) {
      return value.map(function(x) { return App._analysisTextValue(x); }).filter(Boolean).join("\n").trim();
    }
    if (typeof value === "object") {
      var keys = ["text", "content", "result", "output", "message", "summary", "thesis",
        "rationale", "verdict", "final_decision", "final_trade_decision", "trader_investment_plan"];
      for (var i = 0; i < keys.length; i++) {
        if (value[keys[i]] !== undefined) {
          var hit = App._analysisTextValue(value[keys[i]]);
          if (hit) return hit;
        }
      }
      return Object.keys(value).map(function(k) {
        return App._analysisTextValue(value[k]);
      }).filter(Boolean).join("\n").trim();
    }
    return String(value || "").replace(/^\s*\[object Object\]\s*$/gm, "").trim();
  },

  _analysisMetaHtml: function(report) {
    var meta = report && report._analysis_meta;
    if (!meta) return "";
    var agents = (meta.selected_analysts || []).map(function(a) {
      return App._analysisAgentLabels[a] || a;
    }).join("、");
    var total = meta.timings && meta.timings.total_seconds ? meta.timings.total_seconds + "s" : "";
    var bits = [
      this._analysisModeLabels[meta.mode] || meta.mode || "分析",
      meta.cached ? "缓存命中" : "实时生成",
      meta.trade_date ? "交易日 " + meta.trade_date : "",
      agents ? "Agent " + agents : "",
      total ? "耗时 " + total : ""
    ].filter(Boolean);
    return '<div class="ap-meta">' + bits.map(function(b) {
      return '<span>' + esc(b) + '</span>';
    }).join("") + '</div>';
  },

  _renderTextReport: function(report, signal) {
    var container = document.getElementById("apReport");
    if (!container) return;
    container.style.display = "block";

    var rating = this._analysisTextValue(signal || (report && report.signal)) || "—";
    var rc = /buy/i.test(rating) ? "green" : /sell/i.test(rating) ? "red" : "yellow";
    var html = this._analysisMetaHtml(report) +
      '<div class="ar-card ar-rating ar-rating--' + rc + '">' +
      '<div class="ar-rating-badge">' + rating + '</div>' +
      '<div class="ar-rating-sub">投资评级</div></div>';

    var decision = report ? this._analysisTextValue(report.final_decision || "") : "";
    if (decision) {
      html += '<div class="ar-card"><h4>📝 投资决策</h4><div class="ar-body">' + App._renderMarkdown(decision) + '</div></div>';
    }

    // Analyst reports — tabbed
    var ar = report ? (report.analyst_reports || {}) : {};
    var arKeys = Object.keys(ar);
    if (arKeys.length) {
      html += '<div class="ar-card"><h4>🔬 分析师研报</h4>';
      for (var ai = 0; ai < arKeys.length; ai++) {
        var ak = arKeys[ai];
        html += '<details class="ar-fold"><summary>' + esc(this._analysisAgentLabels[ak] || ak) + '</summary>' +
          '<div class="ar-body">' + App._renderMarkdown(ar[ak] || "") + '</div></details>';
      }
      html += '</div>';
    }

    // Debate
    var debate = report ? (report.investment_debate || {}) : {};
    if (debate.bull_history || debate.bear_history) {
      html += '<div class="ar-card"><h4>⚔️ 多空辩论</h4>';
      if (debate.bull_history) html += '<details class="ar-fold"><summary>📈 多方</summary><div class="ar-body">' + App._renderMarkdown(debate.bull_history) + '</div></details>';
      if (debate.bear_history) html += '<details class="ar-fold"><summary>📉 空方</summary><div class="ar-body">' + App._renderMarkdown(debate.bear_history) + '</div></details>';
      html += '</div>';
    }

    // Trader
    var tp = report ? this._analysisTextValue(report.trader_plan || "") : "";
    if (tp) html += '<div class="ar-card"><h4>📊 交易方案</h4><div class="ar-body">' + App._renderMarkdown(tp) + '</div></div>';

    // Risk
    var risk = report ? (report.risk_debate || {}) : {};
    if (risk.aggressive || risk.conservative) {
      html += '<div class="ar-card"><h4>🛡️ 风险评估</h4>';
      if (risk.aggressive) html += '<details class="ar-fold"><summary>🟢 激进方</summary><div class="ar-body">' + App._renderMarkdown(risk.aggressive) + '</div></details>';
      if (risk.conservative) html += '<details class="ar-fold"><summary>🔴 保守方</summary><div class="ar-body">' + App._renderMarkdown(risk.conservative) + '</div></details>';
      html += '</div>';
    }

    html += '<div class="ap-report-actions">' +
      '<button class="analysis-run-btn" onclick="App._saveAnalysisReport()">保存到报告库</button>' +
      '<button class="analysis-run-btn" onclick="App._addAnalysisToChat()">添加到对话</button>' +
      '</div>';
    container.innerHTML = html;
    container.scrollIntoView({behavior: "smooth"});
  },

  _renderFinalReport: function(report, signal) {
    if (!(report && report.structured)) { this._renderTextReport(report, signal); return; }
    var container = document.getElementById("apReport");
    if (!container) return;
    container.style.display = "block";
    container.innerHTML = this._analysisMetaHtml(report) +
      this._renderDashboard(report.structured, report, signal) +
      '<div class="ap-report-actions">' +
      '<button class="analysis-run-btn" onclick="App._saveAnalysisReport()">保存到报告库</button>' +
      '<button class="analysis-run-btn" onclick="App._addAnalysisToChat()">添加到对话</button>' +
      '</div>';
    container.scrollIntoView({behavior: "smooth"});
  },

  _renderDashboard: function(st, report, signal) {
    var sig = this._analysisTextValue(st.signal || signal) || "—";
    var sc = /买入|buy/i.test(sig) ? "up" : /卖出|sell/i.test(sig) ? "dn" : "neutral";
    var conf = Math.max(0, Math.min(100, parseInt(st.confidence, 10) || 0));
    var h = '<div class="ap-dash">';

    h += '<div class="ap-hero ap-hero--' + sc + '">' +
      '<div class="ap-hero-left">' +
        '<div class="ap-hero-name">' + esc(this._analysisName || "") +
          '<span class="ap-hero-ticker">' + esc(this._analysisTicker || (report && report.ticker) || "") + '</span></div>' +
        '<div class="ap-hero-thesis">' + esc(this._analysisTextValue(st.thesis || "")) + '</div></div>' +
      '<div class="ap-hero-right"><div class="ap-signal ap-signal--' + sc + '">' + esc(sig) + '</div>' +
        this._confRing(conf) + '</div></div>';

    var m = st.metrics || {};
    h += '<div class="ap-tiles">' + this._tile("估值", m.valuation) + this._tile("增长", m.growth) +
      this._tile("风险", (m.risk_score !== undefined && m.risk_score !== "" && m.risk_score !== null) ? (this._analysisTextValue(m.risk_score) + "/10") : "") +
      this._tile("动量", m.momentum) + '</div>';

    var dg = st.analyst_digests || [];
    if (dg.length) {
      h += '<div class="ap-section"><h4>🔬 分析师研报</h4><div class="ap-analyst-grid">';
      for (var i = 0; i < dg.length; i++) h += this._analystCard(dg[i]);
      h += '</div></div>';
    }

    var bb = st.bull_bear || {};
    if (bb.bull || bb.bear) {
      h += '<div class="ap-section ap-debate">' +
        '<div class="ap-debate-col ap-bull"><h5>🐂 多方</h5><p>' + esc(this._analysisTextValue(bb.bull || "—")) + '</p></div>' +
        '<div class="ap-debate-col ap-bear"><h5>🐉 空方</h5><p>' + esc(this._analysisTextValue(bb.bear || "—")) + '</p></div>' +
        (bb.judge ? '<div class="ap-debate-judge">裁决：<b>' + esc(this._analysisTextValue(bb.judge)) + '</b></div>' : '') + '</div>';
    }

    var tr = st.trade || {}, risk = (report && report.risk_debate) || {};
    h += '<div class="ap-section ap-twocol">';
    h += '<div class="ap-trade"><h4>📊 交易方案</h4>';
    if (tr.target_price || tr.stop_loss || tr.position || tr.horizon) {
      h += '<div class="ap-trade-grid">' + this._tradeField("目标价", tr.target_price) +
        this._tradeField("止损", tr.stop_loss) + this._tradeField("仓位", tr.position) +
        this._tradeField("持有期", tr.horizon) + '</div>';
    }
    if (tr.rationale) h += '<div class="ap-trade-why">' + App._renderMarkdown(tr.rationale) + '</div>';
    h += '</div>';
    h += '<div class="ap-risk-tri"><h4>🛡 风险评估</h4><div class="ap-risk-cols">' +
      this._riskCol("激进", risk.aggressive, "up") + this._riskCol("中性", risk.neutral, "neutral") +
      this._riskCol("保守", risk.conservative, "dn") + '</div></div></div>';

    var verdict = this._analysisTextValue(st.verdict_summary || (report && report.final_decision) || "");
    if (verdict) {
      h += '<div class="ap-section ap-final"><h4>🎯 最终决策</h4><div class="ap-body">' +
        (st.verdict_summary ? esc(verdict) : App._renderMarkdown(verdict)) + '</div></div>';
    }
    h += '</div>';
    return h;
  },

  _confRing: function(pct) {
    var r = 26, c = 2 * Math.PI * r, off = c * (1 - pct / 100);
    return '<div class="ap-conf"><svg width="64" height="64" viewBox="0 0 64 64">' +
      '<circle cx="32" cy="32" r="' + r + '" fill="none" stroke="var(--bg3)" stroke-width="6"/>' +
      '<circle cx="32" cy="32" r="' + r + '" fill="none" stroke="var(--accent)" stroke-width="6" ' +
      'stroke-linecap="round" stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '" ' +
      'transform="rotate(-90 32 32)"/></svg><span class="ap-conf-val">' + pct + '%</span></div>';
  },

  _tile: function(k, v) {
    v = this._analysisTextValue(v || "");
    var cls = /高|强/i.test(v || "") ? "hi" : /低|弱/i.test(v || "") ? "lo" : "";
    return '<div class="ap-tile"><span class="ap-tile-k">' + k + '</span>' +
      '<span class="ap-tile-v ' + cls + '">' + esc(v || "—") + '</span></div>';
  },

  _analystCard: function(d) {
    var ic = {market:"📈",social:"💬",news:"📰",fundamentals:"💰",policy:"🏛",hot_money:"🔥",lockup:"🔒"}[d.agent] || "▪";
    var v = this._analysisTextValue(d.verdict || "");
    var vc = /多/i.test(v) ? "up" : /空/i.test(v) ? "dn" : "neutral";
    var bl = (d.bullets || []).map(function(b) { return "<li>" + esc(App._analysisTextValue(b)) + "</li>"; }).join("");
    return '<div class="ap-acard"><div class="ap-acard-hd"><span class="ap-acard-ic">' + ic + '</span>' +
      '<span class="ap-acard-nm">' + esc(this._analysisAgentLabels[d.agent] || d.agent) + '</span>' +
      '<span class="ap-verdict ap-verdict--' + vc + '">' + esc(v || "—") + '</span></div>' +
      (bl ? '<ul class="ap-acard-bl">' + bl + '</ul>' : '') + '</div>';
  },

  _tradeField: function(k, v) {
    return '<div class="ap-tf"><span class="ap-tf-k">' + k + '</span><span class="ap-tf-v">' + esc(this._analysisTextValue(v || "—")) + '</span></div>';
  },

  _riskCol: function(title, text, cls) {
    return '<div class="ap-risk-col ap-risk-col--' + cls + '"><h6>' + title + '</h6>' +
      '<div class="ap-risk-col-body">' + (text ? App._renderMarkdown(text) : "—") + '</div></div>';
  },

  _normalizeAnalysisMarkdown: function(text) {
    text = this._analysisTextValue(text);
    if (!text) return "";
    return text
      .replace(/\r\n?/g, "\n")
      .replace(/^[A-Za-z][A-Za-z /&-]{1,48}Analyst\s*:\s*(?=#{1,6}\s+)/gmi, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  },

  _renderMarkdown: function(text) {
    text = this._normalizeAnalysisMarkdown(text);
    if (!text) return "";
    try { return sanitizeHtml(marked.parse(escapeAllGt(text))); }
    catch(e) { return escHtml(text); }
  },

  _addAnalysisToChat: function() {
    var report = "";
    if (this._analysisTicker) report += "## " + this._analysisTicker + " " + this._analysisName + " 分析报告\n\n";
    var le = null;
    for (var i = this._analysisEvents.length - 1; i >= 0; i--) {
      if (this._analysisEvents[i].type === "done") { le = this._analysisEvents[i]; break; }
    }
    if (le && le.report) {
      var finalDecision = this._analysisTextValue(le.report.final_decision || "");
      var traderPlan = this._analysisTextValue(le.report.trader_plan || "");
      if (finalDecision) report += finalDecision + "\n\n";
      if (traderPlan) report += "### 交易方案\n" + traderPlan + "\n\n";
    }
    this.switchMode("chat");
    var input = document.getElementById("input");
    if (input) { input.value = report; input.focus(); }
  },

  /* ── Persist analysis across page refreshes ── */
  _saveAnalysisState: function() {
    if (!this._analysisId) return;
    var state = {
      id: this._analysisId,
      ticker: this._analysisTicker,
      name: this._analysisName,
      startTime: this._analysisStartTime,
      streaming: this._analysisStreaming,
      mode: this._analysisMode,
      selectedAgents: this._analysisSelectedAgents,
      stage: this._analysisCurrentStage,
      progress: this._analysisProgressPct,
      agentDone: this._analysisAgentDone,
      eventCount: this._analysisEvents.length
    };
    try { sessionStorage.setItem('prism_analysis', JSON.stringify(state)); } catch(e) {}
  },

  _restoreAnalysisState: function() {
    if (this._analysisAttachAttempted) return;
    this._analysisAttachAttempted = true;
    var raw;
    try { raw = sessionStorage.getItem('prism_analysis'); } catch(e) { return; }
    if (!raw) return;
    var state;
    try { state = JSON.parse(raw); } catch(e) { return; }
    if (!state || !state.id) return;

    this._analysisId = state.id;
    this._analysisTicker = state.ticker;
    this._analysisName = state.name;
    this._analysisStartTime = state.startTime || Date.now();
    this._analysisMode = state.mode || "fast";
    this._analysisSelectedAgents = state.selectedAgents || this._analysisAgentsForMode(this._analysisMode);
    this._analysisCurrentStage = state.stage || "";
    this._analysisProgressPct = state.progress || 0;
    this._analysisAgentDone = state.agentDone || {};
    this._analysisEvents = [];
    this._setAnalysisMode(this._analysisMode);

    // Rebuild the in-progress UI
    document.getElementById("analysisPanelInner").innerHTML =
      '<div class="ap-container">' +
        this._buildProgressBar() +
        '<div class="ap-status" id="apStatus">正在重新连接…</div>' +
        '<div class="ap-agent-grid" id="apAgentGrid"></div>' +
        '<div class="ap-stream" id="apStream"><div class="ap-stream-hd" onclick="App._toggleStream()">🧠 分析过程 <span class="ap-stream-cnt"></span></div><div class="ap-stream-body" id="apStreamBody"></div></div>' +
      '</div>' +
      '<div class="ap-report" id="apReport" style="display:none"></div>';

    if (this._analysisProgressPct > 0) {
      this._updateProgress(this._analysisProgressPct);
    }
    this._renderAgentGrid();

    if (state.streaming) {
      // Re-attach SSE
      this._analysisStreaming = true;
      this._connectAnalysisStream();
    } else {
      // Analysis was already complete — fetch report
      this._fetchCompletedReport();
    }
  },

  _fetchCompletedReport: async function() {
    var r = await this.api("/api/analysis/" + this._analysisId + "/report");
    if (r.ok) {
      var data = await r.json();
      if (data.report) {
        this._updateProgress(100);
        var s = document.getElementById("apStatus");
        if (s) s.textContent = "✅ 分析完成";
        this._setAnalysisMode(data.mode || this._analysisMode);
        this._analysisSelectedAgents = data.selected_analysts || this._analysisSelectedAgents;
        this._renderFinalReport(data.report, data.signal);
      }
    }
    // Clean up sessionStorage after showing report
    try { sessionStorage.removeItem('prism_analysis'); } catch(e) {}
  }

});
