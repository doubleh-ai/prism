/* ═══════════════════════════════════════
   Prism — Step-by-step Guided Tour (v0.12)
   Spotlight overlay + popup on target elements.
   ═══════════════════════════════════════ */
Object.assign(App, {

  _tourSteps: {
    chat: [
      {sel:".icon-rail-btn[data-mode='chat']",title:"对话",text:"在这里与 AI 对话。输入问题后按回车发送。"},
      {sel:"#input",title:"输入框",text:"输入投资问题或分析指令。支持 [[quote:600519]] 股票链接。"},
      {sel:"#modelSelect",title:"模型选择",text:"下拉切换 AI 模型（DeepSeek / MiniMax 等）。"},
      {sel:"#cmpTools",title:"工具管理",text:"点击这里增删分析工具（技能）。"},
    ],
    market: [
      {sel:".icon-rail-btn[data-mode='market']",title:"行情",text:"看实时行情和 K 线图。搜索或自选股票。"},
      {sel:"#marketSidebarQ",title:"搜索",text:"输入股票代码或名称，回车搜索。"},
      {sel:"#indicatorInput",title:"指标库",text:"输入自然语言描述（如 \"MACD金叉\"），翻译成可执行指标并叠加到 K 线。"},
    ],
    analysis: [
      {sel:".icon-rail-btn[data-mode='analysis']",title:"分析",text:"一键生成 7 位 AI 分析师的结构化投资报告。"},
      {sel:"#analysisQ",title:"搜索股票",text:"输入代码或名称，选中后点击「一键分析」。"},
      {sel:".analysis-run-btn",title:"一键分析",text:"开始分析。7 位分析师依次给出研报，最终生成仪表盘。"},
    ],
    industry: [
      {sel:".icon-rail-btn[data-mode='industry']",title:"产业链图谱",text:"看全球产业分布地图，钻取产业链上下游关系。"},
      {sel:".ind-chain-item",title:"产业链选择",text:"点击任一产业名称，钻取到该产业链的上下游关系图。"},
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
    p.seenTour = {}; // reset all; replay the tour
    this._savePrefs();
    this._startTour(this._mode || "chat");
  },

});
