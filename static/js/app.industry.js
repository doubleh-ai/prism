/* Prism - Industry Intelligence Workbench (v0.19.7) */
Object.assign(App, {
  _industryView: "overview",
  _industryCategory: "",
  _industryOverview: null,
  _industryCategories: [],
  _industryChainNodes: [],
  _industryChainEdges: [],
  _industryMapChart: null,
  _industryMiniMapChart: null,
  _industrySearchTimer: null,
  _industryContextTimer: null,
  _industryContextLoading: false,
  _industryContext: null,
  _worldMap: null,

  _industryTierLabels: ["上游资源", "中游制造", "下游应用"],
  _industryTierHints: ["原料、设备、零部件", "制造、平台、集成", "客户、渠道、场景"],

  _initIndustry: function() {
    this._loadIndustryWorkbench();
  },

  _leaveIndustry: function() {
    this._industryChainSeq = (this._industryChainSeq || 0) + 1;
    if (this._industryContextTimer) {
      clearTimeout(this._industryContextTimer);
      this._industryContextTimer = null;
    }
    this._disposeIndustryCharts();
    this._closeIndustryDrawer();
  },

  _disposeIndustryCharts: function() {
    if (this._industryMapChart) {
      try { this._industryMapChart.dispose(); } catch (e) {}
      this._industryMapChart = null;
    }
    if (this._industryMiniMapChart) {
      try { this._industryMiniMapChart.dispose(); } catch (e) {}
      this._industryMiniMapChart = null;
    }
  },

  _refreshIndustryTheme: function() {
    if (this._mode !== "industry") return;
    if (this._industryView === "chain" && this._industryCategory) {
      this._renderIndustryChain(this._industryCategory);
    } else {
      this._renderIndustryOverview();
    }
  },

  _loadIndustryWorkbench: async function() {
    await Promise.all([this._fetchIndustryOverview(), this._fetchIndustryContext()]);
    this._renderIndustrySidebar();
    if (this._industryView === "chain" && this._industryCategory) {
      this._renderIndustryChain(this._industryCategory);
    } else {
      this._renderIndustryOverview();
    }
    this._scheduleIndustryContext();
  },

  _fetchIndustryOverview: async function() {
    try {
      var r = await this.api("/api/industry/overview");
      if (r.ok) {
        this._industryOverview = await r.json();
        this._industryCategories = this._industryOverview.categories || [];
        return;
      }
    } catch (e) {}
    try {
      var cr = await this.api("/api/industry/categories");
      if (cr.ok) this._industryCategories = (await cr.json()).categories || [];
    } catch (e) {}
  },

  _fetchIndustryContext: async function() {
    try {
      var r = await this.api("/api/industry/context");
      if (!r.ok) return false;
      this._industryContext = await r.json();
      return true;
    } catch (e) {
      return false;
    }
  },

  _scheduleIndustryContext: function() {
    var self = this;
    if (this._industryContextTimer) clearTimeout(this._industryContextTimer);
    var seconds = Number((this._industryContext || {}).auto_refresh_seconds || 90);
    this._industryContextTimer = setTimeout(async function() {
      if (self._mode !== "industry") return;
      await self._fetchIndustryContext();
      if (self._industryView === "chain" && self._industryCategory) self._renderIndustryChain(self._industryCategory);
      else self._renderIndustryOverview();
      self._renderIndustrySidebar();
      self._scheduleIndustryContext();
    }, Math.max(30, seconds) * 1000);
  },

  _refreshIndustryContext: async function(button) {
    if (this._industryContextLoading) return;
    this._industryContextLoading = true;
    if (button) button.classList.add("loading");
    await Promise.all([this._fetchIndustryOverview(), this._fetchIndustryContext()]);
    this._industryContextLoading = false;
    if (button) button.classList.remove("loading");
    this._renderIndustrySidebar();
    if (this._industryView === "chain" && this._industryCategory) this._renderIndustryChain(this._industryCategory);
    else this._renderIndustryOverview();
    this._scheduleIndustryContext();
  },

  _loadIndustrySidebar: function() {
    this._loadIndustryWorkbench();
  },

  _renderIndustrySidebar: function() {
    var el = document.getElementById("industrySidebarList");
    if (!el) return;
    var overviewActive = this._industryView !== "chain";
    var cats = this._industryCategories || [];
    var top = cats.slice(0, 12).map(function(c) {
      var active = c.category === App._industryCategory ? " active" : "";
      var mix = [c.upstream_count || 0, c.midstream_count || 0, c.downstream_count || 0].join("/");
      return '<button class="industry-side-chain' + active + '" data-c="' + escAttr(c.category) + '" onclick="App._selectIndustryChain(this.dataset.c)">' +
        '<span><b>' + esc(c.category) + '</b><em>' + esc(mix) + ' · ' + esc(String(c.country_count || 0)) + '国</em></span>' +
        '<strong>' + esc(String(c.node_count || 0)) + '</strong>' +
      '</button>';
    }).join("");
    var watch = (this._watchlist || []).slice(0, 8).map(function(w) {
      return '<button class="industry-side-watch" data-t="' + escAttr(w.ticker) + '" onclick="App._selectIndustryTicker(this.dataset.t)">' +
        '<span>' + esc(w.ticker) + '</span><em>' + esc(w.name || "") + '</em></button>';
    }).join("");
    el.innerHTML =
      '<div class="industry-side">' +
        '<button class="industry-side-overview' + (overviewActive ? " active" : "") + '" onclick="App._backToMap()">总览</button>' +
        '<div class="industry-side-search">' +
          '<input id="industrySearchQ" class="market-q-input" placeholder="搜索产业 / 公司 / 代码" autocomplete="off" oninput="App._industrySearchInput(this.value)">' +
          '<div class="industry-search-results" id="industrySearchResults"></div>' +
        '</div>' +
        '<div class="industry-side-title"><span>产业雷达</span><em>' + esc(String(cats.length)) + '</em></div>' +
        '<div class="industry-side-list">' + (top || '<div class="ind-empty small">暂无产业数据</div>') + '</div>' +
        '<div class="industry-side-title"><span>自选链路</span><em>' + esc(String((this._watchlist || []).length)) + '</em></div>' +
        '<div class="industry-side-watchlist">' + (watch || '<div class="ind-empty small">暂无自选股</div>') + '</div>' +
      '</div>';
  },

  _industrySearchInput: function(q) {
    var self = this;
    clearTimeout(this._industrySearchTimer);
    this._industrySearchTimer = setTimeout(function() { self._runIndustrySearch(q); }, 180);
  },

  _runIndustrySearch: async function(q) {
    var box = document.getElementById("industrySearchResults");
    if (!box) return;
    q = (q || "").trim();
    if (!q) { box.innerHTML = ""; return; }
    box.innerHTML = '<div class="industry-search-empty">搜索中...</div>';
    try {
      var r = await this.api("/api/industry/search?q=" + encodeURIComponent(q) + "&limit=8");
      if (!r.ok) throw new Error("search failed");
      var rows = (await r.json()).results || [];
      if (!rows.length) { box.innerHTML = '<div class="industry-search-empty">无匹配节点</div>'; return; }
      box.innerHTML = rows.map(function(n) {
        return '<button class="industry-search-hit" data-id="' + escAttr(String(n.id)) + '" data-c="' + escAttr(n.category || "") + '" onclick="App._openIndustrySearchHit(this.dataset.id,this.dataset.c)">' +
          '<span>' + esc(n.name) + '</span><em>' + esc((n.source_ticker || n.category || "") + (n.description ? " · " + n.description : "")) + '</em></button>';
      }).join("");
    } catch (e) {
      box.innerHTML = '<div class="industry-search-empty">搜索失败</div>';
    }
  },

  _openIndustrySearchHit: function(nodeId, category) {
    if (category && category !== this._industryCategory) {
      this._industryCategory = category;
      this._industryView = "chain";
      this._renderIndustrySidebar();
      this._renderIndustryChain(category, nodeId);
      return;
    }
    this._showIndustryNode(nodeId);
  },

  _selectIndustryChain: function(cat) {
    if (!cat) return;
    this._industryCategory = cat;
    this._industryView = "chain";
    this._renderIndustrySidebar();
    this._renderIndustryChain(cat);
  },

  _selectIndustryTicker: function(ticker) {
    if (!ticker) return;
    this._industryView = "chain";
    this._renderIndustryChainByTicker(ticker);
  },

  _backToMap: function() {
    this._industryChainSeq = (this._industryChainSeq || 0) + 1;
    this._industryView = "overview";
    this._industryCategory = "";
    this._renderIndustrySidebar();
    this._renderIndustryOverview();
  },

  _cssVar: function(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  },

  _industryRelativeTime: function(timestamp) {
    var value = Number(timestamp || 0);
    if (!value) return "尚未同步";
    var seconds = Math.max(0, Math.floor(Date.now() / 1000 - value));
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return Math.floor(seconds / 60) + " 分钟前";
    if (seconds < 86400) return Math.floor(seconds / 3600) + " 小时前";
    return Math.floor(seconds / 86400) + " 天前";
  },

  _industryTickerKey: function(ticker) {
    var match = String(ticker || "").match(/(?:^|\D)([034689]\d{5})(?:\D|$)/);
    return match ? match[1] : "";
  },

  _industryTickerContext: function(ticker) {
    var key = this._industryTickerKey(ticker);
    return key && this._industryContext && this._industryContext.tickers ? this._industryContext.tickers[key] || null : null;
  },

  _industrySourceName: function(source) {
    return {analysis:"Astock",vibe:"Vibe",report:"报告",watchlist:"自选"}[source] || source;
  },

  _industrySyncBadge: function() {
    var context = this._industryContext || {};
    var summary = context.summary || {};
    var ready = Number(context.generated_at || 0) > 0;
    return '<div class="industry-sync' + (ready ? " ready" : "") + '">' +
      '<span><i></i>' + (ready ? "跨模块已同步" : "等待数据联动") + '</span>' +
      '<em>' + esc(this._industryRelativeTime(context.generated_at)) + (ready ? " · " + Number(summary.linked_tickers || 0) + " 已归链" : "") + '</em>' +
      '<button type="button" title="刷新跨模块数据" aria-label="刷新跨模块数据" onclick="App._refreshIndustryContext(this)">↻</button>' +
    '</div>';
  },

  _industryShell: function(active, body) {
    return '<div class="industry-workbench">' +
      '<div class="industry-topbar">' +
        '<div><h2>产业链研究工作台</h2><span>' + esc(active === "chain" ? this._industryCategory : "全局总览") + '</span></div>' +
        '<div class="industry-topbar-actions">' + this._industrySyncBadge() +
          '<div class="industry-tabs">' +
            '<button class="' + (active === "overview" ? "active" : "") + '" onclick="App._backToMap()">总览</button>' +
            '<button class="' + (active === "chain" ? "active" : "") + (this._industryCategory ? "" : " disabled") + '" data-c="' + escAttr(this._industryCategory || "") + '" onclick="App._selectIndustryChain(this.dataset.c)">链路</button>' +
          '</div>' +
        '</div>' +
      '</div>' + body +
    '</div>';
  },

  _renderIndustryOverview: async function() {
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    this._industryView = "overview";
    this._disposeIndustryCharts();
    var overview = this._industryOverview || {};
    var t = overview.totals || {};
    var cats = overview.categories || this._industryCategories || [];
    var keyNodes = overview.key_nodes || [];
    var body =
      this._renderIndustryContextStrip() +
      '<section class="industry-kpis">' +
        this._industryKpi("产业数", t.industry_count, "覆盖链路") +
        this._industryKpi("节点", t.node_count, "公司/环节") +
        this._industryKpi("背景连接", t.edge_count, "待核验，不代表已披露供货") +
        this._industryKpi("地区", t.country_count, "国家/区域") +
      '</section>' +
      '<section class="industry-grid">' +
        '<div class="industry-section industry-map-section"><div class="industry-section-hd"><h3>区域分布</h3><span>' + esc(String((overview.countries || []).length)) + ' 个市场</span></div><div id="industryGeoMap" class="industry-geo-map"><div class="ind-loading">加载地图...</div></div></div>' +
        '<div class="industry-section"><div class="industry-section-hd"><h3>关键节点</h3><span>按连接度</span></div><div class="industry-node-rank">' + this._renderIndustryKeyNodes(keyNodes) + '</div></div>' +
      '</section>' +
      this._renderIndustryContextPanels() +
      '<section class="industry-section"><div class="industry-section-hd"><h3>产业雷达</h3><span>' + esc(String(cats.length)) + ' 条链路</span></div><div class="industry-chain-grid">' +
        cats.map(function(c) { return App._renderIndustryCategoryCard(c); }).join("") +
      '</div></section>';
    inner.innerHTML = this._industryShell("overview", body);
    await this._renderIndustryGeoMap(overview.countries || []);
  },

  _industryKpi: function(label, value, meta) {
    return '<div class="industry-kpi"><span>' + esc(label) + '</span><strong>' + esc(String(value || 0)) + '</strong><em>' + esc(meta || "") + '</em></div>';
  },

  _renderIndustryContextStrip: function() {
    var summary = ((this._industryContext || {}).summary || {});
    var totals = summary.source_totals || {};
    var sources = [
      ["analysis", "Astock", totals.analysis || 0],
      ["vibe", "Vibe", totals.vibe || 0],
      ["report", "报告库", totals.report || 0],
      ["watchlist", "自选", totals.watchlist || 0]
    ];
    return '<section class="industry-context-strip">' +
      '<div class="industry-context-copy"><span>实时数据层</span><strong>' + esc(String(summary.linked_tickers || 0)) + ' 个标的已归链</strong><em>预置关系为待核验背景</em></div>' +
      '<div class="industry-context-sources">' + sources.map(function(source) {
        return '<span data-source="' + source[0] + '"><i></i><b>' + esc(source[1]) + '</b><em>' + esc(String(source[2])) + '</em></span>';
      }).join("") + '</div>' +
      '<div class="industry-context-coverage"><strong>' + esc(String(summary.coverage_pct || 0)) + '%</strong><span>归链覆盖</span></div>' +
    '</section>';
  },

  _renderIndustryContextPanels: function() {
    var context = this._industryContext || {};
    var events = context.recent_events || [];
    var unlinked = context.unlinked || [];
    return '<section class="industry-context-grid">' +
      '<div class="industry-section"><div class="industry-section-hd"><h3>跨模块动态</h3><span>' + esc(String(events.length)) + ' 条</span></div>' +
        '<div class="industry-context-events">' + (events.length ? events.slice(0, 8).map(function(event) {
          var category = (event.categories || [])[0] || "";
          return '<button type="button" data-c="' + escAttr(category) + '" data-t="' + escAttr(event.ticker || "") + '" onclick="App._openIndustryContext(this.dataset.c,this.dataset.t)">' +
            '<i class="source-' + escAttr(event.source || "") + '"></i><span><b>' + esc(event.name || event.ticker) + '</b><em>' + esc(event.source_label + (event.signal ? " · " + event.signal : "")) + '</em></span><time>' + esc(App._industryRelativeTime(event.at)) + '</time>' +
          '</button>';
        }).join("") : '<div class="ind-empty small">暂无跨模块动态</div>') + '</div>' +
      '</div>' +
      '<div class="industry-section"><div class="industry-section-hd"><h3>待归链</h3><span>' + esc(String((((context.summary || {}).unlinked_tickers) || 0))) + ' 个标的</span></div>' +
        '<div class="industry-unlinked">' + (unlinked.length ? unlinked.slice(0, 8).map(function(item) {
          return '<button type="button" data-t="' + escAttr(item.ticker || "") + '" onclick="App._industryGoMarket(this.dataset.t)">' +
            '<span><b>' + esc(item.ticker) + '</b><em>' + esc(item.name || "等待行业证据") + '</em></span><i>' + esc((item.sources || []).map(function(source) { return App._industrySourceName(source); }).join(" / ")) + '</i>' +
          '</button>';
        }).join("") : '<div class="ind-empty small">其他模块的标的均已归入现有链路</div>') + '</div>' +
      '</div>' +
    '</section>';
  },

  _openIndustryContext: function(category, ticker) {
    if (category) {
      this._selectIndustryChain(category);
      return;
    }
    if (ticker) this._industryGoMarket(ticker);
  },

  _renderIndustryCategoryCard: function(c) {
    var total = Math.max(1, Number(c.node_count || 0));
    var up = Math.round((c.upstream_count || 0) * 100 / total);
    var mid = Math.round((c.midstream_count || 0) * 100 / total);
    var down = Math.max(0, 100 - up - mid);
    var nodes = (c.top_nodes || []).slice(0, 3).map(function(n) {
      return '<span>' + esc(n.name) + (n.source_ticker ? '<em>' + esc(n.source_ticker) + '</em>' : '') + '</span>';
    }).join("");
    var live = (((this._industryContext || {}).categories || {})[c.category] || {});
    var latest = (live.signals || [])[0] || {};
    return '<button class="industry-chain-card" data-c="' + escAttr(c.category) + '" onclick="App._selectIndustryChain(this.dataset.c)">' +
      '<div class="industry-chain-card-hd"><strong>' + esc(c.category) + '</strong><span>' + esc(String(c.node_count || 0)) + ' 节点</span></div>' +
      (live.active_tickers ? '<div class="industry-chain-live"><i></i><span>' + esc(String(live.active_tickers)) + ' 个联动标的</span>' + (latest.signal ? '<em class="tone-' + escAttr(latest.tone || "neutral") + '">' + esc(latest.ticker + " " + latest.signal) + '</em>' : '<em>' + esc(this._industryRelativeTime(live.latest_at)) + '</em>') + '</div>' : '') +
      '<div class="industry-chain-bars"><i style="width:' + up + '%"></i><b style="width:' + mid + '%"></b><em style="width:' + down + '%"></em></div>' +
      '<div class="industry-chain-card-meta">' + esc(String(c.edge_count || 0)) + ' 关系 · ' + esc(String(c.country_count || 0)) + ' 国 · ' + esc(String(c.ticker_count || 0)) + ' 标的</div>' +
      '<div class="industry-chain-card-nodes">' + nodes + '</div>' +
    '</button>';
  },

  _renderIndustryKeyNodes: function(nodes) {
    if (!nodes.length) return '<div class="ind-empty small">暂无关键节点</div>';
    return nodes.slice(0, 12).map(function(n, idx) {
      var live = App._industryTickerContext(n.source_ticker);
      return '<button class="industry-node-row" data-id="' + escAttr(String(n.id)) + '" onclick="App._showIndustryNode(this.dataset.id)">' +
        '<strong>' + (idx + 1) + '</strong><span><b>' + esc(n.name) + '</b><em>' + esc((n.category || "") + (n.source_ticker ? " · " + n.source_ticker : "")) + '</em></span>' +
        (live ? '<mark title="跨模块已有最新数据"></mark>' : '') + '<i>' + esc(String(n.degree || 0)) + '</i></button>';
    }).join("");
  },

  _renderIndustryGeoMap: async function(countryRows) {
    var el = document.getElementById("industryGeoMap");
    if (!el || typeof echarts === "undefined") {
      if (el) el.innerHTML = '<div class="ind-empty">图表库未加载</div>';
      return;
    }
    try {
      if (!this._worldMap) {
        var wr = await fetch("/static/lib/world.json?v=77");
        this._worldMap = await wr.json();
      }
      echarts.registerMap("world", this._worldMap);
    } catch (e) {
      el.innerHTML = '<div class="ind-empty">地图资源加载失败</div>';
      return;
    }
    var geo = [];
    try {
      var gr = await this.api("/api/industry/geo");
      if (gr.ok) geo = (await gr.json()).regions || [];
    } catch (e) {}
    var counts = {};
    for (var c = 0; c < countryRows.length; c++) counts[countryRows[c].country] = countryRows[c];
    var data = [];
    for (var i = 0; i < geo.length; i++) {
      var g = geo[i], row = counts[g.country_code] || {};
      if (g.lat == null || g.lng == null) continue;
      data.push({name: g.country_name, code: g.country_code, value: [g.lng, g.lat, row.node_count || 0], industries: row.industry_count || 0, tickers: row.ticker_count || 0});
    }
    var bg = this._cssVar("--bg2", "#20242b");
    var border = this._cssVar("--border", "#3a3f49");
    var accent = this._cssVar("--accent", "#6b95c8");
    this._industryMapChart = echarts.init(el);
    this._industryMapChart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item",
        backgroundColor: "rgba(20,20,20,.92)",
        borderColor: border,
        textStyle: {color: "#eee", fontSize: 12},
        formatter: function(p) {
          if (!p.data) return p.name || "";
          return "<b>" + p.name + "</b><br/>节点 " + (p.data.value[2] || 0) + " · 产业 " + (p.data.industries || 0) + " · 标的 " + (p.data.tickers || 0);
        }
      },
      geo: {
        map: "world", roam: true, zoom: 1.18,
        itemStyle: {areaColor: bg, borderColor: border, borderWidth: .4},
        emphasis: {itemStyle: {areaColor: "rgba(107,149,200,.32)"}, label: {show: false}}
      },
      series: [{
        type: "scatter",
        coordinateSystem: "geo",
        data: data,
        symbolSize: function(v) { return Math.max(6, Math.min(34, (v[2] || 0) * 3 + 6)); },
        itemStyle: {color: accent, opacity: .85, shadowBlur: 10, shadowColor: accent}
      }]
    });
  },

  _renderIndustryChain: async function(cat, focusNodeId) {
    var sequence = this._industryChainSeq = (this._industryChainSeq || 0) + 1;
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    this._industryView = "chain";
    this._disposeIndustryCharts();
    inner.innerHTML = this._industryShell("chain", '<div class="industry-loading">加载产业链...</div>');
    try {
      var r = await this.api("/api/industry/graph?category=" + encodeURIComponent(cat) + "&depth=2&min_confidence=0");
      if (!r.ok) throw new Error("chain failed");
      var d = await r.json();
      if (sequence !== this._industryChainSeq) return;
      this._renderIndustryChainDetail(cat, d.nodes || [], d.edges || [], d.summary || {}, focusNodeId);
    } catch (e) {
      if (sequence !== this._industryChainSeq) return;
      inner.innerHTML = this._industryShell("chain", '<div class="ind-empty">产业链加载失败</div>');
    }
  },

  _renderIndustryChainByTicker: async function(ticker) {
    var sequence = this._industryChainSeq = (this._industryChainSeq || 0) + 1;
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    this._industryView = "chain";
    this._disposeIndustryCharts();
    inner.innerHTML = this._industryShell("chain", '<div class="industry-loading">加载标的链路...</div>');
    var nodes = [], edges = [];
    try {
      var r = await this.api("/api/industry/ticker/" + encodeURIComponent(ticker) + "/chain");
      if (r.ok) { var d = await r.json(); nodes = d.nodes || []; edges = d.edges || []; }
    } catch (e) {}
    if (sequence !== this._industryChainSeq) return;
    this._industryCategory = (nodes[0] && nodes[0].category) || ticker;
    this._renderIndustrySidebar();
    this._renderIndustryChainDetail(this._industryCategory, nodes, edges, {}, "", ticker);
  },

  _renderIndustryChainDetail: function(cat, nodes, edges, summary, focusNodeId, highlightTicker) {
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    if (!nodes.length) {
      inner.innerHTML = this._industryShell("chain", '<div class="ind-empty">暂无「' + esc(cat) + '」产业链数据</div>');
      return;
    }
    this._industryCategory = cat;
    this._industryChainNodes = nodes;
    this._industryChainEdges = edges;
    var tiers = [[], [], []];
    var countrySet = {};
    var tickerSet = {};
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i], t = n.tier == null ? 1 : Number(n.tier);
      if (t < 0 || t > 2) t = 1;
      tiers[t].push(n);
      if (n.country) countrySet[n.country] = true;
      if (n.source_ticker) tickerSet[n.source_ticker] = true;
    }
    var body =
      '<section class="industry-chain-hero">' +
        '<div><span>INDUSTRY CHAIN</span><h3>' + esc(cat) + '</h3><p>' + esc(this._industryChainThesis(nodes, edges)) + '</p></div>' +
        '<div class="industry-chain-actions"><button onclick="App._backToMap()">返回总览</button><button onclick="App._industryAskChain()">交给 AI 深挖</button></div>' +
      '</section>' +
      '<section class="industry-kpis compact">' +
        this._industryKpi("节点", nodes.length, "链路环节") +
        this._industryKpi("关系", edges.length, "背景连接 · 待核验") +
        this._industryKpi("地区", Object.keys(countrySet).length, "区域分布") +
        this._industryKpi("标的", Object.keys(tickerSet).length, "可联动行情") +
      '</section>' +
      this._renderIndustryChainLive(cat) +
      '<section class="industry-chain-layout">' +
        '<div class="industry-stage-board"><div class="industry-stage-cols" id="industryStageCols">' +
          tiers.map(function(arr, idx) { return App._renderIndustryTier(idx, arr, focusNodeId, highlightTicker); }).join("") +
          '<svg class="industry-stage-edges" id="industryStageEdges"></svg>' +
        '</div></div>' +
        '<aside class="industry-chain-aside">' +
          '<div class="industry-section"><div class="industry-section-hd"><h3>连接度观察</h3><span>不代表供给瓶颈</span></div>' + this._renderIndustryBottlenecks(nodes, edges) + '</div>' +
          '<div class="industry-section"><div class="industry-section-hd"><h3>区域露出</h3><span>' + esc(String(Object.keys(countrySet).length)) + ' 地区</span></div><div id="industryMiniMap" class="industry-mini-map"></div></div>' +
        '</aside>' +
      '</section>';
    inner.innerHTML = this._industryShell("chain", body);
    var self = this;
    requestAnimationFrame(function() { requestAnimationFrame(function() { self._drawIndustryStageEdges(); }); });
    this._renderIndustryMiniMap(nodes);
    if (focusNodeId) setTimeout(function() { self._showIndustryNode(focusNodeId); }, 100);
  },

  _industryChainThesis: function(nodes, edges) {
    var upstream = nodes.filter(function(n) { return Number(n.tier || 0) === 0; }).length;
    var mid = nodes.filter(function(n) { return Number(n.tier || 0) === 1; }).length;
    var down = nodes.filter(function(n) { return Number(n.tier || 0) === 2; }).length;
    return "上游 " + upstream + " / 中游 " + mid + " / 下游 " + down + "，共 " + edges.length + " 条关系。";
  },

  _renderIndustryChainLive: function(category) {
    var context = (((this._industryContext || {}).categories || {})[category] || {});
    if (!context.active_tickers) return '';
    var totals = context.source_counts || {};
    var signals = context.signals || [];
    return '<section class="industry-chain-context"><div><span>跨模块信号</span><strong>' + esc(String(context.active_tickers)) + ' 个标的有新数据</strong><em>' + esc(this._industryRelativeTime(context.latest_at)) + '</em></div>' +
      '<div class="industry-chain-context-sources">' +
        [["Astock",totals.analysis],["Vibe",totals.vibe],["报告",totals.report],["自选",totals.watchlist]].filter(function(item) { return item[1]; }).map(function(item) { return '<span>' + esc(item[0]) + ' ' + esc(String(item[1])) + '</span>'; }).join("") +
      '</div><div class="industry-chain-context-signals">' + signals.slice(0, 3).map(function(signal) {
        return '<button type="button" class="tone-' + escAttr(signal.tone || "neutral") + '" data-t="' + escAttr(signal.ticker) + '" onclick="App._industryGoAnalysis(this.dataset.t,\'\')"><b>' + esc(signal.ticker) + '</b><span>' + esc(signal.signal) + '</span></button>';
      }).join("") + '</div></section>';
  },

  _renderIndustryTier: function(idx, arr, focusNodeId, highlightTicker) {
    return '<div class="industry-tier">' +
      '<div class="industry-tier-hd"><strong>' + esc(this._industryTierLabels[idx]) + '</strong><span>' + esc(this._industryTierHints[idx]) + '</span></div>' +
      '<div class="industry-tier-list">' + (arr.length ? arr.map(function(n) {
        return App._renderIndustryNodeCard(n, focusNodeId, highlightTicker);
      }).join("") : '<div class="ind-empty small">暂无节点</div>') + '</div>' +
    '</div>';
  },

  _renderIndustryNodeCard: function(n, focusNodeId, highlightTicker) {
    var active = focusNodeId && String(n.id) === String(focusNodeId);
    var hl = highlightTicker && n.source_ticker === highlightTicker;
    var cls = active || hl ? " active" : "";
    var conf = n.confidence == null ? "" : Math.round(Number(n.confidence) * 100) + "%";
    var share = n.market_share == null ? "" : Math.round(Number(n.market_share) * 100) + "%";
    var live = this._industryTickerContext(n.source_ticker);
    return '<button class="industry-node-card' + cls + '" data-node="' + escAttr(String(n.id)) + '" data-tier="' + escAttr(String(Number(n.tier || 0))) + '" onmouseenter="App._highlightIndustryEdges(this.dataset.node,true)" onmouseleave="App._highlightIndustryEdges(this.dataset.node,false)" onclick="App._showIndustryNode(this.dataset.node)">' +
      '<div class="industry-node-card-hd"><strong>' + esc(n.name) + '</strong>' + (n.source_ticker ? '<span>' + esc(n.source_ticker) + '</span>' : '') + '</div>' +
      '<p>' + esc(n.description || "产业节点") + '</p>' +
      '<div class="industry-node-card-ft">' + (n.country ? '<em>' + esc(n.country) + '</em>' : '') + (share ? '<em>市占 ' + esc(share) + '</em>' : '') + (conf ? '<em>置信 ' + esc(conf) + '</em>' : '') + this._renderIndustryTickerBadges(live) + '</div>' +
    '</button>';
  },

  _renderIndustryTickerBadges: function(context) {
    if (!context) return '';
    var badges = (context.sources || []).map(function(source) {
      return '<em class="live-source source-' + escAttr(source) + '">' + esc(App._industrySourceName(source)) + '</em>';
    }).join("");
    if (context.latest_signal) badges += '<em class="live-signal tone-' + escAttr(context.signal_tone || "neutral") + '">' + esc(context.latest_signal) + '</em>';
    return badges;
  },

  _renderIndustryBottlenecks: function(nodes, edges) {
    var degree = {};
    for (var i = 0; i < edges.length; i++) {
      degree[edges[i].source_node_id] = (degree[edges[i].source_node_id] || 0) + 1;
      degree[edges[i].target_node_id] = (degree[edges[i].target_node_id] || 0) + 1;
    }
    return nodes.slice().sort(function(a, b) {
      return (degree[b.id] || 0) - (degree[a.id] || 0) || (b.market_share || 0) - (a.market_share || 0);
    }).slice(0, 6).map(function(n, idx) {
      return '<button class="industry-bottleneck" data-node="' + escAttr(String(n.id)) + '" onclick="App._showIndustryNode(this.dataset.node)">' +
        '<strong>' + (idx + 1) + '</strong><span><b>' + esc(n.name) + '</b><em>' + esc((n.description || "") + (n.source_ticker ? " · " + n.source_ticker : "")) + '</em></span><i>' + esc(String(degree[n.id] || 0)) + '</i></button>';
    }).join("");
  },

  _drawIndustryStageEdges: function() {
    var cols = document.getElementById("industryStageCols");
    var svg = document.getElementById("industryStageEdges");
    if (!cols || !svg || !this._industryChainEdges) return;
    var rect = cols.getBoundingClientRect();
    svg.setAttribute("width", rect.width);
    svg.setAttribute("height", rect.height);
    var pts = {};
    cols.querySelectorAll(".industry-node-card").forEach(function(card) {
      var r = card.getBoundingClientRect();
      pts[card.getAttribute("data-node")] = {
        l: r.left - rect.left,
        r: r.right - rect.left,
        cy: r.top - rect.top + r.height / 2,
        tier: Number(card.getAttribute("data-tier") || 0)
      };
    });
    var paths = "";
    for (var e = 0; e < this._industryChainEdges.length; e++) {
      var ed = this._industryChainEdges[e], s = pts[ed.source_node_id], t = pts[ed.target_node_id];
      if (!s || !t) continue;
      // Only render the adjacent left-to-right primary flow. Other relations
      // remain available in the node drawer and bottleneck statistics.
      if (t.tier !== s.tier + 1 || t.l - s.r < 16) continue;
      var mx = (s.r + t.l) / 2;
      paths += '<path class="industry-edge" data-s="' + escAttr(ed.source_node_id) + '" data-t="' + escAttr(ed.target_node_id) + '" d="M' +
        s.r.toFixed(0) + "," + s.cy.toFixed(0) + " C" + mx.toFixed(0) + "," + s.cy.toFixed(0) + " " + mx.toFixed(0) + "," + t.cy.toFixed(0) + " " + t.l.toFixed(0) + "," + t.cy.toFixed(0) + '"/>';
    }
    svg.innerHTML = paths;
  },

  _highlightIndustryEdges: function(nodeId, on) {
    var svg = document.getElementById("industryStageEdges");
    if (!svg) return;
    svg.querySelectorAll(".industry-edge").forEach(function(p) {
      var hit = p.getAttribute("data-s") === nodeId || p.getAttribute("data-t") === nodeId;
      p.classList.toggle("active", !!on && hit);
      p.classList.toggle("muted", !!on && !hit);
    });
  },

  _renderIndustryMiniMap: async function(nodes) {
    var el = document.getElementById("industryMiniMap");
    if (!el || typeof echarts === "undefined") return;
    try {
      if (!this._worldMap) {
        var wr = await fetch("/static/lib/world.json?v=77");
        this._worldMap = await wr.json();
      }
      echarts.registerMap("world", this._worldMap);
    } catch (e) { return; }
    var counts = {};
    for (var i = 0; i < nodes.length; i++) if (nodes[i].country) counts[nodes[i].country] = (counts[nodes[i].country] || 0) + 1;
    var geo = [];
    try {
      var gr = await this.api("/api/industry/geo");
      if (gr.ok) geo = (await gr.json()).regions || [];
    } catch (e) {}
    var data = [];
    for (var j = 0; j < geo.length; j++) {
      var g = geo[j], count = counts[g.country_code] || 0;
      if (!count || g.lat == null || g.lng == null) continue;
      data.push({name: g.country_name, value: [g.lng, g.lat, count]});
    }
    var bg = this._cssVar("--bg2", "#20242b");
    var border = this._cssVar("--border", "#3a3f49");
    this._industryMiniMapChart = echarts.init(el);
    this._industryMiniMapChart.setOption({
      backgroundColor: "transparent",
      tooltip: {trigger: "item"},
      geo: {map: "world", roam: false, silent: true, zoom: 1.08, itemStyle: {areaColor: bg, borderColor: border, borderWidth: .3}},
      series: [{type: "scatter", coordinateSystem: "geo", data: data, symbolSize: function(v) { return Math.max(6, Math.min(24, v[2] * 6)); }}]
    });
  },

  _showIndustryNode: async function(nodeId) {
    if (!nodeId) return;
    var sequence = this._industryNodeSeq = (this._industryNodeSeq || 0) + 1;
    var r = await this.api("/api/industry/node/" + encodeURIComponent(nodeId));
    if (sequence !== this._industryNodeSeq) return;
    if (!r.ok) return;
    var d = await r.json();
    if (sequence !== this._industryNodeSeq) return;
    this._renderIndustryNodeDrawer(d.node || {}, d.upstream || [], d.downstream || []);
  },

  _renderIndustryNodeDrawer: function(n, upstream, downstream) {
    var drawer = document.getElementById("industryDrawer");
    if (!drawer) return;
    var tier = n.tier == null ? "" : (this._industryTierLabels[Number(n.tier)] || "");
    var share = n.market_share == null ? "" : Math.round(Number(n.market_share) * 100) + "%";
    var conf = n.confidence == null ? "" : Math.round(Number(n.confidence) * 100) + "%";
    var live = this._industryTickerContext(n.source_ticker);
    drawer.innerHTML =
      '<div class="industry-drawer-head"><div><span>' + esc(n.category || "产业节点") + '</span><h3>' + esc(n.name || "-") + '</h3></div><button onclick="App._closeIndustryDrawer()">×</button></div>' +
      '<div class="industry-drawer-tags">' +
        (n.source_ticker ? '<em>' + esc(n.source_ticker) + '</em>' : '') +
        (tier ? '<em>' + esc(tier) + '</em>' : '') +
        (n.country ? '<em>' + esc(n.country) + '</em>' : '') +
      '</div>' +
      (n.description ? '<p class="industry-drawer-desc">' + esc(n.description) + '</p>' : '') +
      '<div class="industry-drawer-metrics">' +
        '<span><b>' + esc(share || "待核验") + '</b><em>市占率</em></span>' +
        '<span><b>' + esc(conf || "未核验") + '</b><em>证据状态</em></span>' +
        '<span><b>' + esc(String(upstream.length)) + '</b><em>上游</em></span>' +
        '<span><b>' + esc(String(downstream.length)) + '</b><em>下游</em></span>' +
      '</div>' +
      '<p class="industry-drawer-desc">背景结构与历史研究均需核对原始披露；连线不等于已确认供货关系。</p>' +
      this._renderIndustryDrawerContext(live) +
      this._drawerList("上游连接", upstream) +
      this._drawerList("下游连接", downstream) +
      '<div class="industry-drawer-actions">' +
        (n.source_ticker ? '<button data-tk="' + escAttr(String(n.source_ticker)) + '" data-nm="' + escAttr(String(n.name || "")) + '" onclick="App._industryGoAnalysis(this.dataset.tk,this.dataset.nm)">一键分析</button><button data-tk="' + escAttr(String(n.source_ticker)) + '" onclick="App._industryGoMarket(this.dataset.tk)">行情</button>' : '') +
        '<button data-c="' + escAttr(String(n.category || "")) + '" onclick="App._selectIndustryChain(this.dataset.c)">查看链路</button>' +
      '</div>';
    drawer.classList.add("open");
  },

  _renderIndustryDrawerContext: function(context) {
    if (!context) return '';
    var evidence = context.evidence || [];
    return '<div class="industry-drawer-context"><div class="industry-drawer-context-hd"><span>跨模块动态</span><em>' + esc(this._industryRelativeTime(context.latest_at)) + '</em></div>' +
      (context.latest_signal ? '<div class="industry-drawer-signal tone-' + escAttr(context.signal_tone || "neutral") + '"><span>最新评级</span><strong>' + esc(context.latest_signal) + '</strong></div>' : '') +
      '<div class="industry-drawer-evidence">' + evidence.slice(0, 5).map(function(item) {
        return '<div><i class="source-' + escAttr(item.source || "") + '"></i><span><b>' + esc(item.label || item.source_label || "联动记录") + '</b><em>' + esc((item.detail || item.source_label || "") + " · " + App._industryRelativeTime(item.at)) + '</em></span></div>';
      }).join("") + '</div></div>';
  },

  _drawerList: function(title, arr) {
    if (!arr || !arr.length) return "";
    return '<div class="industry-drawer-list"><h4>' + esc(title) + '</h4>' + arr.map(function(x) {
      return '<button data-node="' + escAttr(String(x.id)) + '" onclick="App._showIndustryNode(this.dataset.node)">' +
        '<span>' + esc(x.name) + '</span><em>' + esc((x.relation === "background_link" ? "背景关系 · 待核验" : (x.relation || "待核验")) + (x.source_ticker ? " · " + x.source_ticker : "")) + '</em></button>';
    }).join("") + '</div>';
  },

  _closeIndustryDrawer: function() {
    this._industryNodeSeq = (this._industryNodeSeq || 0) + 1;
    var d = document.getElementById("industryDrawer");
    if (d) d.classList.remove("open");
  },

  _industryGoAnalysis: function(ticker, name) {
    this._analysisTicker = ticker;
    this._analysisName = name || ticker;
    this.switchMode("analysis");
  },

  _industryGoMarket: function(ticker) {
    this._currentTicker = ticker;
    this.switchMode("market");
  },

  _industryAskChain: function() {
    var cat = this._industryCategory || "";
    if (!cat) return;
    this.switchMode("chat");
    var input = document.getElementById("input");
    if (input) {
      input.value = "用供应链瓶颈框架分析「" + cat + "」产业链，重点找上游卡点、被忽视环节、A股候选和验证信号。";
      input.focus();
    }
  }
});
