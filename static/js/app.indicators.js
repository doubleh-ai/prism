/* Prism - Indicator workbench */
Object.assign(App, {

  _indicators: [],
  _indicatorOverlays: [],   // [{name,color,points:[{t,v}]}] read by renderKlineChart
  _indicatorMarkers: [],    // [{t,label,reason,strength,color}] read by renderKlineChart
  _indicatorTracks: [],     // [{name,color,events:[...]}] read by renderKlineChart
  _indicatorZones: [],      // [{start,end,name,color}] read by renderKlineChart
  _indicatorPreview: null,
  _indicatorEditId: null,

  _loadIndicators: async function() {
    try {
      var r = await this.api("/api/indicators");
      if (r.ok) this._indicators = (await r.json()).indicators || [];
    } catch (e) {}
    this._renderIndicatorList();
    if (this._syncScannerIndicators && this._mode === "market") this._syncScannerIndicators();
    if (this._renderIndicatorDock) this._renderIndicatorDock();
  },

  _indicatorParamItems: function(params) {
    params = params || {};
    return Array.isArray(params.items) ? params.items : [];
  },

  _indicatorParamValues: function(params) {
    var vals = {};
    var items = this._indicatorParamItems(params);
    for (var i = 0; i < items.length; i++) {
      if (items[i] && items[i].key) vals[items[i].key] = items[i].value;
    }
    return vals;
  },

  _indicatorDate: function(ts) {
    if (!ts) return "-";
    try { return new Date(ts * 1000).toISOString().slice(0, 10); } catch (e) { return String(ts); }
  },

  _bindIndicatorListActions: function(el) {
    var self = this;
    el.onclick = function(ev) {
      var btn = ev.target.closest("[data-mi-act]");
      if (!btn) return;
      ev.preventDefault();
      var id = btn.getAttribute("data-id");
      var act = btn.getAttribute("data-mi-act");
      if (act === "apply") self._applyIndicator(id);
      else if (act === "scan") self._scanIndicator(id);
      else if (act === "edit") self._openIndicatorEditor(id);
      else if (act === "delete") self._deleteIndicator(id);
    };
  },

  _renderIndicatorList: function() {
    var el = document.getElementById("indicatorList");
    if (!el) return;
    this._bindIndicatorListActions(el);
    if (!this._indicators.length) {
      el.innerHTML = '<div class="ind-empty">尚无指标，用上方输入框翻译一个</div>';
      return;
    }
    var h = "";
    for (var i = 0; i < this._indicators.length; i++) {
      var it = this._indicators[i];
      var isSig = it.kind === "signal";
      var params = this._indicatorParamItems(it.params || {});
      var ui = this._indicatorUi(it);
      var plan = it.plan || {};
      var paramChips = "";
      for (var p = 0; p < Math.min(params.length, 4); p++) {
        paramChips += '<span>' + esc(params[p].label || params[p].key) + " " + esc(String(params[p].value)) + esc(params[p].unit || "") + "</span>";
      }
      h += '<article class="indicator-card">' +
        '<div class="indicator-card-head"><div><span class="mi-tag ' + (isSig ? "sig" : "plot") + '">' + (isSig ? "信号" : "数值") + '</span><b>' + esc(it.name) + '</b></div><span class="mi-source">' + esc(it.source || "custom") + "</span></div>" +
        '<div class="indicator-card-family">' + esc(ui.family_label || "自定义逻辑") + "</div>" +
        '<p>' + esc(it.description || "暂无说明") + "</p>" +
        '<div class="indicator-card-params">' + (paramChips || '<span>无可调参数</span>') + "</div>" +
        '<div class="indicator-card-plan"><span>' + esc((plan.period || "1d").toUpperCase()) + '</span><span>回看 ' + esc(String(plan.lookback || 120)) + '</span><span>' + (plan.universe === "market" ? "全市场" : "支持全市场") + "</span></div>" +
        '<div class="indicator-card-actions"><button type="button" data-mi-act="apply" data-id="' + escAttr(it.id) + '">当前股试算</button><button type="button" data-mi-act="scan" data-id="' + escAttr(it.id) + '">全市场选股</button><button type="button" data-mi-act="edit" data-id="' + escAttr(it.id) + '">配置</button><button type="button" class="danger" data-mi-act="delete" data-id="' + escAttr(it.id) + '" title="删除指标">×</button></div>' +
      "</article>";
    }
    el.innerHTML = h;
  },

  _openIndicatorEditor: function(id) {
    var indicator = this._findIndicator(id);
    if (!indicator) return;
    this._indicatorEditId = id;
    this._indicatorPreview = JSON.parse(JSON.stringify(indicator));
    this._renderIndicatorPreview();
  },

  _translateIndicator: async function() {
    var inp = document.getElementById("indicatorInput");
    if (!inp) return;
    var text = (inp.value || "").trim();
    if (!text) return;
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = '<div class="ind-empty">正在翻译为可调指标计划...</div>';
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
      this._indicatorEditId = null;
      this._indicatorPreview = await r.json();
      this._renderIndicatorPreview();
    } catch (e) {
      if (pv) pv.innerHTML = '<div class="ind-empty">翻译失败</div>';
    }
  },

  _indicatorUi: function(indicator) {
    var params = (indicator && indicator.params) || {};
    return params.ui && typeof params.ui === "object" ? params.ui : {};
  },

  _indicatorLogicText: function(indicator) {
    if (!indicator) return "";
    var ui = this._indicatorUi(indicator);
    var template = ui.logic_template || indicator.description || "调整参数后，计算逻辑会同步更新";
    return this._indicatorTemplateText(template, indicator);
  },

  _indicatorTemplateText: function(template, indicator) {
    var values = this._indicatorParamValues(indicator.params || {});
    return String(template).replace(/\{([a-zA-Z0-9_]+)\}/g, function(match, key) {
      return values[key] == null ? match : String(values[key]);
    });
  },

  _indicatorConditionRows: function(indicator) {
    var ui = this._indicatorUi(indicator);
    var conditions = Array.isArray(ui.conditions) ? ui.conditions : [];
    var html = "";
    for (var i = 0; i < conditions.length; i++) {
      html += '<div class="mi-condition-row"><span>' + (i + 1) + '</span><b>' + esc(this._indicatorTemplateText(conditions[i], indicator)) + '</b></div>';
    }
    return html;
  },

  _indicatorConditionHtml: function(indicator) {
    var rows = this._indicatorConditionRows(indicator);
    if (!rows) return "";
    return '<div class="mi-condition-list"><div class="mi-condition-head"><span>组合条件</span><em>全部满足 AND</em></div><div data-indicator-conditions>' + rows + '</div></div>';
  },

  _indicatorParamControl: function(item, attrName, value) {
    item = item || {};
    attrName = attrName === "data-scan-param" ? "data-scan-param" : "data-param-key";
    var key = escAttr(item.key || "");
    var attr = attrName + '="' + key + '"';
    var min = item.min == null ? 0 : item.min;
    var max = item.max == null ? 100 : item.max;
    var step = item.step == null ? 1 : item.step;
    var unit = item.unit || "";
    var control = item.control || "number";
    var quick = Array.isArray(item.quick_values) ? item.quick_values : [];
    var html = "";
    if (control === "period" || control === "ratio") {
      if (quick.length) {
        html += '<div class="mi-param-choices" role="group" aria-label="' + escAttr(item.label || item.key) + '常用值">';
        for (var i = 0; i < quick.length; i++) {
          var active = Number(quick[i]) === Number(value);
          html += '<button type="button" ' + attr + ' data-param-set="' + escAttr(quick[i]) + '" class="' + (active ? "active" : "") + '" aria-pressed="' + (active ? "true" : "false") + '">' + esc(String(quick[i])) + '</button>';
        }
        html += "</div>";
      }
      html += '<label class="mi-param-exact"><span>精确值</span><input type="number" min="' + escAttr(min) + '" max="' + escAttr(max) + '" step="' + escAttr(step) + '" value="' + escAttr(value) + '" ' + attr + ' aria-label="' + escAttr(item.label || item.key) + '"><em>' + esc(unit) + "</em></label>";
      return html;
    }
    if (control === "threshold") {
      html += '<div class="mi-param-range"><input type="range" min="' + escAttr(min) + '" max="' + escAttr(max) + '" step="' + escAttr(step) + '" value="' + escAttr(value) + '" ' + attr + ' aria-label="' + escAttr(item.label || item.key) + '"><div><span>' + esc(String(min)) + '</span><span>触发边界</span><span>' + esc(String(max)) + "</span></div></div>";
      html += '<label class="mi-param-exact compact"><span>阈值</span><input type="number" min="' + escAttr(min) + '" max="' + escAttr(max) + '" step="' + escAttr(step) + '" value="' + escAttr(value) + '" ' + attr + ' aria-label="' + escAttr(item.label || item.key) + '"><em>' + esc(unit) + "</em></label>";
      return html;
    }
    return '<label class="mi-param-exact full"><span>数值</span><input type="number" min="' + escAttr(min) + '" max="' + escAttr(max) + '" step="' + escAttr(step) + '" value="' + escAttr(value) + '" ' + attr + ' aria-label="' + escAttr(item.label || item.key) + '"><em>' + esc(unit) + "</em></label>";
  },

  _indicatorParamBlocks: function(items, attrName) {
    var groups = {};
    var order = [];
    for (var i = 0; i < items.length; i++) {
      var group = items[i].group || "其他参数";
      if (!groups[group]) { groups[group] = []; order.push(group); }
      groups[group].push(items[i]);
    }
    var html = "";
    for (var g = 0; g < order.length; g++) {
      var name = order[g];
      html += '<section class="mi-param-group"><div class="mi-param-group-title">' + esc(name) + "</div>";
      for (var j = 0; j < groups[name].length; j++) {
        var item = groups[name][j];
        var value = item.value == null ? "" : item.value;
        var control = item.control || "number";
        var tone = item.tone || "neutral";
        html += '<div class="mi-param mi-param-' + escAttr(control) + ' tone-' + escAttr(tone) + '">' +
          '<div class="mi-param-top"><div><span>' + esc(item.label || item.key) + '</span>' + (item.help ? '<small>' + esc(item.help) + "</small>" : "") + '</div><b data-param-display="' + escAttr(item.key) + '">' + esc(String(value)) + esc(item.unit || "") + "</b></div>" +
          '<div class="mi-param-body">' + this._indicatorParamControl(item, attrName, value) + "</div></div>";
      }
      html += "</section>";
    }
    return html;
  },

  _indicatorPresetHtml: function(params) {
    var ui = (params && params.ui) || {};
    var presets = Array.isArray(ui.presets) ? ui.presets : [];
    if (!presets.length) return "";
    var html = '<div class="mi-preset-row"><span>快速方案</span><div>';
    for (var i = 0; i < presets.length; i++) {
      html += '<button type="button" data-param-preset="' + i + '" title="' + escAttr(presets[i].note || "") + '">' + esc(presets[i].label || ("方案 " + (i + 1))) + "</button>";
    }
    return html + "</div></div>";
  },

  _renderIndicatorPreview: function() {
    var pv = document.getElementById("indicatorPreview");
    if (!pv || !this._indicatorPreview) return;
    var p = this._indicatorPreview;
    p.params = p.params || {items: []};
    p.plan = p.plan || {};
    p.visual = p.visual || {};
    var items = this._indicatorParamItems(p.params);
    var plan = p.plan;
    var visual = p.visual;
    var kindLabel = p.kind === "signal" ? "信号" : "画线";
    var ui = this._indicatorUi(p);
    var paramHtml = items.length ? this._indicatorParamBlocks(items, "data-param-key") : '<div class="ind-empty">该指标暂无可调参数</div>';
    pv.innerHTML = '<div class="mi-prev">' +
      '<div class="mi-prev-hd"><b>' + esc(p.name) + "</b>" +
        '<span class="mi-tag ' + (p.kind === "signal" ? "sig" : "plot") + '">' + kindLabel + "</span>" +
        '<span class="mi-source">' + esc(p.source || "custom") + "</span></div>" +
      (p.description ? '<div class="mi-prev-desc">' + esc(p.description) + "</div>" : "") +
      '<div class="mi-family-line"><span>' + esc(ui.family_label || "自定义逻辑") + '</span><b data-indicator-logic>' + esc(this._indicatorLogicText(p)) + "</b></div>" +
      this._indicatorConditionHtml(p) +
      this._indicatorPresetHtml(p.params) +
      '<div class="mi-section-title primary">核心指标参数</div>' + paramHtml +
      '<details class="mi-config-section"><summary><span>扫描方案</span><small>股票池、周期、排序与流动性</small></summary><div class="mi-config-body">' +
        '<div class="mi-plan-grid">' +
          this._planSelect("universe", "范围", plan.universe || "watchlist", [["watchlist","自选股"],["market","全市场"]]) +
          this._planSelect("period", "周期", plan.period || "1d", [["1d","日线"],["1w","周线"],["1m","月线"]]) +
          this._planNumber("lookback", "回看K线", plan.lookback || 120, 30, 500, 10) +
          this._planNumber("trigger_window", "信号回溯窗口", plan.trigger_window || 5, 1, 30, 1) +
          this._planSelect("score", "排序", plan.score || (p.kind === "signal" ? "recent_signal" : "latest_value"), [["recent_signal","最近触发"],["strength","信号强度"],["latest_value","最新数值"]]) +
          this._planSelect("direction", "排序方向", plan.direction || "desc", [["desc","从高到低"],["asc","从低到高"]]) +
          this._planTurnover(plan.min_turnover || 0) +
          this._planNumber("max_results", "结果上限", plan.max_results || 100, 10, 300, 10) +
        "</div>" +
        '<div class="mi-plan-checks">' +
          this._planChecks("markets", "默认市场", plan.markets || ["SH","SZ","BJ"], [["SH","沪市"],["SZ","深市"],["BJ","北交所"]]) +
          this._planChecks("boards", "默认板块", plan.boards || ["main","gem","star","bse"], [["main","主板"],["gem","创业板"],["star","科创板"],["bse","北交所"]]) +
        '</div><label class="mi-plan-switch"><input type="checkbox" data-plan-bool="exclude_st"' + (plan.exclude_st === false ? "" : " checked") + '><span>默认排除 ST 与退市整理股票</span></label></div></details>' +
      '<details class="mi-config-section"><summary><span>图表显示</span><small>标记样式与颜色</small></summary><div class="mi-config-body"><div class="mi-visual-grid">' +
          this._visualSelect("style", "样式", visual.style || (p.kind === "signal" ? "signal_track" : "overlay"), [["signal_track","信号轨道"],["overlay","叠加线"],["zone","触发区间"]]) +
          '<label><span>颜色</span><input type="color" value="' + escAttr(visual.color || "#60a5fa") + '" data-visual-key="color"></label>' +
          '<label><span>标记</span><input type="text" maxlength="3" value="' + escAttr(visual.marker || "▲") + '" data-visual-key="marker"></label>' +
        '</div></div></details>' +
      '<details class="mi-prev-dsl"><summary>DSL</summary><pre>' + esc(JSON.stringify(p.dsl, null, 2)) + "</pre></details>" +
      '<div class="mi-prev-acts">' +
        '<button class="mi-save" onclick="App._saveIndicatorFromPreview()">' + (this._indicatorEditId ? "保存修改" : "存入指标库") + '</button>' +
        '<button class="mi-btn mi-btn-text" onclick="App._previewApplyIndicator()">试算当前股</button>' +
        '<button class="mi-btn mi-btn-text" onclick="App._cancelIndicatorEditor()">取消</button>' +
      "</div></div>";
    this._bindIndicatorPreviewInputs(pv);
  },

  _planSelect: function(key, label, value, opts) {
    var h = '<label><span>' + esc(label) + '</span><select data-plan-key="' + escAttr(key) + '">';
    for (var i = 0; i < opts.length; i++) {
      h += '<option value="' + escAttr(opts[i][0]) + '"' + (opts[i][0] === value ? " selected" : "") + '>' + esc(opts[i][1]) + '</option>';
    }
    return h + '</select></label>';
  },

  _visualSelect: function(key, label, value, opts) {
    var h = '<label><span>' + esc(label) + '</span><select data-visual-key="' + escAttr(key) + '">';
    for (var i = 0; i < opts.length; i++) {
      h += '<option value="' + escAttr(opts[i][0]) + '"' + (opts[i][0] === value ? " selected" : "") + '>' + esc(opts[i][1]) + '</option>';
    }
    return h + '</select></label>';
  },

  _planChecks: function(key, label, values, opts) {
    var selected = {};
    for (var i = 0; i < values.length; i++) selected[String(values[i])] = true;
    var html = '<fieldset><legend>' + esc(label) + '</legend>';
    for (var j = 0; j < opts.length; j++) {
      html += '<label><input type="checkbox" data-plan-list="' + escAttr(key) + '" value="' + escAttr(opts[j][0]) + '"' + (selected[opts[j][0]] ? " checked" : "") + '><span>' + esc(opts[j][1]) + "</span></label>";
    }
    return html + "</fieldset>";
  },

  _planNumber: function(key, label, value, min, max, step) {
    return '<label><span>' + esc(label) + '</span><input type="number" min="' + escAttr(min) + '" max="' + escAttr(max) + '" step="' + escAttr(step) + '" value="' + escAttr(value) + '" data-plan-key="' + escAttr(key) + '"></label>';
  },

  _planTurnover: function(value) {
    return '<label><span>最低成交额（万元）</span><input type="number" min="0" max="1000000" step="100" value="' + escAttr(Math.round(Number(value || 0) / 10000)) + '" data-plan-turnover="1"></label>';
  },

  _bindIndicatorPreviewInputs: function(pv) {
    var self = this;
    pv.onclick = function(ev) {
      var quick = ev.target.closest("[data-param-set]");
      if (quick) {
        ev.preventDefault();
        self._updateIndicatorParam(quick.getAttribute("data-param-key"), quick.getAttribute("data-param-set"));
        return;
      }
      var preset = ev.target.closest("[data-param-preset]");
      if (preset) {
        ev.preventDefault();
        self._applyIndicatorPreset(Number(preset.getAttribute("data-param-preset")));
      }
    };
    pv.oninput = pv.onchange = function(ev) {
      var t = ev.target;
      if (!self._indicatorPreview || !t) return;
      var pkey = t.getAttribute("data-param-key");
      var planKey = t.getAttribute("data-plan-key");
      var visualKey = t.getAttribute("data-visual-key");
      var planList = t.getAttribute("data-plan-list");
      var planBool = t.getAttribute("data-plan-bool");
      var planTurnover = t.getAttribute("data-plan-turnover");
      if (pkey) self._updateIndicatorParam(pkey, t.value);
      else if (planKey) self._setIndicatorPlanField(planKey, t.value);
      else if (visualKey) self._setIndicatorVisualField(visualKey, t.value);
      else if (planList) self._setIndicatorPlanList(planList, pv);
      else if (planBool) self._setIndicatorPlanField(planBool, !!t.checked);
      else if (planTurnover) self._setIndicatorPlanField("min_turnover", Math.max(0, Number(t.value || 0)) * 10000);
    };
  },

  _updateIndicatorParam: function(key, value) {
    var p = this._indicatorPreview;
    if (!p) return;
    var num = Number(value);
    var val = isNaN(num) ? value : num;
    var items = this._indicatorParamItems(p.params || {});
    for (var i = 0; i < items.length; i++) {
      if (items[i].key === key) {
        items[i].value = val;
        break;
      }
    }
    var inputs = document.querySelectorAll('input[data-param-key="' + key.replace(/"/g, '\\"') + '"]');
    for (var j = 0; j < inputs.length; j++) if (document.activeElement !== inputs[j]) inputs[j].value = val;
    var quick = document.querySelectorAll('button[data-param-key="' + key.replace(/"/g, '\\"') + '"][data-param-set]');
    for (var k = 0; k < quick.length; k++) {
      var active = Number(quick[k].getAttribute("data-param-set")) === Number(val);
      quick[k].classList.toggle("active", active);
      quick[k].setAttribute("aria-pressed", active ? "true" : "false");
    }
    var display = document.querySelector('[data-param-display="' + key.replace(/"/g, '\\"') + '"]');
    for (var d = 0; d < items.length; d++) if (items[d].key === key && display) display.textContent = String(items[d].value) + (items[d].unit || "");
    var logic = document.querySelector("[data-indicator-logic]");
    if (logic) logic.textContent = this._indicatorLogicText(p);
    var conditions = document.querySelector("[data-indicator-conditions]");
    if (conditions) conditions.innerHTML = this._indicatorConditionRows(p);
    this._syncIndicatorPresetState();
  },

  _applyIndicatorPreset: function(index) {
    var p = this._indicatorPreview;
    var presets = p && p.params && p.params.ui && Array.isArray(p.params.ui.presets) ? p.params.ui.presets : [];
    var preset = presets[index];
    if (!preset || !preset.values) return;
    for (var key in preset.values) if (Object.prototype.hasOwnProperty.call(preset.values, key)) this._updateIndicatorParam(key, preset.values[key]);
    this._syncIndicatorPresetState();
  },

  _syncIndicatorPresetState: function() {
    var p = this._indicatorPreview;
    if (!p) return;
    var presets = p.params && p.params.ui && Array.isArray(p.params.ui.presets) ? p.params.ui.presets : [];
    var values = this._indicatorParamValues(p.params || {});
    var buttons = document.querySelectorAll("[data-param-preset]");
    for (var i = 0; i < buttons.length; i++) {
      var preset = presets[i] || {};
      var match = !!preset.values;
      for (var key in (preset.values || {})) {
        if (Number(values[key]) !== Number(preset.values[key])) { match = false; break; }
      }
      buttons[i].classList.toggle("active", match);
      buttons[i].setAttribute("aria-pressed", match ? "true" : "false");
    }
  },

  _setIndicatorPlanField: function(key, value) {
    if (!this._indicatorPreview) return;
    this._indicatorPreview.plan = this._indicatorPreview.plan || {};
    if (typeof value === "boolean") {
      this._indicatorPreview.plan[key] = value;
      return;
    }
    var n = Number(value);
    this._indicatorPreview.plan[key] = isNaN(n) || value === "" ? value : n;
  },

  _setIndicatorPlanList: function(key, root) {
    if (!this._indicatorPreview) return;
    var values = [];
    var boxes = root.querySelectorAll('[data-plan-list="' + key.replace(/"/g, '\\"') + '"]');
    for (var i = 0; i < boxes.length; i++) if (boxes[i].checked) values.push(boxes[i].value);
    this._indicatorPreview.plan = this._indicatorPreview.plan || {};
    this._indicatorPreview.plan[key] = values;
  },

  _setIndicatorVisualField: function(key, value) {
    if (!this._indicatorPreview) return;
    this._indicatorPreview.visual = this._indicatorPreview.visual || {};
    this._indicatorPreview.visual[key] = value;
    if (key === "style") {
      this._indicatorPreview.visual.track = value === "signal_track" || value === "zone";
      this._indicatorPreview.visual.zone = value === "zone";
    }
  },

  _indicatorPayloadFromPreview: function() {
    var p = this._indicatorPreview;
    if (!p) return null;
    return {
      name: p.name,
      kind: p.kind,
      dsl: p.dsl,
      description: p.description || "",
      params: p.params || {},
      plan: p.plan || {},
      visual: p.visual || {},
      source: p.source || "custom"
    };
  },

  _saveIndicatorFromPreview: async function() {
    var payload = this._indicatorPayloadFromPreview();
    if (!payload) return;
    var editing = this._indicatorEditId;
    var r = await this.api(editing ? ("/api/indicators/" + encodeURIComponent(editing)) : "/api/indicators", {
      method: editing ? "PUT" : "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (r.ok) {
      this._indicatorPreview = null;
      this._indicatorEditId = null;
      var pv = document.getElementById("indicatorPreview");
      if (pv) pv.innerHTML = '<div class="indicator-editor-empty">指标已保存，选择另一项继续配置。</div>';
      this._loadIndicators();
    }
  },

  _cancelIndicatorEditor: function() {
    this._indicatorPreview = null;
    this._indicatorEditId = null;
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = '<div class="indicator-editor-empty">从左侧选择指标进行配置，或在上方输入描述生成新指标。</div>';
  },

  _deleteIndicator: async function(id) {
    await this.api("/api/indicators/" + id, {method: "DELETE"});
    if (this._indicatorEditId === id) this._cancelIndicatorEditor();
    this._loadIndicators();
  },

  _findIndicator: function(id) {
    for (var i = 0; i < this._indicators.length; i++)
      if (this._indicators[i].id === id) return this._indicators[i];
    return null;
  },

  _applyIndicator: async function(id) {
    if (!this._currentTicker) { this._toast("请先在行情里选择一只股票"); return; }
    var it = this._findIndicator(id);
    if (!it) return;
    var data = await this._evalIndicator(id, this._currentTicker, this._indicatorParamValues(it.params || {}));
    if (!data) return;
    this._setIndicatorResult(it.name, data, it);
    this._reRenderKline();
  },

  _previewApplyIndicator: async function() {
    var payload = this._indicatorPayloadFromPreview();
    if (!payload) return;
    if (!this._currentTicker) { this._toast("请先在行情里选择一只股票"); return; }
    var editing = this._indicatorEditId;
    var r = await this.api(editing ? ("/api/indicators/" + encodeURIComponent(editing)) : "/api/indicators", {
      method: editing ? "PUT" : "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    if (!r.ok) return;
    var response = await r.json();
    var id = editing || response.id;
    this._indicatorPreview = null;
    this._indicatorEditId = null;
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = "";
    await this._loadIndicators();
    var it = this._findIndicator(id);
    var data = await this._evalIndicator(id, this._currentTicker, this._indicatorParamValues(payload.params || {}));
    if (data && it) { this._setIndicatorResult(it.name, data, it); this._reRenderKline(); }
  },

  _evalIndicator: async function(id, ticker, params) {
    try {
      var period = this._currentPeriod || "1d";
      var payload = {ticker: ticker, period: period, params: params || {}};
      if (ticker === this._currentTicker && Array.isArray(this._lastKline) && this._lastKline.length) {
        payload.kline = this._lastKline.slice(-200).map(function(k) {
          return {
            timestamp: k.timestamp || k.ts || k.t,
            open: k.open,
            high: k.high,
            low: k.low,
            close: k.close,
            volume: k.volume,
            amount: k.amount || k.turnover
          };
        });
      }
      var r = await this.api("/api/indicators/" + id + "/eval", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
      });
      if (!r.ok) { var e = await r.json().catch(function() { return {}; }); this._toast(e.error || "计算失败"); return null; }
      return await r.json();
    } catch (e) { return null; }
  },

  _setIndicatorResult: function(name, data, indicator) {
    var visual = data.visual || (indicator && indicator.visual) || {};
    if (data.kind === "signal") {
      var events = data.events || (data.triggers || []).slice(-30).map(function(t) {
        return {t: t, label: visual.marker || "▲", name: name, color: visual.color || "#22c55e"};
      });
      this._indicatorMarkers = events;
      this._indicatorTracks = data.tracks || [{name: name, color: visual.color || "#22c55e", events: events}];
      this._indicatorZones = data.zones || [];
      this._indicatorOverlays = [];
      this._toast(name + "：历史触发 " + events.length + " 次" + (data.latest ? "，最新K线触发" : ""));
    } else {
      this._indicatorOverlays = [{name: name, points: data.points || [], color: visual.color || "#60a5fa"}];
      this._indicatorMarkers = [];
      this._indicatorTracks = [];
      this._indicatorZones = [];
      if (data.latest_value != null) this._toast(name + "：最新值 " + Number(data.latest_value).toFixed(2));
    }
  },

  _clearIndicators: function() {
    this._indicatorOverlays = [];
    this._indicatorMarkers = [];
    this._indicatorTracks = [];
    this._indicatorZones = [];
    this._reRenderKline();
  },

  _reRenderKline: function() {
    var div = document.getElementById("marketKlineChart");
    if (div && this._lastKline) this.renderKlineChart(div, this._lastKline);
  },

  _scanIndicator: async function(id) {
    var it = this._findIndicator(id);
    if (!it) return;
    if (this._prepareIndicatorScan) {
      this._prepareIndicatorScan(id);
      return;
    }
    var tickers = (this._watchlist || []).map(function(w) { return w.ticker; });
    if (!tickers.length) { this._toast("自选为空，无法选股"); return; }
    var pv = document.getElementById("indicatorPreview");
    if (pv) pv.innerHTML = '<div class="ind-empty">扫描 ' + tickers.length + " 只自选中...</div>";
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
    var h = '<div class="mi-prev"><div class="mi-prev-hd"><b>⌕ ' + esc(name) + "</b> · 命中 " +
      ms.length + "/" + (d.scanned || 0) + "</div><div class=\"mi-scan\">";
    if (!ms.length) h += '<div class="ind-empty">自选中暂无股票满足该指标</div>';
    for (var i = 0; i < ms.length; i++) {
      h += '<div class="mi-scan-item" onclick="App._pickScanTicker(\'' + escAttr(ms[i].ticker) + "')\">" +
        '<div><b>' + esc(ms[i].ticker) + "</b><span>" + esc(this._indicatorDate(ms[i].triggered_at)) + " · " + esc(String(ms[i].price)) + "</span></div>" +
        '<em style="width:' + Math.max(5, Math.min(100, ms[i].strength || 0)) + '%"></em>' +
        '<p>' + esc(ms[i].reason || "") + "</p></div>";
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
