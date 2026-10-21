/* Prism - full-market indicator screener workspace */
Object.assign(App, {

  _marketWorkspaceTab: "quote",
  _scanJobId: null,
  _scanPollTimer: null,
  _scanParamValues: {},
  _scanWorkbenchWired: false,

  _initMarketWorkbench: function() {
    if (!this._scanWorkbenchWired) {
      this._scanWorkbenchWired = true;
      var self = this;
      var config = document.querySelector(".market-scan-config");
      if (config) {
        config.addEventListener("input", function(ev) {
          var key = ev.target.getAttribute("data-scan-param");
          if (key) self._updateScanParam(key, ev.target.value);
        });
        config.addEventListener("change", function(ev) {
          if (ev.target.matches('input[name="scanMarket"],input[name="scanBoard"],#marketScanExcludeST')) {
            self._refreshUniverseSummary();
          }
        });
        config.addEventListener("click", function(ev) {
          var quick = ev.target.closest("button[data-scan-param][data-param-set]");
          if (!quick) return;
          ev.preventDefault();
          self._updateScanParam(quick.getAttribute("data-scan-param"), quick.getAttribute("data-param-set"));
        });
      }
    }
    this._showMarketWorkspace(this._marketWorkspaceTab || "quote");
    this._loadRecentScans();
  },

  _showMarketWorkspace: function(tab) {
    tab = ["quote", "screener", "library"].indexOf(tab) >= 0 ? tab : "quote";
    if (this._mode !== "market") {
      this.switchMode("market");
    }
    this._marketWorkspaceTab = tab;
    var tabs = document.querySelectorAll("[data-market-tab]");
    for (var i = 0; i < tabs.length; i++) {
      var active = tabs[i].getAttribute("data-market-tab") === tab;
      tabs[i].classList.toggle("active", active);
      tabs[i].setAttribute("aria-selected", active ? "true" : "false");
    }
    var views = document.querySelectorAll("[data-market-view]");
    for (var j = 0; j < views.length; j++) {
      views[j].classList.toggle("active", views[j].getAttribute("data-market-view") === tab);
    }
    if (tab === "library") {
      this._loadIndicators();
    } else if (tab === "screener") {
      this._syncScannerIndicators();
      this._refreshUniverseSummary();
      this._loadRecentScans();
    }
    this._closeMobileSidebar();
  },

  _setScanSegment: function(kind, value) {
    var id = kind === "period" ? "marketScanPeriod" : "marketScanScope";
    var hidden = document.getElementById(id);
    if (hidden) hidden.value = value;
    var wrap = document.querySelector('[data-scan-segment="' + kind + '"]');
    if (wrap) {
      var buttons = wrap.querySelectorAll("button[data-value]");
      for (var i = 0; i < buttons.length; i++) {
        var active = buttons[i].getAttribute("data-value") === value;
        buttons[i].classList.toggle("active", active);
        buttons[i].setAttribute("aria-pressed", active ? "true" : "false");
      }
    }
    if (kind === "scope") this._refreshUniverseSummary();
  },

  _syncScannerIndicators: function(preferredId) {
    var select = document.getElementById("marketScanIndicator");
    if (!select) return;
    var current = preferredId || select.value || "";
    var options = [];
    for (var i = 0; i < this._indicators.length; i++) {
      var item = this._indicators[i];
      options.push('<option value="' + escAttr(item.id) + '">' + esc(item.name) + (item.kind === "signal" ? " · 信号" : " · 数值") + "</option>");
    }
    select.innerHTML = options.join("");
    if (!this._indicators.length) {
      select.innerHTML = '<option value="">请先创建指标</option>';
      this._renderScannerParams(null);
      return;
    }
    var exists = false;
    for (var j = 0; j < this._indicators.length; j++) if (this._indicators[j].id === current) exists = true;
    select.value = exists ? current : this._indicators[0].id;
    this._selectScannerIndicator(select.value);
  },

  _selectScannerIndicator: function(id) {
    var indicator = this._findIndicator(id);
    if (!indicator) return;
    var plan = indicator.plan || {};
    var lookback = document.getElementById("marketScanLookback");
    var win = document.getElementById("marketScanWindow");
    var turnover = document.getElementById("marketScanTurnover");
    var maxResults = document.getElementById("marketScanMaxResults");
    if (lookback) lookback.value = plan.lookback || 120;
    if (win) win.value = plan.trigger_window || 5;
    if (turnover) turnover.value = Math.round(Number(plan.min_turnover || 0) / 10000);
    if (maxResults) maxResults.value = plan.max_results || 100;
    this._setScanSegment("scope", plan.universe === "watchlist" ? "watchlist" : "market");
    this._setScanSegment("period", plan.period || "1d");
    this._setScanChecks('input[name="scanMarket"]', plan.markets || ["SH", "SZ", "BJ"]);
    this._setScanChecks('input[name="scanBoard"]', plan.boards || ["main", "gem", "star", "bse"]);
    var exclude = document.getElementById("marketScanExcludeST");
    if (exclude) exclude.checked = plan.exclude_st !== false;
    this._renderScannerParams(indicator);
    var hint = document.getElementById("marketScanResultHint");
    if (hint) hint.textContent = indicator.kind === "signal" ? "结果按信号强度与触发时间排序" : "数值指标将按最新值进行全市场排名";
    this._refreshUniverseSummary();
  },

  _setScanChecks: function(selector, values) {
    var wanted = {};
    for (var i = 0; i < values.length; i++) wanted[String(values[i])] = true;
    var boxes = document.querySelectorAll(selector);
    for (var j = 0; j < boxes.length; j++) boxes[j].checked = !!wanted[boxes[j].value];
  },

  _renderScannerParams: function(indicator) {
    var wrap = document.getElementById("marketScanParams");
    if (!wrap) return;
    this._scanParamValues = {};
    if (!indicator) {
      wrap.innerHTML = '<div class="market-scan-param-empty">暂无可配置参数</div>';
      return;
    }
    var items = this._indicatorParamItems(indicator.params || {});
    if (!items.length) {
      wrap.innerHTML = '<div class="market-field"><span>指标参数</span><div class="market-scan-param-empty">该指标无需额外参数</div></div>';
      return;
    }
    var html = '<div class="market-field"><span>本次扫描逻辑</span><div class="market-scan-logic" data-scan-logic></div></div><div class="market-field"><span>核心指标参数</span><div class="market-scan-param-list">';
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var value = item.value == null ? 0 : item.value;
      this._scanParamValues[item.key] = value;
    }
    html += this._indicatorParamBlocks(items, "data-scan-param");
    wrap.innerHTML = html + "</div></div>";
    var logic = wrap.querySelector("[data-scan-logic]");
    if (logic) logic.textContent = this._scannerLogicText(indicator);
  },

  _scannerLogicText: function(indicator) {
    var ui = this._indicatorUi(indicator);
    var template = ui.logic_template || indicator.description || "调整参数后，扫描逻辑会同步更新";
    var values = this._scanParamValues || {};
    return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, function(match, key) {
      return values[key] == null ? match : String(values[key]);
    });
  },

  _updateScanParam: function(key, value) {
    var number = Number(value);
    this._scanParamValues[key] = isNaN(number) ? value : number;
    var wrap = document.getElementById("marketScanParams");
    var inputs = (wrap || document).querySelectorAll('input[data-scan-param="' + key.replace(/"/g, '\\"') + '"]');
    for (var i = 0; i < inputs.length; i++) {
      if (document.activeElement !== inputs[i]) inputs[i].value = this._scanParamValues[key];
    }
    var quick = (wrap || document).querySelectorAll('button[data-scan-param="' + key.replace(/"/g, '\\"') + '"][data-param-set]');
    for (var q = 0; q < quick.length; q++) {
      var active = Number(quick[q].getAttribute("data-param-set")) === Number(this._scanParamValues[key]);
      quick[q].classList.toggle("active", active);
      quick[q].setAttribute("aria-pressed", active ? "true" : "false");
    }
    var label = (wrap || document).querySelector('[data-param-display="' + key.replace(/"/g, '\\"') + '"]');
    if (label) {
      var unit = "";
      var selected = this._findIndicator((document.getElementById("marketScanIndicator") || {}).value);
      var items = selected ? this._indicatorParamItems(selected.params || {}) : [];
      for (var j = 0; j < items.length; j++) if (items[j].key === key) unit = items[j].unit || "";
      label.textContent = String(this._scanParamValues[key]) + unit;
    }
    var logic = (wrap || document).querySelector("[data-scan-logic]");
    var selectedIndicator = this._findIndicator((document.getElementById("marketScanIndicator") || {}).value);
    if (logic && selectedIndicator) logic.textContent = this._scannerLogicText(selectedIndicator);
  },

  _scanValues: function(selector) {
    var values = [];
    var boxes = document.querySelectorAll(selector);
    for (var i = 0; i < boxes.length; i++) if (boxes[i].checked) values.push(boxes[i].value);
    return values;
  },

  _refreshUniverseSummary: async function() {
    var meta = document.getElementById("marketUniverseMeta");
    if (!meta) return;
    var scope = (document.getElementById("marketScanScope") || {}).value || "market";
    if (scope === "watchlist") {
      meta.textContent = "本次将扫描 " + (this._watchlist || []).length + " 只自选股";
      return;
    }
    var markets = this._scanValues('input[name="scanMarket"]');
    var boards = this._scanValues('input[name="scanBoard"]');
    if (!markets.length || !boards.length) {
      meta.textContent = "请至少选择一个市场和一个板块";
      return;
    }
    meta.textContent = "正在计算股票池...";
    clearTimeout(this._scanUniverseTimer);
    var self = this;
    this._scanUniverseTimer = setTimeout(async function() {
      try {
        var url = "/api/indicators/universe/summary?markets=" + encodeURIComponent(markets.join(",")) + "&boards=" + encodeURIComponent(boards.join(",")) + "&exclude_st=" + ((document.getElementById("marketScanExcludeST") || {}).checked !== false);
        var r = await self.api(url);
        var data = r.ok ? await r.json() : null;
        meta.textContent = data ? ("覆盖 " + data.selected + " / " + data.total + " 只 A 股，后台分批扫描") : "股票池读取失败";
      } catch (e) {
        meta.textContent = "股票池读取失败";
      }
    }, 220);
  },

  _scanPayload: function() {
    var scope = (document.getElementById("marketScanScope") || {}).value || "market";
    var selected = this._findIndicator((document.getElementById("marketScanIndicator") || {}).value) || {};
    var preferredSort = (selected.plan || {}).score;
    var validSort = ["strength", "recent_signal", "latest_value"].indexOf(preferredSort) >= 0;
    var preferredDirection = (selected.plan || {}).direction;
    var names = {};
    var tickers = [];
    if (scope === "watchlist") {
      for (var i = 0; i < (this._watchlist || []).length; i++) {
        tickers.push(this._watchlist[i].ticker);
        names[this._watchlist[i].ticker] = this._watchlist[i].name || this._watchlist[i].ticker;
      }
    }
    return {
      scope: scope,
      tickers: tickers,
      ticker_names: names,
      period: (document.getElementById("marketScanPeriod") || {}).value || "1d",
      lookback: Number((document.getElementById("marketScanLookback") || {}).value || 120),
      trigger_window: Number((document.getElementById("marketScanWindow") || {}).value || 5),
      params: Object.assign({}, this._scanParamValues),
      markets: this._scanValues('input[name="scanMarket"]'),
      boards: this._scanValues('input[name="scanBoard"]'),
      exclude_st: (document.getElementById("marketScanExcludeST") || {}).checked !== false,
      min_turnover: Number((document.getElementById("marketScanTurnover") || {}).value || 0) * 10000,
      max_results: Number((document.getElementById("marketScanMaxResults") || {}).value || 100),
      max_universe: 0,
      sort: validSort ? preferredSort : (selected.kind === "plot" ? "latest_value" : "strength"),
      direction: preferredDirection === "asc" ? "asc" : "desc"
    };
  },

  _startMarketScan: async function() {
    var select = document.getElementById("marketScanIndicator");
    var id = select ? select.value : "";
    if (!id) { this._toast("请先选择一个指标"); return; }
    var payload = this._scanPayload();
    if (payload.scope === "watchlist" && !payload.tickers.length) { this._toast("自选为空，无法扫描"); return; }
    if (!payload.markets.length || !payload.boards.length) { this._toast("请至少选择一个市场和一个板块"); return; }
    this._setScanBusy(true);
    this._setScanStatus("正在创建后台扫描任务", "running");
    try {
      var r = await this.api("/api/indicators/" + encodeURIComponent(id) + "/scan-jobs", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      var data = await r.json().catch(function() { return {}; });
      if (r.status === 409 && data.job) {
        this._scanJobId = data.job.id;
        this._renderScanJob(data.job);
        this._scheduleScanPoll();
        this._toast("已接入正在运行的扫描任务");
        return;
      }
      if (!r.ok || !data.job) throw new Error(data.error || data.detail || "创建任务失败");
      this._scanJobId = data.job.id;
      this._renderScanJob(data.job);
      this._scheduleScanPoll(300);
      this._loadRecentScans();
      if (this._trackUiEvent) this._trackUiEvent("market", "screener_start", "indicator", id, payload.scope);
    } catch (e) {
      this._setScanBusy(false);
      this._setScanStatus(e.message || "创建任务失败", "failed");
    }
  },

  _setScanBusy: function(busy) {
    var start = document.getElementById("marketScanStart");
    var cancel = document.getElementById("marketScanCancel");
    if (start) start.disabled = !!busy;
    if (cancel) cancel.disabled = !busy;
  },

  _setScanStatus: function(text, status) {
    var state = document.getElementById("marketScanState");
    if (state) {
      state.textContent = text;
      state.className = "market-scan-state " + (status || "");
    }
  },

  _scheduleScanPoll: function(delay) {
    clearTimeout(this._scanPollTimer);
    var self = this;
    this._scanPollTimer = setTimeout(function() { self._pollScanJob(); }, delay == null ? 1500 : delay);
  },

  _pollScanJob: async function() {
    if (!this._scanJobId) return;
    try {
      var r = await this.api("/api/indicators/scan-jobs/" + encodeURIComponent(this._scanJobId));
      if (!r.ok) throw new Error("任务读取失败");
      var data = await r.json();
      var job = data.job;
      this._renderScanJob(job);
      if (job.status === "queued" || job.status === "running") this._scheduleScanPoll();
      else this._loadRecentScans();
    } catch (e) {
      this._setScanBusy(false);
      this._setScanStatus("任务状态读取失败", "failed");
    }
  },

  _renderScanJob: function(job) {
    if (!job) return;
    this._scanJobId = job.id;
    var active = job.status === "queued" || job.status === "running";
    this._setScanBusy(active);
    var labels = {queued:"等待执行",running:"扫描中",completed:"扫描完成",failed:"扫描失败",cancelled:"已取消",interrupted:"任务已中断"};
    this._setScanStatus(labels[job.status] || job.status, job.status);
    var status = document.getElementById("marketScanStatus");
    if (status) status.textContent = job.error || (job.indicator_name + " · " + (labels[job.status] || job.status));
    var progress = Number(job.progress || 0);
    var progressText = document.getElementById("marketScanProgressText");
    var progressBar = document.getElementById("marketScanProgressBar");
    if (progressText) progressText.textContent = progress.toFixed(progress % 1 ? 1 : 0) + "%";
    if (progressBar) progressBar.style.width = Math.max(0, Math.min(100, progress)) + "%";
    var total = document.getElementById("marketScanTotal");
    var processed = document.getElementById("marketScanProcessed");
    var matched = document.getElementById("marketScanMatched");
    var failed = document.getElementById("marketScanFailed");
    if (total) total.textContent = job.total || 0;
    if (processed) processed.textContent = job.processed || 0;
    if (matched) matched.textContent = job.matched || 0;
    if (failed) failed.textContent = job.failed || 0;
    this._renderMarketScanResults(job.matches || [], job);
  },

  _fmtScanTurnover: function(value) {
    var n = Number(value || 0);
    if (!n) return "-";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    return (n / 1e4).toFixed(0) + "万";
  },

  _renderMarketScanResults: function(matches, job) {
    var el = document.getElementById("marketScanResults");
    if (!el) return;
    if (!matches.length) {
      var pending = job && (job.status === "queued" || job.status === "running");
      el.innerHTML = '<div class="market-scan-empty">' + (pending ? "正在扫描，命中结果会实时出现在这里" : "当前条件暂无命中结果") + "</div>";
      return;
    }
    var html = '<div class="market-scan-table"><div class="market-scan-row market-scan-row-head"><span>股票</span><span>板块</span><span>价格</span><span>信号 / 数值</span><span>成交额</span><span>操作</span></div>';
    for (var i = 0; i < matches.length; i++) {
      var row = matches[i];
      var tickerArg = JSON.stringify(row.ticker || "").replace(/"/g, "&quot;");
      var nameArg = JSON.stringify(row.name || row.ticker || "").replace(/"/g, "&quot;");
      var signal = row.value != null ? Number(row.value).toFixed(3) : ((row.days_ago || 0) === 0 ? "最新触发" : (row.days_ago + " 根前触发"));
      html += '<div class="market-scan-row"><span class="market-scan-stock"><b>' + esc(row.ticker || "") + "</b><em>" + esc(row.name || "") + '</em></span><span class="market-scan-board"><i class="market-board-tag">' + esc((row.market || "") + " · " + (row.board || "")) + '</i></span><span class="market-scan-price">' + (row.price == null ? "-" : Number(row.price).toFixed(2)) + '</span><span class="market-scan-signal"><b>' + esc(signal) + "</b><em>" + esc(row.reason || "") + '</em></span><span class="market-scan-turnover">' + esc(this._fmtScanTurnover(row.turnover)) + '</span><span class="market-scan-row-actions"><button type="button" onclick="App._openScanTicker(' + tickerArg + "," + nameArg + ')">行情</button><button type="button" onclick="App._analyzeScanTicker(' + tickerArg + "," + nameArg + ')">分析</button></span></div>';
    }
    el.innerHTML = html + "</div>";
  },

  _openScanTicker: function(ticker, name) {
    this._selectStock(ticker, name);
  },

  _analyzeScanTicker: function(ticker, name) {
    this._analysisTicker = ticker;
    this._analysisName = name || ticker;
    this.switchMode("analysis");
    var q = document.getElementById("analysisQ");
    if (q) q.value = ticker + " " + (name || "");
  },

  _cancelMarketScan: async function() {
    if (!this._scanJobId) return;
    await this.api("/api/indicators/scan-jobs/" + encodeURIComponent(this._scanJobId) + "/cancel", {method:"POST"});
    this._scheduleScanPoll(200);
  },

  _loadRecentScans: async function() {
    var el = document.getElementById("marketScanHistory");
    if (!el) return;
    try {
      var r = await this.api("/api/indicators/scan-jobs?limit=8");
      if (!r.ok) return;
      var jobs = (await r.json()).jobs || [];
      if (!jobs.length) { el.innerHTML = '<div class="market-scan-empty compact">暂无历史任务</div>'; return; }
      var html = "";
      for (var i = 0; i < jobs.length; i++) {
        var job = jobs[i];
        var jobArg = JSON.stringify(job.id).replace(/"/g, "&quot;");
        html += '<div class="market-scan-history-item' + (job.id === this._scanJobId ? " active" : "") + '" data-job-id="' + escAttr(job.id) + '"><button type="button" class="market-scan-history-open" onclick="App._openScanJob(' + jobArg + ')"><b>' + esc(job.indicator_name || "指标扫描") + '</b><span>' + esc((job.config || {}).scope === "market" ? "全市场" : "自选") + " · " + esc(String(job.processed || 0)) + "/" + esc(String(job.total || 0)) + " · 命中 " + esc(String(job.matched || 0)) + '</span></button><button type="button" class="market-scan-history-del" title="删除任务" onclick="App._deleteScanJob(' + jobArg + ',event)">×</button></div>';
      }
      el.innerHTML = html;
      if (!this._scanJobId) {
        for (var j = 0; j < jobs.length; j++) {
          if (jobs[j].status === "queued" || jobs[j].status === "running") {
            this._scanJobId = jobs[j].id;
            this._renderScanJob(jobs[j]);
            this._scheduleScanPoll(300);
            break;
          }
        }
      }
    } catch (e) {}
  },

  _openScanJob: async function(id) {
    clearTimeout(this._scanPollTimer);
    this._scanJobId = id;
    var r = await this.api("/api/indicators/scan-jobs/" + encodeURIComponent(id));
    if (!r.ok) return;
    var job = (await r.json()).job;
    this._renderScanJob(job);
    if (job.status === "queued" || job.status === "running") this._scheduleScanPoll();
    this._loadRecentScans();
  },

  _deleteScanJob: async function(id, ev) {
    if (ev) ev.stopPropagation();
    await this.api("/api/indicators/scan-jobs/" + encodeURIComponent(id), {method:"DELETE"});
    if (this._scanJobId === id) {
      this._scanJobId = null;
      this._setScanBusy(false);
      this._setScanStatus("等待配置", "");
      this._renderMarketScanResults([], null);
    }
    this._loadRecentScans();
  },

  _prepareIndicatorScan: function(id) {
    this._showMarketWorkspace("screener");
    this._syncScannerIndicators(id);
  },

  _renderIndicatorDock: function() {
    var dock = document.getElementById("marketIndicatorDock");
    if (!dock) return;
    if (!this._indicators.length) {
      dock.innerHTML = '<div class="market-dock-head"><h4>指标快测</h4><button type="button" onclick="App._showMarketWorkspace(\'library\')">创建指标</button></div><div class="market-dock-empty">指标库加载中...</div>';
      return;
    }
    var html = '<div class="market-dock-head"><h4>指标快测</h4><button type="button" onclick="App._showMarketWorkspace(\'library\')">管理指标</button></div><p class="market-dock-note">直接叠加到当前 K 线，参数可在指标库中微调。</p><div class="market-dock-list">';
    for (var i = 0; i < Math.min(6, this._indicators.length); i++) {
      var item = this._indicators[i];
      html += '<div class="market-dock-item"><span><i class="mi-tag ' + (item.kind === "signal" ? "sig" : "plot") + '">' + (item.kind === "signal" ? "信号" : "数值") + "</i><b>" + esc(item.name) + '</b></span><button type="button" onclick="App._applyIndicator(\'' + escAttr(item.id) + '\')">试算</button></div>';
    }
    dock.innerHTML = html + '</div><button type="button" class="market-dock-scan" onclick="App._showMarketWorkspace(\'screener\')">用指标扫描全市场</button>';
  }

});
