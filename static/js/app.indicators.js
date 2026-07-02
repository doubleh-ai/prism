/* ═══════════════════════════════════════
   Prism — Indicators (v0.11)
   LLM-translate any input → DSL → library;
   apply to K-line (plot overlay / signal marks) + scan watchlist.
   ═══════════════════════════════════════ */
Object.assign(App, {

  _indicators: [],
  _indicatorOverlays: [],   // [{name,color,points:[{t,v}]}] read by renderKlineChart
  _indicatorMarkers: [],    // [{t,label,color}] read by renderKlineChart
  _indicatorPreview: null,

  _loadIndicators: async function() {
    try {
      var r = await this.api("/api/indicators");
      if (r.ok) this._indicators = (await r.json()).indicators || [];
    } catch (e) {}
    this._renderIndicatorList();
  },

  _renderIndicatorList: function() {
    var el = document.getElementById("indicatorList");
    if (!el) return;
    if (!this._indicators.length) { el.innerHTML = '<div class="ind-empty">尚无指标，用上方框翻译一个</div>'; return; }
    var h = "";
    for (var i = 0; i < this._indicators.length; i++) {
      var it = this._indicators[i];
      var isSig = it.kind === "signal";
      h += '<div class="mi-item">' +
        '<span class="mi-tag ' + (isSig ? "sig" : "plot") + '">' + (isSig ? "信号" : "线") + "</span>" +
        '<span class="mi-nm" title="' + escAttr(it.description || "") + '">' + esc(it.name) + "</span>" +
        '<div class="mi-acts">' +
          '<button class="mi-btn" title="叠加到当前K线" onclick="App._applyIndicator(\'' + escAttr(it.id) + "')\">📈</button>" +
          (isSig ? '<button class="mi-btn" title="用此指标选股(自选)" onclick="App._scanIndicator(\'' + escAttr(it.id) + "')\">🔍</button>" : "") +
          '<button class="mi-btn mi-del" title="删除" onclick="App._deleteIndicator(\'' + escAttr(it.id) + "')\">🗑</button>" +
        "</div></div>";
    }
    el.innerHTML = h;
  },

  _translateIndicator: async function() {
    var inp = document.getElementById("indicatorInput");
    if (!inp) return;
    var text = (inp.value || "").trim();
    if (!text) return;
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = '<div class="ind-empty">翻译中…</div>';
    try {
      var r = await this.api("/api/indicators/translate", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({input: text})
      });
      if (!r.ok) {
        var err = await r.json().catch(function() { return {}; });
        if (pv) pv.innerHTML = '<div class="ind-empty">' + esc(err.error || "翻译失败，换种说法") + "</div>";
        return;
      }
      this._indicatorPreview = await r.json();
      this._renderIndicatorPreview();
    } catch (e) {
      if (pv) pv.innerHTML = '<div class="ind-empty">翻译失败</div>';
    }
  },

  _renderIndicatorPreview: function() {
    var pv = document.getElementById("indicatorPreview");
    if (!pv || !this._indicatorPreview) return;
    var p = this._indicatorPreview;
    pv.innerHTML = '<div class="mi-prev">' +
      '<div class="mi-prev-hd"><b>' + esc(p.name) + "</b> " +
      '<span class="mi-tag ' + (p.kind === "signal" ? "sig" : "plot") + '">' +
      (p.kind === "signal" ? "信号" : "画线") + "</span></div>" +
      (p.description ? '<div class="mi-prev-desc">' + esc(p.description) + "</div>" : "") +
      '<div class="mi-prev-dsl">' + esc(JSON.stringify(p.dsl)) + "</div>" +
      '<div class="mi-prev-acts">' +
        '<button class="mi-save" onclick="App._saveIndicatorFromPreview()">💾 存入指标库</button>' +
        '<button class="mi-btn" onclick="App._previewApplyIndicator()">📈 试算到当前股</button>' +
      "</div></div>";
  },

  _saveIndicatorFromPreview: async function() {
    var p = this._indicatorPreview;
    if (!p) return;
    var r = await this.api("/api/indicators", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: p.name, kind: p.kind, dsl: p.dsl, description: p.description})
    });
    if (r.ok) {
      this._indicatorPreview = null;
      var pv = document.getElementById("indicatorPreview");
      if (pv) pv.innerHTML = '<div class="ind-empty">已存入指标库 ✓</div>';
      this._loadIndicators();
    }
  },

  _deleteIndicator: async function(id) {
    await this.api("/api/indicators/" + id, {method: "DELETE"});
    this._loadIndicators();
  },

  _findIndicator: function(id) {
    for (var i = 0; i < this._indicators.length; i++)
      if (this._indicators[i].id === id) return this._indicators[i];
    return null;
  },

  _applyIndicator: async function(id) {
    if (!this._currentTicker) { this._toast("请先在行情里选一只股票"); return; }
    var it = this._findIndicator(id);
    if (!it) return;
    var data = await this._evalIndicator(id, this._currentTicker);
    if (!data) return;
    this._setIndicatorResult(it.name, data);
    this._reRenderKline();
  },

  _previewApplyIndicator: async function() {
    // Save first (eval needs a stored id), then apply.
    var p = this._indicatorPreview;
    if (!p) return;
    if (!this._currentTicker) { this._toast("请先在行情里选一只股票"); return; }
    var r = await this.api("/api/indicators", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: p.name, kind: p.kind, dsl: p.dsl, description: p.description})
    });
    if (!r.ok) return;
    var id = (await r.json()).id;
    this._indicatorPreview = null;
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = "";
    await this._loadIndicators();
    var data = await this._evalIndicator(id, this._currentTicker);
    var it = this._findIndicator(id);
    if (data && it) { this._setIndicatorResult(it.name, data); this._reRenderKline(); }
  },

  _evalIndicator: async function(id, ticker) {
    try {
      var r = await this.api("/api/indicators/" + id + "/eval", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ticker: ticker, period: "1d"})
      });
      if (!r.ok) { var e = await r.json().catch(function() { return {}; }); this._toast(e.error || "计算失败"); return null; }
      return await r.json();
    } catch (e) { return null; }
  },

  _setIndicatorResult: function(name, data) {
    if (data.kind === "signal") {
      var label = "▲";
      this._indicatorMarkers = (data.triggers || []).slice(-30).map(function(t) { return {t: t, label: label}; });
      this._indicatorOverlays = [];
      this._toast(name + "：历史触发 " + (data.triggers || []).length + " 次" + (data.latest ? "（今日触发）" : ""));
    } else {
      this._indicatorOverlays = [{name: name, points: data.points || []}];
      this._indicatorMarkers = [];
    }
  },

  _clearIndicators: function() {
    this._indicatorOverlays = [];
    this._indicatorMarkers = [];
    this._reRenderKline();
  },

  _reRenderKline: function() {
    var div = document.getElementById("marketKlineChart");
    if (div && this._lastKline) this.renderKlineChart(div, this._lastKline);
  },

  _scanIndicator: async function(id) {
    var it = this._findIndicator(id);
    if (!it) return;
    var tickers = (this._watchlist || []).map(function(w) { return w.ticker; });
    if (!tickers.length) { this._toast("自选为空，无法选股"); return; }
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = '<div class="ind-empty">扫描 ' + tickers.length + " 只自选中…</div>";
    try {
      var r = await this.api("/api/indicators/" + id + "/scan", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({scope: "watchlist", tickers: tickers})
      });
      var d = r.ok ? await r.json() : {matches: []};
      this._renderScanResults(it.name, d);
    } catch (e) {}
  },

  _renderScanResults: function(name, d) {
    var pv = document.getElementById("indicatorPreview");
    if (!pv) return;
    var ms = d.matches || [];
    var h = '<div class="mi-prev"><div class="mi-prev-hd"><b>🔍 ' + esc(name) + "</b> · 命中 " +
      ms.length + "/" + d.scanned + "</div><div class=\"mi-scan\">";
    if (!ms.length) h += '<div class="ind-empty">自选中暂无股票满足该指标</div>';
    for (var i = 0; i < ms.length; i++) {
      h += '<div class="mi-scan-item" onclick="App._pickScanTicker(\'' + escAttr(ms[i].ticker) + "')\">" +
        "<b>" + esc(ms[i].ticker) + "</b> · 触发 " + esc(new Date(ms[i].triggered_at * 1000).toISOString().slice(0, 10)) +
        " · ¥" + esc(String(ms[i].price)) + "</div>";
    }
    h += "</div></div>";
    pv.innerHTML = h;
  },

  _pickScanTicker: function(ticker) {
    if (this._selectStock) this._selectStock(ticker);
  },

  _toast: function(msg) {
    var t = document.getElementById("appToast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(this._toastT);
    this._toastT = setTimeout(function() { t.classList.remove("show"); }, 2400);
  }

});
