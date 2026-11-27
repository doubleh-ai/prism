/* ═══════════════════════════════════════
   Prism — Step-by-step Guided Tour (v0.12)
   Spotlight overlay + popup on target elements.
   ═══════════════════════════════════════ */
Object.assign(App, {

  _tourSteps: {
    market: [
      {sel:".icon-rail-btn[data-workbench-view='market']",title:"行情工作区",text:"全站核心看盘工作台：查看自选股实时涨跌、个股日周月 K 线、财务估值与上下游大宗商品锚。"},
      {sel:"#marketSidebarQ",title:"快速检索与自选池",text:"输入股票代码或简称秒搜，一键加入个人自选池；也可点顶栏左侧按钮（⌘B）随时收起列表侧栏。"},
      {sel:"a.icon-rail-btn[href='/futures']",title:"期货研究",text:"直达 46 品种期货本体工作台：查看近 5 年分位温度计、产业链盘面利润与期股联动。"},
      {sel:".icon-rail-btn[data-workbench-view='screener']",title:"全市场选股与指标实验室",text:"直接从左栏切换至沪深京全市场量化扫描或自然语言参数化指标实验室。"},
      {sel:"#topbarPersonaBtn",title:"独立投研画像",text:"配置你的主导投资流派（价值/周期/趋势/套利）、仓位回撤红线与个体化研究准则。"},
      {sel:"#copilotLauncher",title:"自适应随行 Agent (⌘K)",text:"按 ⌘K 呼出伴随助手：结论先行，长文与表格自动拓宽，并支持拖拽边缘与一键 ⤢ 居中沉浸精读。"},
    ],
    reports: [
      {sel:"#researchReportsTab",title:"精选研报归档",text:"收纳所有经过工具核验的深度研究报告与原文引用。"},
      {sel:"#researchJournalTab",title:"全域判断与复盘账本",text:"跨股票与期货统一管理你的多空观点，按真实收盘行情自动核对印证与证伪，并反哺个人画像。"},
    ],
  },

  _tourIdx: 0,
  _tourMode: "",

  _startTour: function(mode) {
    var steps = (this._tourSteps[mode || this._mode] || []).concat(
      [{sel:null, title:"就这样！", text:"点任意位置关闭指引。随时点顶栏 ❔ 重看。"}]
    );
    if (steps.length <= 1) return;
    this._tourIdx = 0;
    this._tourMode = mode || this._mode;
    this._tourStepsActive = steps;
    // mark as seen
    var p = this._loadPrefs(); p.seenTour = p.seenTour || {};
    p.seenTour[this._tourMode] = true;
    this._savePrefs();
    this._renderTourStep();
  },

  _renderTourStep: function() {
    var step = this._tourStepsActive[this._tourIdx];
    if (!step) return this._closeTour();

    // Remove previous highlight
    this._closeTour(false);
    var target = step.sel ? document.querySelector(step.sel) : null;
    var overlay = document.createElement("div");
    overlay.id = "tourOverlay";
    overlay.className = "tour-overlay";
    overlay.onclick = function() { App._nextTourStep(); };

    var tip = document.createElement("div");
    tip.id = "tourTip"; tip.className = "tour-tip";
    tip.innerHTML = '<div class="tour-tip-hd"><b>' + esc(step.title) +
      '</b><span class="tour-tip-idx">' + (this._tourIdx + 1) + '/' +
      (this._tourStepsActive.length) + '</span></div>' +
      '<div class="tour-tip-body">' + esc(step.text) + '</div>' +
      '<div class="tour-tip-acts">' +
      (this._tourIdx > 0 ? '<button class="tour-btn" onclick="App._prevTourStep()">← 上一步</button>' : '') +
      '<button class="tour-btn tour-btn-next" onclick="App._nextTourStep()">' +
      (this._tourIdx < this._tourStepsActive.length - 1 ? '下一步 →' : '知道了') + '</button>' +
      '</div>';

    document.body.appendChild(tip);
    if (target) {
      // Spotlight: huge box-shadow outside the target
      target.classList.add("tour-spot");
    }
    overlay.style.display = "block";
    document.body.appendChild(overlay);

    // display:none has a zero-sized rect. Lay out invisibly before measuring,
    // then use the visible viewport (including zoom / an on-screen keyboard).
    tip.style.visibility = "hidden";
    tip.style.display = "flex";
    this._tourTarget = target;
    this._positionTourTip();
    tip.style.visibility = "visible";

    var self = this;
    this._tourReposition = function() {
      if (self._tourFrame) return;
      self._tourFrame = window.requestAnimationFrame(function() {
        self._tourFrame = null;
        self._positionTourTip();
      });
    };
    window.addEventListener("resize", this._tourReposition);
    // Capture nested workbench scrolling too, not just document scrolling.
    window.addEventListener("scroll", this._tourReposition, true);
    this._tourViewport = window.visualViewport;
    if (this._tourViewport) {
      this._tourViewport.addEventListener("resize", this._tourReposition);
      this._tourViewport.addEventListener("scroll", this._tourReposition);
    }
    if (window.ResizeObserver) {
      this._tourObserver = new window.ResizeObserver(this._tourReposition);
      this._tourObserver.observe(tip);
      if (target) this._tourObserver.observe(target);
    }
  },

  _positionTourTip: function() {
    var tip = document.getElementById("tourTip");
    if (!tip) return;
    var viewport = window.visualViewport;
    var x = viewport ? viewport.offsetLeft : 0;
    var y = viewport ? viewport.offsetTop : 0;
    var width = viewport ? viewport.width : document.documentElement.clientWidth;
    var height = viewport ? viewport.height : window.innerHeight;
    var margin = 12, gap = 10;
    tip.style.width = Math.max(0, Math.min(340, width - margin * 2)) + "px";
    tip.style.maxHeight = Math.max(0, height - margin * 2) + "px";
    var size = tip.getBoundingClientRect();
    var left = x + (width - size.width) / 2;
    var top = y + (height - size.height) / 2;
    var target = this._tourTarget;
    if (target && target.isConnected) {
      var rect = target.getBoundingClientRect();
      left = rect.left;
      top = rect.bottom + gap;
      if (top + size.height > y + height - margin) {
        top = rect.top - size.height - gap;
      }
    }
    tip.style.left = Math.max(x + margin, Math.min(left, x + width - size.width - margin)) + "px";
    tip.style.top = Math.max(y + margin, Math.min(top, y + height - size.height - margin)) + "px";
  },

  _nextTourStep: function() {
    if (this._tourIdx >= (this._tourStepsActive || []).length - 1) { this._closeTour(); return; }
    this._tourIdx++;
    this._renderTourStep();
  },

  _prevTourStep: function() {
    if (this._tourIdx <= 0) return;
    this._tourIdx--;
    this._renderTourStep();
  },

  _closeTour: function(persist) {
    if (this._tourReposition) {
      window.removeEventListener("resize", this._tourReposition);
      window.removeEventListener("scroll", this._tourReposition, true);
      if (this._tourViewport) {
        this._tourViewport.removeEventListener("resize", this._tourReposition);
        this._tourViewport.removeEventListener("scroll", this._tourReposition);
      }
    }
    if (this._tourFrame) window.cancelAnimationFrame(this._tourFrame);
    if (this._tourObserver) this._tourObserver.disconnect();
    this._tourReposition = this._tourViewport = this._tourObserver = this._tourTarget = this._tourFrame = null;
    var ov = document.getElementById("tourOverlay"); if (ov) ov.remove();
    var tp = document.getElementById("tourTip"); if (tp) tp.remove();
    var spots = document.querySelectorAll(".tour-spot");
    for (var i=0;i<spots.length;i++) spots[i].classList.remove("tour-spot");
  },

  /* ── Replaces old _toggleGuide; wired to the ❔ button ── */
  _toggleTour: function() {
    var p = this._loadPrefs();
    p.seenTour = {};
    this._savePrefs();
    this._startTour(this._mode === "reports" ? "reports" : "market");
  },

});
