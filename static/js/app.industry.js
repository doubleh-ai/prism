/* ═══════════════════════════════════════
   Prism — Industry chain panel (v0.11)
   Map overview + layered chain drill-down.
   ═══════════════════════════════════════ */
Object.assign(App, {

  _industryView: "map",        // 'map' | 'chain'
  _industryCategory: "",
  _industryMapChart: null,
  _industryChainEdges: [],
  _industryCategories: [],
  _worldMap: null,

  _industryChainIcons: {
    "锂电池": "🔋", "光伏": "☀️", "半导体": "💾", "稀土": "⛏",
    "白酒": "🍶", "创新药": "💊", "电动车": "🚗", "消费电子": "📱",
    "军工": "🎖", "钢铁": "🏗"
  },
  _industryTierLabels: ["上游 · 原材料", "中游 · 制造", "下游 · 应用"],

  /* ── lifecycle (called from switchMode) ── */
  _initIndustry: function() {
    this._loadIndustrySidebar();
    if (this._industryView === "chain" && this._industryCategory) {
      this._renderIndustryChain(this._industryCategory);
    } else {
      this._renderIndustryMap();
    }
  },

  _leaveIndustry: function() {
    if (this._industryMapChart) {
      try { this._industryMapChart.dispose(); } catch (e) {}
      this._industryMapChart = null;
    }
  },

  /* ── sidebar ── */
  _loadIndustrySidebar: async function() {
    try {
      var r = await this.api("/api/industry/categories");
      if (r.ok) this._industryCategories = (await r.json()).categories || [];
    } catch (e) {}
    this._renderIndustrySidebar();
  },

  _renderIndustrySidebar: function() {
    var el = document.getElementById("industrySidebarList");
    if (!el) return;
    var h = '<div class="ind-chain-list">';
    for (var i = 0; i < this._industryCategories.length; i++) {
      var c = this._industryCategories[i];
      h += '<div class="ind-chain-item' + (c.category === this._industryCategory ? " active" : "") +
        '" onclick="App._selectIndustryChain(this.getAttribute(\'data-c\'))" data-c="' + escAttr(c.category) + '">' +
        '<span class="ind-chain-ic">' + (this._industryChainIcons[c.category] || "🔗") + "</span>" +
        "<span class=\"ind-chain-nm\">" + esc(c.category) + "</span>" +
        '<span class="ind-chain-cnt">' + c.node_count + "</span></div>";
    }
    h += "</div>";
    h += '<div class="ind-watchlist-block"><div class="sidebar-mode-title">自选股（点选看链路）</div>';
    var wl = this._watchlist || [];
    if (wl.length) {
      for (var j = 0; j < wl.length; j++) {
        h += '<div class="ind-wl-item" onclick="App._selectIndustryTicker(this.getAttribute(\'data-t\'))" data-t="' +
          escAttr(wl[j].ticker) + '">' + esc(wl[j].ticker) + " " + esc(wl[j].name || "") + "</div>";
      }
    } else {
      h += '<div class="ind-empty">暂无自选股</div>';
    }
    h += "</div>";
    el.innerHTML = h;
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
    this._industryView = "map";
    this._industryCategory = "";
    this._renderIndustrySidebar();
    this._renderIndustryMap();
  },

  _cssVar: function(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    } catch (e) { return fallback; }
  },

  /* ── MAP view ── */
  _renderIndustryMap: async function() {
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    inner.innerHTML = this._industryTabs("map") +
      '<div id="indMap" class="ind-map"><div class="ind-loading">加载地图…</div></div>';

    if (typeof echarts === "undefined") {
      document.getElementById("indMap").innerHTML = '<div class="ind-empty">图表库未加载</div>';
      return;
    }
    try {
      if (!this._worldMap) {
        var r = await fetch("/static/lib/world.json?v=28");
        this._worldMap = await r.json();
        echarts.registerMap("world", this._worldMap);
      }
    } catch (e) {
      document.getElementById("indMap").innerHTML = '<div class="ind-empty">世界地图资源加载失败</div>';
      return;
    }

    var geo = [];
    try {
      var gr = await this.api("/api/industry/geo");
      if (gr.ok) geo = (await gr.json()).regions || [];
    } catch (e) {}

    // Bubble per country sized by node count
    var counts = {};
    try {
      var cr = await this.api("/api/industry/categories");
    } catch (e) {}
    var data = [];
    for (var i = 0; i < geo.length; i++) {
      var g = geo[i];
      if (g.lat == null || g.lng == null) continue;
      var n = g.node_count || 0;
      data.push({
        name: g.country_name, value: [g.lng, g.lat, n],
        cnt: n, industries: g.key_industries || []
      });
      counts[g.country_code] = n;
    }

    var bg = this._cssVar("--bg3", "#2a2a2a");
    var accent = this._cssVar("--accent", "#3b5d8a");
    var border = this._cssVar("--border", "#3a3a3a");
    var txt = this._cssVar("--text3", "#888");

    if (this._industryMapChart) { try { this._industryMapChart.dispose(); } catch (e) {} }
    var chart = echarts.init(document.getElementById("indMap"));
    this._industryMapChart = chart;
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "item", backgroundColor: "rgba(20,20,20,.92)", borderColor: border,
        textStyle: { color: "#eee", fontSize: 12 },
        formatter: function(p) {
          if (!p.data || !p.data.industries) return p.name;
          var ind = p.data.industries.length ? "<br/>" + p.data.industries.join("、") : "";
          return "<b>" + p.name + "</b> · " + (p.data.cnt || 0) + " 节点" + ind;
        }
      },
      geo: {
        map: "world", roam: true, silent: false, zoom: 1.2,
        itemStyle: { areaColor: bg, borderColor: border, borderWidth: 0.4 },
        emphasis: { itemStyle: { areaColor: accent }, label: { show: false } }
      },
      series: [{
        type: "scatter", coordinateSystem: "geo", data: data,
        symbolSize: function(v) { return Math.max(8, Math.min(40, (v[2] || 0) * 3 + 8)); },
        itemStyle: { color: accent, opacity: 0.85, shadowBlur: 8, shadowColor: accent },
        label: { show: false }
      }]
    });
    var self = this;
    if (!this._industryMapResize) {
      this._industryMapResize = function() { if (self._industryMapChart) { try { self._industryMapChart.resize(); } catch (e) {} } };
      window.addEventListener("resize", this._industryMapResize);
    }
  },

  /* ── CHAIN view (layered cards) ── */
  _renderIndustryChain: async function(cat) {
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    inner.innerHTML = this._industryTabs("chain") +
      '<div class="ind-unified"><div class="ind-loading">加载产业链…</div></div>';
    try {
      var r = await this.api("/api/industry/graph?category=" + encodeURIComponent(cat) + "&depth=2&min_confidence=0");
      if (r.ok) { var d = await r.json(); this._renderUnifiedGraph(cat, d.nodes || [], d.edges || []); }
    } catch (e) {}
  },

  _renderIndustryChainByTicker: async function(ticker) {
    var inner = document.getElementById("industryPanelInner");
    if (!inner) return;
    inner.innerHTML = this._industryTabs("chain") +
      '<div class="ind-unified"><div class="ind-loading">加载链路…</div></div>';
    var r = await this.api("/api/industry/ticker/" + encodeURIComponent(ticker) + "/chain");
    var nodes = [], edges = [];
    if (r.ok) { var d = await r.json(); nodes = d.nodes || []; edges = d.edges || []; }
    this._industryCategory = (nodes[0] && nodes[0].category) || ticker;
    this._renderUnifiedGraph(this._industryCategory, nodes, edges, ticker);
  },

  _renderUnifiedGraph: function(cat, nodes, edges, hlTicker) {
    var unwrap = document.querySelector(".ind-unified");
    if (!unwrap) return;
    if (!nodes.length) { unwrap.innerHTML = '<div class="ind-empty">暂无「' + esc(cat) + '」产业链数据</div>'; return; }
    this._industryCategory = cat;
    this._industryChainNodes = nodes;
    this._industryChainEdges = edges;

    var cc = {"CN":"#3b82f6","AU":"#10b981","CL":"#f59e0b","US":"#ef4444","JP":"#f59e0b",
      "KR":"#8b5cf6","TW":"#ec4899","DE":"#6366f1"};
    var tiers = [[],[],[]], countrySet = {};
    for (var i=0;i<nodes.length;i++) {
      var n=nodes[i], t=n.tier!=null?n.tier:1; if (t<0||t>2) t=1;
      tiers[t].push(n); if (n.country) countrySet[n.country]=true;
    }
    var legend = "";
    for (var ck in countrySet) legend += '<span class="ind-legend-dot" style="background:' +
      (cc[ck]||"#777") + '" title="' + ck + '"></span>' + ck + "  ";

    unwrap.innerHTML = '<div class="ind-cardchain-hd"><h3>' +
      (this._industryChainIcons[cat]||"🔗") + " " + esc(cat) +
      '</h3><div class="ind-cardchain-meta">' + nodes.length + " 节点 · " +
      edges.length + " 关系  <span class=\"ind-legend\">" + legend + "</span></div></div>" +
      '<div class="ind-cardchain-row"><div class="ind-cardchain-cols" id="indCardCols">';
    for (var ti=0;ti<3;ti++) {
      unwrap.innerHTML += '<div class="ind-cardchain-tier"><div class="ind-cardchain-tier-hd">' +
        this._industryTierLabels[ti] + '</div>';
      for (var ni=0;ni<tiers[ti].length;ni++) {
        var nd = tiers[ti][ni], isHL = hlTicker && nd.source_ticker===hlTicker;
        var ccode = nd.country||"", ccol = cc[ccode]||"#777";
        var emoji = ccode==="AU"?"🇦🇺":ccode==="CL"?"🇨🇱":ccode==="US"?"🇺🇸":"";
        unwrap.innerHTML += '<div class="ind-cardchain-card '+(isHL?"hl":"")+'" data-node="' + escAttr(String(nd.id)) + '" style="border-left:3px solid ' + ccol + '" ' +
          'onclick="App._showIndustryNode(this.dataset.node)" ' +
          'onmouseenter="App._highlightChainEdges(this.dataset.node,true)" ' +
          'onmouseleave="App._highlightChainEdges(this.dataset.node,false)">' +
          '<div class="ind-cardchain-nm">'+esc(nd.name)+
          (nd.source_ticker?' <span class="ind-cardchain-tk">'+esc(nd.source_ticker)+'</span>':'')+
          (emoji?' <span style="float:right">'+emoji+'</span>':'')+'</div>'+
          (nd.description?'<div class="ind-cardchain-role">'+esc(nd.description)+'</div>':'')+
          (nd.market_share!=null?'<div class="ind-cardchain-ms">市占 '+(nd.market_share*100).toFixed(0)+'%</div>':'')+
          '</div>';
      }
      unwrap.innerHTML += '</div>';
    }
    unwrap.innerHTML += '</div><svg class="ind-cardchain-edges" id="indCardEdgesSvg"></svg></div>' +
      '<div class="ind-cardchain-map" id="indCardMap"></div>';

    // Draw edges after layout (use rAF for reliable post-render timing)
    var self = this;
    requestAnimationFrame(function(){requestAnimationFrame(function(){self._drawCardEdges();});});
    if (!this._chainEdgeResize) {
      this._chainEdgeResize = function(){if(document.getElementById("indCardCols"))self._drawCardEdges();};
      window.addEventListener("resize",this._chainEdgeResize);
    }

    // Mini map (distribution by country)
    if (typeof echarts !== "undefined" && this._worldMap) {
      try { echarts.registerMap("world", this._worldMap); } catch(e) {}
      var mdiv = document.getElementById("indCardMap"), mchart = echarts.init(mdiv);
      var bg = this._cssVar("--bg2","#2a2a2a"), bd = this._cssVar("--border","#3a3a3a");
      var countryCnt = {}; for (var i=0;i<nodes.length;i++){var c=(nodes[i].country||"_");countryCnt[c]=(countryCnt[c]||0)+1;}
      this.api("/api/industry/geo").then(function(r){
        if(!r.ok)return; r.json().then(function(geo){
          var bdata=[], regs=geo.regions||[];
          for(var ri=0;ri<regs.length;ri++){var rg=regs[ri];if(rg.lat==null||rg.lng==null)continue;
            var nc=countryCnt[rg.country_code]||0, col=nc?(cc[rg.country_code]||"#777"):"transparent";
            bdata.push({name:rg.country_name,value:[rg.lng,rg.lat,nc],cnt:nc,itemStyle:{color:col,opacity:nc?.6:.1}});}
          mchart.setOption({backgroundColor:"transparent",
            geo:{map:"world",roam:false,silent:true,zoom:1.08,itemStyle:{areaColor:bg,borderColor:bd,borderWidth:.3}},
            series:[{type:"scatter",coordinateSystem:"geo",data:bdata,
              symbolSize:function(v){return Math.max(4,Math.min(24,(v[2]||0)*5))}}]});
        });
      });
    }
  },

  _drawCardEdges: function(){
    var cols=document.getElementById("indCardCols"), svg=document.getElementById("indCardEdgesSvg");
    if(!cols||!svg||!this._industryChainEdges) return;
    var rect=cols.getBoundingClientRect();svg.setAttribute("width",rect.width);svg.setAttribute("height",rect.height);
    var pts={};cols.querySelectorAll(".ind-cardchain-card").forEach(function(c){
      var r=c.getBoundingClientRect(),id=c.getAttribute("data-node");
      pts[id]={l:r.left-rect.left,r:r.right-rect.left,cy:r.top-rect.top+r.height/2};
    });
    var paths="",edges=this._industryChainEdges;
    for(var e=0;e<edges.length;e++){var s=pts[edges[e].source_node_id],t=pts[edges[e].target_node_id];
      if(!s||!t)continue;var mx=(s.r+t.l)/2;
      paths+='<path class="ind-ce" data-s="'+edges[e].source_node_id+'" data-t="'+edges[e].target_node_id+
        '" d="M'+s.r.toFixed(0)+','+s.cy.toFixed(0)+' C'+mx.toFixed(0)+','+s.cy.toFixed(0)+
        ' '+mx.toFixed(0)+','+t.cy.toFixed(0)+' '+t.l.toFixed(0)+','+t.cy.toFixed(0)+
        '" stroke="rgba(107,149,200,.35)" stroke-width="1.6" fill="none"/>';}
    svg.innerHTML=paths;
  },

  _highlightChainEdges: function(nid,on){
    var svg=document.getElementById("indCardEdgesSvg");if(!svg)return;
    svg.querySelectorAll(".ind-ce").forEach(function(p){
      p.setAttribute("stroke", (on&&(p.getAttribute("data-s")===nid||p.getAttribute("data-t")===nid))?"rgba(34,197,94,.85)":"rgba(107,149,200,.35)");
    });
    var card=document.querySelector('.ind-cardchain-card[data-node="'+nid+'"]');
    if(card) card.classList.toggle("hl",on);
  },

  _renderChainCanvas: function(cat, nodes, edges, highlightTicker) {
    var inner = document.getElementById("industryPanelInner");
    var wrap = inner.querySelector(".ind-chain-wrap");
    if (!nodes.length) {
      wrap.innerHTML = '<div class="ind-empty">暂无「' + esc(cat) + '」产业链数据</div>';
      return;
    }
    // group by tier
    var byTier = [[], [], []];
    for (var i = 0; i < nodes.length; i++) {
      var t = nodes[i].tier || 0;
      if (t < 0 || t > 2) t = 1;
      byTier[t].push(nodes[i]);
    }
    var h = '<div class="ind-chain-head"><h3>' + (this._industryChainIcons[cat] || "🔗") + " " + esc(cat) +
      '</h3><span class="ind-chain-sub">' + nodes.length + " 节点 · " + edges.length + " 关系</span></div>";
    h += '<div class="ind-chain-cols" id="indChainCols">';
    for (var ti = 0; ti < 3; ti++) {
      h += '<div class="ind-tier" data-tier="' + ti + '"><div class="ind-tier-hd">' +
        this._industryTierLabels[ti] + "</div>";
      for (var ni = 0; ni < byTier[ti].length; ni++) {
        h += this._chainCard(byTier[ti][ni], highlightTicker);
      }
      h += "</div>";
    }
    h += '</div><svg class="ind-chain-edges" id="indChainEdges"></svg>';
    wrap.innerHTML = h;

    this._industryChainEdges = edges;
    var self = this;
    // draw edges after layout settles
    setTimeout(function() { self._drawChainEdges(); }, 30);
    if (!this._chainEdgeResize) {
      this._chainEdgeResize = function() { if (document.getElementById("indChainCols")) self._drawChainEdges(); };
      window.addEventListener("resize", this._chainEdgeResize);
    }
  },

  _chainCard: function(n, hl) {
    var isHL = hl && n.source_ticker === hl;
    var ms = (n.market_share != null) ? (Math.round(n.market_share * 100) + "%") : "";
    return '<div class="ind-card' + (isHL ? " hl" : "") + '" data-node="' + escAttr(String(n.id)) + '" ' +
      'onclick="App._showIndustryNode(this.dataset.node)">' +
      '<div class="ind-card-nm">' + esc(n.name) +
      (n.source_ticker ? '<span class="ind-card-tk">' + esc(n.source_ticker) + "</span>" : "") + "</div>" +
      (n.description ? '<div class="ind-card-role">' + esc(n.description) + "</div>" : "") +
      (ms ? '<div class="ind-card-ms">市占 ' + ms + "</div>" : "") +
      "</div>";
  },

  _drawChainEdges: function() {
    var svg = document.getElementById("indChainEdges");
    var cols = document.getElementById("indChainCols");
    if (!svg || !cols) return;
    var wrap = svg.parentElement;
    var W = wrap.clientWidth, H = wrap.clientHeight;
    svg.setAttribute("width", W); svg.setAttribute("height", H);
    // map node id → anchor point (right-center of card)
    var pts = {};
    var cards = wrap.querySelectorAll(".ind-card");
    for (var i = 0; i < cards.length; i++) {
      var id = cards[i].getAttribute("data-node");
      var r = cards[i].getBoundingClientRect();
      var wr = wrap.getBoundingClientRect();
      pts[id] = { l: r.left - wr.left, r: r.right - wr.left, cy: r.top - wr.top + r.height / 2 };
    }
    var accent = this._cssVar("--accent", "#3b5d8a");
    var paths = "";
    for (var e = 0; e < this._industryChainEdges.length; e++) {
      var ed = this._industryChainEdges[e];
      var s = pts[ed.source_node_id], t = pts[ed.target_node_id];
      if (!s || !t) continue;
      var mx = (s.r + t.l) / 2;
      paths += '<path d="M' + s.r.toFixed(0) + "," + s.cy.toFixed(0) +
        " C" + mx.toFixed(0) + "," + s.cy.toFixed(0) + " " + mx.toFixed(0) + "," + t.cy.toFixed(0) +
        " " + t.l.toFixed(0) + "," + t.cy.toFixed(0) +
        '" stroke="' + accent + '" stroke-width="1.4" fill="none" opacity="0.5" />';
    }
    svg.innerHTML = paths;
  },

  /* ── detail drawer ── */
  _showIndustryNode: async function(nodeId) {
    if (!nodeId) return;
    var r = await this.api("/api/industry/node/" + encodeURIComponent(nodeId));
    if (!r.ok) return;
    var d = await r.json();
    var drawer = document.getElementById("industryDrawer");
    if (!drawer) return;
    var n = d.node || {};
    var h = '<div class="ind-drawer-hd"><b>' + esc(n.name) + "</b>" +
      (n.source_ticker ? '<span class="ind-card-tk">' + esc(n.source_ticker) + "</span>" : "") +
      '<button class="ind-drawer-x" onclick="App._closeIndustryDrawer()">×</button></div>';
    h += '<div class="ind-drawer-meta">' + esc(n.category || "") + " · " + esc(this._industryTierLabels[n.tier] || "") +
      (n.country ? " · " + esc(n.country) : "") + "</div>";
    if (n.market_share != null) h += '<div class="ind-drawer-ms">市占率 ' + Math.round(n.market_share * 100) + "%</div>";
    h += this._drawerList("上游", d.upstream);
    h += this._drawerList("下游", d.downstream);
    if (n.source_ticker) {
      h += '<div class="ind-drawer-acts">' +
        '<button class="ind-mini" data-tk="'+escAttr(String(n.source_ticker||''))+'" data-nm="'+escAttr(String(n.name||''))+'" onclick="App._industryGoAnalysis(this.dataset.tk,this.dataset.nm)">🔍 分析</button>' +
        '<button class="ind-mini" data-tk="'+escAttr(String(n.source_ticker||''))+'" onclick="App._industryGoMarket(this.dataset.tk)">📈 行情</button>' +
        "</div>";
    }
    drawer.innerHTML = h;
    drawer.classList.add("open");
  },

  _drawerList: function(title, arr) {
    if (!arr || !arr.length) return "";
    var h = '<div class="ind-drawer-sec"><div class="ind-drawer-st">' + title + "</div>";
    for (var i = 0; i < arr.length; i++) {
      h += '<div class="ind-drawer-node" data-node="'+escAttr(String(arr[i].id))+'" onclick="App._showIndustryNode(this.dataset.node)">' +
        esc(arr[i].name) + (arr[i].source_ticker ? " · " + esc(arr[i].source_ticker) : "") + "</div>";
    }
    return h + "</div>";
  },

  _closeIndustryDrawer: function() {
    var d = document.getElementById("industryDrawer");
    if (d) d.classList.remove("open");
  },

  _industryGoAnalysis: function(ticker, name) {
    this._analysisTicker = ticker; this._analysisName = name;
    this.switchMode("analysis");
  },

  _industryGoMarket: function(ticker) {
    this._currentTicker = ticker;
    this.switchMode("market");
  },

  _industryTabs: function(active) {
    return '<div class="ind-toolbar">' +
      '<button class="ind-tab' + (active === "map" ? " active" : "") + '" onclick="App._backToMap()">🗺 地图</button>' +
      '<button class="ind-tab' + (active === "chain" ? " active" : "") + (this._industryCategory ? "" : " disabled") +
      '" data-c="' + escAttr(String(this._industryCategory||'')) + '" onclick="App._selectIndustryChain(this.dataset.c)">🔗 链路</button>' +
      '<span class="ind-crumb">' + (this._industryCategory ? esc(this._industryCategory) : "全球产业格局") + "</span>" +
      "</div>";
  }

});
