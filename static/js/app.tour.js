/* ═══════════════════════════════════════
   Prism — Step-by-step Guided Tour (v0.12)
   Spotlight overlay + popup on target elements.
   ═══════════════════════════════════════ */
Object.assign(App, {

  _tourSteps: {
    market: [
      {sel:".icon-rail-btn[data-workbench-view='market']",title:"行情与跨市场看盘",text:"查看自选股实时涨跌、个股日周月 K 线、财务估值与上下游大宗商品价格锚；也可点顶栏左侧按钮（⌘B）收起列表侧栏。"},
      {sel:".icon-rail-btn[data-workbench-view='radar']",title:"全球风向雷达",text:"90 秒轮询财联社、新浪、东财及美联储/SEC 8-K 全球一手源，自动划分 T0/T1/T2 级别并联动跨市场实时价格印证。"},
      {sel:".icon-rail-btn[data-workbench-view='intel']",title:"圈内情报与盘口体检",text:"零门槛粘贴熟人圈策略观点或产业线索，系统自动提炼条件分支、核验实时盘口涨跌并设置保鲜期（支持匿名分享或仅自己可见）。"},
      {sel:".icon-rail-btn[data-workbench-view='screener']",title:"全市场选股与指标实验室",text:"直接扫描沪深京全市场量化信号，或用大白话生成参数化技术指标。"},
      {sel:"#accountMenuBtn",title:"私人助理 · 模型偏好 · 画像模板",text:"打开右上角个人菜单，即可管理「私人仓位底牌与 0-Token 盯盘推送」、自选默认 AI 模型，或一键套用成熟投研画像模板。"},
      {sel:"#copilotLauncher",title:"私人投研 Agent (⌘K)",text:"按 ⌘K 呼出伴随助理：严格按账号物理隔离，自动结合你的私人仓位底牌与圈内鲜活情报对号入座，并附反方证伪视角。"},
    ],
    radar: [
      {sel:"#radarTopbarPill",title:"顶栏实时风向哨兵",text:"无论在哪个模块看盘，一旦出现 T0 顶层重磅或 T1 产业强催化，顶栏胶囊会第一时间变色提醒。"},
      {sel:"#radarSidebarInner",title:"重要性分级与全球一手源筛选",text:"按 T0 顶层重磅、T1 强催化、GLOBAL 全球一手源或命中自选池一键过滤噪音。"},
      {sel:".radar-warroom__top",title:"全球价格锚与早午报",text:"顶部实时展示离岸人民币、沪深300、恒科、纳指及核心大宗商品涨跌，每个交易日 08:45 与 12:45 自动汇总作战早午报。"},
      {sel:"#copilotLauncher",title:"一键交由私人 Agent 推演",text:"在任意事件剧本底部点击「交给随行 Agent 结合画像推演」，即可结合你当前的真实仓位给出应对纪律。"},
    ],
    intel: [
      {sel:".intel-composer-card",title:"零门槛投喂圈内情报",text:"原封不动粘贴群聊策略或调研线索，系统自动拆解空仓/底仓/重仓应对分支并核验当前盘口是否已透支；可自由选「圈内共享（含匿名）」或「仅自己可见」。"},
      {sel:"#intelFilterTabs",title:"保鲜期自动淘汰与行情对账",text:"每条情报均带智能保鲜期，过期自动归档不污染 Agent 判断；随时可点击卡片上的「按最新行情对账」检验历史准度。"},
      {sel:"#copilotLauncher",title:"结合本人底牌对号入座",text:"点击任意情报卡片上的「结合我的仓位底牌推演」，私人 Agent 会按你保密的真实仓位成数直接给出贴身建议。"},
    ],
    reports: [
      {sel:".journal-kpi-grid",title:"跨资产判断胜率统计",text:"统一汇总你在 A 股个股与商品期货中登记的多空观点，按最新真实行情客观统计印证率。"},
      {sel:".journal-bias-banner",title:"画像联动诊断与深度复盘",text:"系统自动识别你的多空偏差倾向，可一键交由随行 Agent 生成阶段性复盘改进建议。"},
    ],
  },

  _tourIdx: 0,
  _tourMode: "",

  _startTour: function(mode) {
    var steps = (this._tourSteps[mode || this._mode] || []).concat(
      [{sel:null, title:"导览完成", text:"点任意位置关闭指引。随时可在右上角账号菜单中点击「功能导览」重看。"}]
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

  /* ── Contextual Guided Tour wired to topbar ? and account menu ── */
  _toggleTour: function() {
    var p = this._loadPrefs();
    p.seenTour = {};
    this._savePrefs();
    var targetMode = this._tourSteps[this._mode] ? this._mode : "market";
    this._startTour(targetMode);
  },

});
