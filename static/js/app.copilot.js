/* Prism V3.1 — Adaptive Omni-Agent Copilot, Resizable Drawer, Zen Reader & Persona Engine */
(function () {
  'use strict';

  var DEFAULT_COPILOT_W = 480;
  var AUTO_WIDE_COPILOT_W = 700;
  var MIN_COPILOT_W = 380;
  var MAX_COPILOT_W = 920;

  var ARCHETYPE_LABELS = {
    value: '价值基本面派',
    cycle: '产业周期派',
    trend: '趋势动量派',
    defensive: '稳健套利派',
    custom: '自定义风格'
  };

  var PRESET_NEGATIVE_RULES = [
    '排除 ST 与退市整理风险标的',
    '回避近5年 >85% 分位的单边追高',
    '日成交额低于 2 亿元不参与',
    '回避缺少现金流兑现的纯概念题材',
    '触及预设止损失效位无条件离场，不逆势补仓'
  ];

  var PERSONA_TEMPLATES = [
    {
      id: 'steady_core',
      name: '稳健底仓 · 龙头白马',
      fit: '适合日常稳健配置（新手推荐）',
      archetype: 'value',
      style: 'conservative',
      horizon: 'long',
      max_position_pct: 20,
      max_drawdown_pct: 10,
      allow_leverage: false,
      negative_rules: [
        '排除 ST 与退市整理风险标的',
        '回避近5年 >85% 分位的单边追高',
        '回避缺少现金流兑现的纯概念题材'
      ],
      note: '结论先行，优先核查扣非净利与经营现金流匹配度；估值高于历史 70% 分位时主动提示回调风险。',
      traits: [
        '财报分析必查经营现金流净额是否持续覆盖净利润',
        '政策预期快速拉升时不重仓追涨，优先等缩量回调机会'
      ]
    },
    {
      id: 'growth_trend',
      name: '主线景气 · 右侧趋势',
      fit: '适合跟踪 AI 算力 / 科技成长主线波段',
      archetype: 'trend',
      style: 'balanced',
      horizon: 'medium',
      max_position_pct: 25,
      max_drawdown_pct: 12,
      allow_leverage: false,
      negative_rules: [
        '排除 ST 与退市整理风险标的',
        '日成交额低于 2 亿元不参与',
        '触及预设止损失效位无条件离场，不逆势补仓'
      ],
      note: '聚焦订单排产与行业景气边际加速，每次推演必须给出明确的均线支撑位与破位止损线。',
      traits: [
        '优先选择有真实订单与业绩兑现的主线龙头或核心 ETF',
        '高位放量滞涨或有效跌破 20 日均线时果断减仓保护利润'
      ]
    },
    {
      id: 'cycle_cross',
      name: '产业供需 · 股期联动',
      fit: '适合大宗商品周期与上下游顺价研究',
      archetype: 'cycle',
      style: 'balanced',
      horizon: 'medium',
      max_position_pct: 25,
      max_drawdown_pct: 15,
      allow_leverage: true,
      negative_rules: [
        '排除 ST 与退市整理风险标的',
        '回避近5年 >85% 分位的单边追高',
        '触及预设止损失效位无条件离场，不逆势补仓'
      ],
      note: '必查近 5 年价格分位、期现基差与交易所仓单库存，严格区分供给收缩与终端需求真实回暖。',
      traits: [
        '看多周期品前先核对下游利润与开工率能否承接顺价',
        '商品价格处于近 5 年 85% 以上极高分位时重点防范利好兑现'
      ]
    },
    {
      id: 'catalyst_agile',
      name: '催化快反 · 小仓试错',
      fit: '适合结合风向与圈内情报捕捉短线预期差',
      archetype: 'trend',
      style: 'aggressive',
      horizon: 'short',
      max_position_pct: 15,
      max_drawdown_pct: 8,
      allow_leverage: false,
      negative_rules: [
        '排除 ST 与退市整理风险标的',
        '回避近5年 >85% 分位的单边追高',
        '日成交额低于 2 亿元不参与',
        '触及预设止损失效位无条件离场，不逆势补仓'
      ],
      note: '直奔核心催化增量与市场预期差；若盘口已提前抢跑大涨，必须第一时间警示冲高分化风险。',
      traits: [
        '圈内传闻必须交叉比对当日成交量能与前排龙头承接力度',
        '短线试错单笔亏损触及止损线立即离场，绝不拖成被动长线'
      ]
    },
    {
      id: 'defensive_yield',
      name: '低波红利 · 绝对收益',
      fit: '适合大资金防守底仓与期现套保对冲',
      archetype: 'defensive',
      style: 'conservative',
      horizon: 'long',
      max_position_pct: 20,
      max_drawdown_pct: 6,
      allow_leverage: true,
      negative_rules: [
        '排除 ST 与退市整理风险标的',
        '回避近5年 >85% 分位的单边追高',
        '日成交额低于 2 亿元不参与',
        '回避缺少现金流兑现的纯概念题材'
      ],
      note: '以本金安全与赔率确定性为第一优先级，重点评估股息率安全垫、自由现金流与极端情景压力测试。',
      traits: [
        '仅将连续三年分红稳定且自由现金流充沛的标的纳入底仓',
        '市场一致预期过热时主动分批降仓锁定确定性收益'
      ]
    }
  ];

  var PRESET_PERSONA_TRAITS = [
    '财报分析必查经营现金流净额是否持续覆盖净利润',
    '政策预期快速拉升时不重仓追涨，等量能企稳与回调机会',
    '优先选择有业绩兑现的主线龙头或 ETF，回避纯概念后排跟风',
    '圈内传闻必须与客观盘口涨跌、成交量及官方公告交叉核验',
    '单笔亏损触及止损红线无条件认错离场，绝不逆势向下摊平',
    '商品或个股处于近 5 年 85% 以上分位时主动提示冲高分化风险'
  ];

  function setCssVar(name, val) {
    var root = document.documentElement;
    if (root && root.style && typeof root.style.setProperty === 'function') {
      root.style.setProperty(name, val);
    }
  }

  var origNewConv = App.newConv;

  Object.assign(App, {
    _copilotState: 'closed',
    _copilotSplit: true,
    _copilotUserWidth: 0,
    _copilotAutoWidened: false,
    _copilotResponseMode: 'compact',
    _personaData: null,
    _selectedArchetype: 'cycle',
    _selectedNegativeRules: [],
    _selectedTemplateId: '',
    _pendingTemplateTraits: [],

    _initCopilot: function () {
      var self = this;
      try {
        var savedW = parseInt(localStorage.getItem('prism-copilot-width') || '', 10);
        if (savedW >= MIN_COPILOT_W && savedW <= MAX_COPILOT_W) {
          this._copilotUserWidth = savedW;
          setCssVar('--copilot-w', savedW + 'px');
        } else {
          setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
        }
      } catch (e) {
        setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
      }

      document.addEventListener('keydown', function (ev) {
        if ((ev.metaKey || ev.ctrlKey) && String(ev.key || '').toLowerCase() === 'k') {
          ev.preventDefault();
          self.toggleCopilot();
        }
      });

      document.addEventListener('click', function (ev) {
        var pop = document.getElementById('copilotHistoryPopover');
        if (pop && !pop.hidden && !ev.target.closest('#copilotHistoryPopover') && !ev.target.closest('#copilotHistoryBtn')) {
          pop.hidden = true;
        }
      });

      ['personaDialog', 'equityJudgmentDialog', 'copilotReaderDialog'].forEach(function (dlgId) {
        var dlg = document.getElementById(dlgId);
        if (dlg && typeof dlg.addEventListener === 'function') {
          dlg.addEventListener('click', function (ev) {
            if (ev.target === dlg && dlg.open) dlg.close();
          });
        }
      });

      this._bindCopilotResizer();
      try {
        var savedMode = localStorage.getItem('prism-copilot-mode');
        this._copilotResponseMode = savedMode === 'deep' ? 'deep' : 'compact';
      } catch (e) {
        this._copilotResponseMode = 'compact';
      }
      this._syncCopilotSpeedToggle();
      this.loadPersona();
      this._refreshCopilotContextBar();
      try {
        var saved = sessionStorage.getItem('prism_copilot_state');
        if (saved === 'docked' || saved === 'expanded') {
          this.openCopilot(saved, true);
        }
      } catch (e) {}
    },

    _bindCopilotResizer: function () {
      var handle = document.getElementById('copilotResizer');
      if (!handle || typeof handle.addEventListener !== 'function') return;
      var self = this;
      var dragging = false;

      handle.addEventListener('dblclick', function (ev) {
        ev.preventDefault();
        self._copilotUserWidth = 0;
        self._copilotAutoWidened = false;
        try { localStorage.removeItem('prism-copilot-width'); } catch (e) {}
        setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
        if (self._copilotState === 'expanded') self.openCopilot('docked', true);
        var appShell = document.getElementById('appShell');
        if (appShell) appShell.classList.remove('copilot-auto-wide');
        setTimeout(function () {
          if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} }
        }, 60);
      });

      handle.addEventListener('pointerdown', function (ev) {
        if (ev.button !== 0) return;
        ev.preventDefault();
        dragging = true;
        if (handle.setPointerCapture) {
          try { handle.setPointerCapture(ev.pointerId); } catch (e) {}
        }
        if (document.body && document.body.classList) {
          document.body.classList.add('copilot-resizing');
        }
        if (self._copilotState === 'expanded') {
          self._copilotState = 'docked';
          var shell = document.getElementById('copilotShell');
          var appShell = document.getElementById('appShell');
          if (shell) shell.setAttribute('data-copilot-state', 'docked');
          if (appShell) appShell.classList.remove('copilot-wide');
          try { sessionStorage.setItem('prism_copilot_state', 'docked'); } catch (e) {}
        }
      });

      handle.addEventListener('pointermove', function (ev) {
        if (!dragging) return;
        var vw = window.innerWidth || 1440;
        var maxAllowed = Math.min(MAX_COPILOT_W, Math.max(MIN_COPILOT_W, vw - 260));
        var nextW = Math.round(Math.max(MIN_COPILOT_W, Math.min(maxAllowed, vw - ev.clientX)));
        self._copilotUserWidth = nextW;
        self._copilotAutoWidened = false;
        setCssVar('--copilot-w', nextW + 'px');
      });

      function stopDrag(ev) {
        if (!dragging) return;
        dragging = false;
        if (handle.releasePointerCapture && ev && ev.pointerId != null) {
          try { handle.releasePointerCapture(ev.pointerId); } catch (e) {}
        }
        if (document.body && document.body.classList) {
          document.body.classList.remove('copilot-resizing');
        }
        if (self._copilotUserWidth >= MIN_COPILOT_W) {
          try { localStorage.setItem('prism-copilot-width', String(self._copilotUserWidth)); } catch (e) {}
        }
        if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} }
      }

      handle.addEventListener('pointerup', stopDrag);
      handle.addEventListener('pointercancel', stopDrag);
    },

    _autoAdaptCopilotWidth: function () {
      if (this._copilotState !== 'docked' || this._copilotUserWidth > 0) return;
      var el = document.getElementById('msgList');
      if (!el || typeof el.querySelector !== 'function') return;
      var hasRichBlock = !!(el.querySelector('table') || el.querySelector('.chat-inline-report') || el.querySelector('pre'));
      var longText = false;
      var msgs = this.msgs || [];
      for (var i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i] && msgs[i].role === 'assistant') {
          var raw = String(msgs[i].content || '');
          if (raw.length > 560) longText = true;
          break;
        }
      }
      var shouldWiden = hasRichBlock || longText;
      if (shouldWiden && !this._copilotAutoWidened) {
        this._copilotAutoWidened = true;
        var vw = window.innerWidth || 1440;
        var targetW = Math.min(AUTO_WIDE_COPILOT_W, Math.max(DEFAULT_COPILOT_W, vw - 300));
        setCssVar('--copilot-w', targetW + 'px');
        var appShell = document.getElementById('appShell');
        if (appShell) appShell.classList.add('copilot-auto-wide');
        var self = this;
        setTimeout(function () {
          if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} }
        }, 220);
      }
    },

    openCopilot: function (nextState, silentFocus) {
      var target = nextState === 'expanded' ? 'expanded' : 'docked';
      this._copilotState = target;
      var shell = document.getElementById('copilotShell');
      var launcher = document.getElementById('copilotLauncher');
      var appShell = document.getElementById('appShell');
      if (shell) shell.setAttribute('data-copilot-state', target);
      if (launcher) launcher.hidden = true;
      if (appShell) {
        appShell.classList.toggle('copilot-split', !!this._copilotSplit);
        appShell.classList.toggle('copilot-wide', target === 'expanded');
      }
      try { sessionStorage.setItem('prism_copilot_state', target); } catch (e) {}
      this._refreshCopilotContextBar();
      if (this._autoAdaptCopilotWidth) this._autoAdaptCopilotWidth();
      if (this._trackUiEvent) this._trackUiEvent('copilot', 'open');
      var self = this;
      setTimeout(function () {
        if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} }
        if (!silentFocus) {
          var inp = document.getElementById('input');
          if (inp) inp.focus();
        }
      }, 220);
    },

    closeCopilot: function () {
      this._copilotState = 'closed';
      var shell = document.getElementById('copilotShell');
      var launcher = document.getElementById('copilotLauncher');
      var appShell = document.getElementById('appShell');
      var pop = document.getElementById('copilotHistoryPopover');
      if (pop) pop.hidden = true;
      if (shell) shell.setAttribute('data-copilot-state', 'closed');
      if (launcher) launcher.hidden = false;
      if (appShell) {
        appShell.classList.remove('copilot-split', 'copilot-wide', 'copilot-auto-wide');
      }
      try { sessionStorage.setItem('prism_copilot_state', 'closed'); } catch (e) {}
      var self = this;
      setTimeout(function () {
        if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} }
      }, 220);
    },

    toggleCopilot: function () {
      if (this._copilotState === 'closed') this.openCopilot('docked');
      else this.closeCopilot();
    },

    toggleCopilotWide: function () {
      if (this._copilotState === 'closed') {
        this.openCopilot('expanded');
        return;
      }
      if (this._copilotState === 'expanded') {
        this._copilotAutoWidened = false;
        if (!this._copilotUserWidth) setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
        this.openCopilot('docked', true);
      } else {
        this.openCopilot('expanded', true);
      }
    },

    toggleCopilotHistory: function () {
      var pop = document.getElementById('copilotHistoryPopover');
      if (!pop) return;
      pop.hidden = !pop.hidden;
      if (!pop.hidden) this.loadConvs();
    },

    /* ═══ Immersive Centered Zen Reader (⤢ 精读) ═══ */
    openReaderForCard: function (triggerEl) {
      var card = triggerEl && triggerEl.closest ? triggerEl.closest('.msg-card') : null;
      var bubble = card && card.querySelector ? card.querySelector('.bubble') : null;
      if (!bubble) return;
      this._openReaderWithHtml(bubble.innerHTML, this._findUserPromptBeforeCard(card));
    },

    openLatestInReader: function () {
      var el = document.getElementById('msgList');
      if (!el || !el.querySelectorAll) return;
      var cards = el.querySelectorAll('.msg.assistant .msg-card');
      if (!cards.length) {
        if (this._toast) this._toast('当前会话暂无可精读的分析回复');
        return;
      }
      var lastCard = cards[cards.length - 1];
      var bubble = lastCard.querySelector('.bubble');
      if (!bubble) return;
      this._openReaderWithHtml(bubble.innerHTML, this._findUserPromptBeforeCard(lastCard));
    },

    _findUserPromptBeforeCard: function (card) {
      var msgRow = card && card.closest ? card.closest('.msg') : null;
      var prev = msgRow ? msgRow.previousElementSibling : null;
      while (prev) {
        if (prev.classList && prev.classList.contains('user')) {
          var b = prev.querySelector('.bubble');
          return b ? b.textContent.trim() : '';
        }
        prev = prev.previousElementSibling;
      }
      return '';
    },

    _openReaderWithHtml: function (html, promptTitle) {
      var dlg = document.getElementById('copilotReaderDialog');
      var body = document.getElementById('copilotReaderBody');
      var titleEl = document.getElementById('readerDialogTitle');
      if (!dlg || !body) return;
      if (titleEl) {
        titleEl.textContent = promptTitle
          ? (promptTitle.length > 42 ? promptTitle.slice(0, 42) + '…' : promptTitle)
          : '投研深度精读视图';
      }
      body.innerHTML = '<article class="reader-prose bubble">' + html + '</article>';
      if (!dlg.open) dlg.showModal();
      body.scrollTop = 0;
    },

    closeCopilotReader: function () {
      var dlg = document.getElementById('copilotReaderDialog');
      if (dlg && dlg.open) dlg.close();
    },

    copyReaderContent: function () {
      var body = document.getElementById('copilotReaderBody');
      if (!body || !navigator.clipboard) return;
      navigator.clipboard.writeText(body.innerText || '').then(function () {
        if (App._toast) App._toast('已复制全文到剪贴板');
      }).catch(function () {});
    },

    _currentPageContext: function () {
      var mode = this._mode || 'market';
      var rmode = this._copilotResponseMode || 'compact';
      if (mode === 'market') {
        var tab = this._marketWorkspaceTab || 'quote';
        if (tab === 'quote' && this._currentTicker) {
          return {
            surface: 'stock_quote',
            ticker: String(this._currentTicker),
            name: String(this._currentName || ''),
            period: String(this._currentPeriod || '1d'),
            workspace_tab: 'quote',
            response_mode: rmode,
            summary: '个股复盘 ' + this._currentTicker + ' ' + (this._currentName || '') + ' (' + (this._currentPeriod || '1d') + ' K线)'
          };
        }
        if (tab === 'screener') {
          var selInd = document.getElementById('marketScanIndicator');
          var indLabel = selInd && selInd.selectedOptions && selInd.selectedOptions[0] ? selInd.selectedOptions[0].textContent : '';
          var topHits = (this._lastScanMatches || []).slice(0, 8).map(function (m) {
            return (m.ticker || '') + (m.name ? '(' + m.name + ')' : '');
          }).join('、');
          return {
            surface: 'stock_screener',
            workspace_tab: 'screener',
            response_mode: rmode,
            summary: '全市场量化选股' + (indLabel ? ' · 指标：' + indLabel : '') + (topHits ? ' · 前排命中：' + topHits : '')
          };
        }
        if (tab === 'library') {
          var prev = this._indicatorPreview;
          return {
            surface: 'indicator_lab',
            workspace_tab: 'library',
            response_mode: rmode,
            summary: '指标实验室' + (prev && prev.name ? ' · 当前编辑：' + prev.name : '')
          };
        }
        return {
          surface: 'market_cockpit',
          workspace_tab: 'quote',
          response_mode: rmode,
          summary: '行情工作区（自选股池 ' + ((this._watchlist || []).length) + ' 只）'
        };
      }
      if (mode === 'reports') {
        var rtab = this._researchLibraryTab || 'reports';
        return {
          surface: rtab === 'journal' ? 'decision_journal' : 'research_vault',
          workspace_tab: rtab,
          response_mode: rmode,
          summary: rtab === 'journal' ? '全域多空判断与复盘账本' : '精选投研报告归档库'
        };
      }
      if (mode === 'radar') {
        return {
          surface: 'event_radar',
          response_mode: rmode,
          summary: '风向雷达 · 全球与国内实时催化'
        };
      }
      if (mode === 'intel') {
        return {
          surface: 'circle_intel',
          response_mode: rmode,
          summary: '圈内情报与策略共享池'
        };
      }
      return { surface: mode, response_mode: rmode, summary: 'Prism 投研工作台' };
    },

    _buildContextualStarters: function (ctx) {
      if (ctx.surface === 'stock_quote' && ctx.ticker) {
        var labelName = (ctx.name ? ctx.name + '（' + ctx.ticker + '）' : ctx.ticker);
        return [
          {
            title: '全面拆解 ' + labelName,
            sub: '结合估值分位、财报现金流质量与趋势结构给出明确倾向与失效位',
            prompt: '请结合我的投研画像、私人仓位底牌与风控纪律，全面拆解 ' + labelName + '：先给核心结论，再核对估值水平、财报现金流成色、日周线结构以及明确的风险失效条件。',
            skill: 'serenity',
            mode: 'deep'
          },
          {
            title: '核查近期公告与潜在排雷点',
            sub: '检索官方财报与重大公告，排查经营兑现度、解禁减持与商誉风险',
            prompt: '请检索 ' + labelName + ' 近期官方财报与重要公告原文，用精炼要点排查营收利润真实成色、经营现金流匹配度及潜在减持或解禁风险。',
            skill: 'serenity',
            mode: 'compact'
          },
          {
            title: '上下游成本与商品景气传导',
            sub: '对比关联大宗商品与行业景气度，评估对毛利率的边际影响',
            prompt: '请结合产业链上下游与关联大宗商品表现，简明分析近期原材料或产品价格变化对 ' + labelName + ' 毛利率与景气度的影响。',
            skill: 'bottleneck-hunter',
            mode: 'compact'
          }
        ];
      }
      if (ctx.surface === 'stock_screener') {
        return [
          {
            title: '按我的画像与底牌优中选优',
            sub: '结合负面回避清单，从当前量化命中结果中筛出 2–3 只核心观察标的',
            prompt: '请结合当前量化选股命中结果与我的投研画像（含负面回避清单），先给筛选结论，再挑出最契合我风格的 2–3 只观察标的并指出各自风险点。',
            skill: 'prism-research',
            mode: 'compact'
          },
          {
            title: '评估当前选股信号的适用环境',
            sub: '分析该技术指标在震荡市与趋势市中的胜率特征与假信号陷阱',
            prompt: '请简明评估当前量化选股指标的适用行情阶段、常见假信号特征，以及建议搭配的基本面过滤条件。',
            skill: 'prism-research',
            mode: 'compact'
          }
        ];
      }
      if (ctx.surface === 'indicator_lab') {
        return [
          {
            title: '审视当前指标逻辑与参数盲区',
            sub: '指出震荡市磨损风险与可补充的量价过滤条件',
            prompt: '请简明审视当前正在编辑的量化指标逻辑，指出它在震荡市与单边市中的优缺点及改进建议。',
            skill: 'prism-research',
            mode: 'compact'
          }
        ];
      }
      if (ctx.surface === 'decision_journal') {
        return [
          {
            title: '复盘我的历史判断胜率与偏差',
            sub: '对照已印证与已证伪记录，提炼 2 条可落地的操作改进纪律',
            prompt: '请结合我的历史多空判断对账统计与投研画像，先给核心诊断结论，再给出 2 条针对性的操作纪律改进建议。',
            skill: 'prism-research',
            mode: 'compact'
          }
        ];
      }
      if (ctx.surface === 'event_radar') {
        return [
          {
            title: '提炼今日高优先级风向与应对纪律',
            sub: '结合跨市场价格印证与我的仓位底牌，区分真催化与脉冲噪音',
            prompt: '请梳理当前风向雷达中的高优先级催化事件，结合跨市场实时价格反应与我的仓位底牌，指出哪些值得跟踪、哪些需防冲高兑现。',
            skill: 'prism-research',
            mode: 'compact'
          },
          {
            title: '排查风向事件对自选股池的影响',
            sub: '交叉比对最新政策/产业快讯与我当前关注标的的利好利空敞口',
            prompt: '请将最新风向快讯与我的自选股池及持仓底牌做交叉比对，简明列出直接受益或需要警惕冲击的标的。',
            skill: 'prism-research',
            mode: 'compact'
          }
        ];
      }
      if (ctx.surface === 'circle_intel') {
        return [
          {
            title: '交叉核验圈内鲜活情报与实盘盘口',
            sub: '对照当前总仓位底牌对号入座，并给出反方证伪观察位',
            prompt: '请读取当前有效期内的圈内情报，与客观盘口涨跌和我的私人仓位底牌交叉比对，直接给出适合我当前仓位的操作节奏与反方观察位。',
            skill: 'prism-research',
            mode: 'compact'
          }
        ];
      }
      return [
        {
          title: '扫描自选股池异动与排雷点',
          sub: '汇总今日涨跌、量能变化与潜在风险，直出核心结论',
          prompt: '请读取我的自选股池最新行情，先给 3 行核心异动结论，再简明列出今日值得重点关注的标的与潜在风险点。',
          skill: 'prism-research',
          mode: 'compact'
        },
        {
          title: '结合仓位底牌与圈内情报给应对建议',
          sub: '按我当前真实仓位成数对号入座，附反方证伪与止损观察位',
          prompt: '请结合我当前的私人仓位底牌、投研画像与最新圈内情报，给出今日盘面应对节奏（含加减仓条件与反方证伪观察位）。',
          skill: 'prism-research',
          mode: 'compact'
        },
        {
          title: '股期跨市场极值与顺价机会扫描',
          sub: '对比大宗商品期货分位极值与 A 股上下游龙头传导节奏',
          prompt: '请结合我的投研画像，对比当前处于历史分位极值的大宗商品期货品种与对应 A 股产业链龙头的顺价或背离机会。',
          skill: 'futures-research',
          mode: 'compact'
        }
      ];
    },

    showWelcome: function () {
      var list = document.getElementById('msgList');
      if (!list) return;
      var ctxBar = document.getElementById('copilotContextBar');
      if (ctxBar) ctxBar.hidden = true;

      var ctx = this._currentPageContext ? (this._currentPageContext() || {}) : {};
      var desc = ctx.summary || '行情工作区';
      var starters = this._buildContextualStarters ? this._buildContextualStarters(ctx) : [];
      this._welcomeStarters = starters;

      var prof = (this._personaData && this._personaData.profile) || {};
      var archText = prof.configured ? (ARCHETYPE_LABELS[prof.archetype] || '已定制') : '未定制';
      var hub = window._prismPrivateHubSnapshot || null;
      var pos = (hub && hub.position_state) || {};
      var posText = pos.configured ? ('仓位 ' + pos.position_pct + '%') : '仓位未设';
      var watchCnt = hub && Array.isArray(hub.watchman_rules)
        ? hub.watchman_rules.filter(function (r) { return r.enabled; }).length
        : 0;
      var capErr = this._capabilityError
        ? '<div class="cp-welcome__err" role="status">' + esc(this._capabilityError) + ' <button type="button" class="workbench-inline-retry" data-workbench-retry="capabilities">重试</button></div>'
        : '';

      list.innerHTML =
        '<div class="cp-welcome">' +
          '<div class="cp-welcome__ctx">' +
            '<i class="status-dot live"></i>' +
            '<span>已感知视口 · ' + esc(desc) + '</span>' +
          '</div>' +
          '<div class="cp-welcome__mem">' +
            '<div class="cp-welcome__mem-info">' +
              '<span class="cp-mem-kicker">私人记忆</span>' +
              '<span>' + esc(posText) + '</span>' +
              '<span class="cp-mem-sep">·</span>' +
              '<span>画像：' + esc(archText) + '</span>' +
              (watchCnt > 0 ? '<span class="cp-mem-sep">·</span><span>盯盘 ' + watchCnt + ' 条</span>' : '') +
            '</div>' +
            '<div class="cp-welcome__mem-acts">' +
              '<button type="button" onclick="window.PrismPrivateAgent && window.PrismPrivateAgent.open(\'position\')">校准底牌</button>' +
              '<button type="button" onclick="App.openPersonaDialog()">定制画像</button>' +
            '</div>' +
          '</div>' +
          '<div class="cp-welcome__sec-lbl">针对当前画面的快捷提问</div>' +
          '<div class="cp-starter-list">' +
            starters.map(function (st, idx) {
              return (
                '<button type="button" class="cp-starter-card" data-cp-starter="' + idx + '">' +
                  '<div class="cp-starter-card__body">' +
                    '<strong>' + esc(st.title) + '</strong>' +
                    '<span>' + esc(st.sub) + '</span>' +
                  '</div>' +
                  '<i class="cp-starter-card__arr" aria-hidden="true">→</i>' +
                '</button>'
              );
            }).join('') +
          '</div>' +
          capErr +
        '</div>';

      var self = this;
      if (typeof list.querySelectorAll === 'function') {
        list.querySelectorAll('[data-cp-starter]').forEach(function (btn, idx) {
          btn.onclick = function () {
            var item = starters[idx];
            if (!item) return;
            if (!self.model) {
              var inp = document.getElementById('input');
              if (inp) { inp.value = item.prompt; inp.focus(); }
              if (self._toast) self._toast('已填入提问草稿 · 请先在运维控制台激活可用模型后再发送');
              return;
            }
            self.intervene(item.prompt, item.skill, item.mode);
          };
        });
      }
    },

    _refreshCopilotContextBar: function () {
      var ctx = this._currentPageContext() || {};
      var subEl = document.getElementById('copilotLauncherSub');
      var labelEl = document.getElementById('copilotCtxLabel');
      var ctxBar = document.getElementById('copilotContextBar');
      var desc = ctx.summary || '投研工作台';
      if (subEl) subEl.textContent = '已感知：' + desc;
      if (labelEl) labelEl.textContent = '当前关联：' + desc;

      var hasMsgs = !!(this.msgs && this.msgs.length > 0);
      if (ctxBar) ctxBar.hidden = !hasMsgs;
      if (!hasMsgs && this.showWelcome) {
        this.showWelcome();
      }
    },

    setCopilotResponseMode: function (mode) {
      var next = mode === 'deep' ? 'deep' : 'compact';
      this._copilotResponseMode = next;
      try { localStorage.setItem('prism-copilot-mode', next); } catch (e) {}
      this._syncCopilotSpeedToggle();
      if (this._toast) {
        this._toast(next === 'deep'
          ? '已切换「深度研报模式」：拉取巨潮原文PDF并执行逐条核验'
          : '已切换「敏捷随行模式」：预注实时上下文，快速直出中文结论');
      }
    },

    _syncCopilotSpeedToggle: function () {
      var wrap = document.getElementById('copilotSpeedToggle');
      if (!wrap) return;
      var cur = this._copilotResponseMode === 'deep' ? 'deep' : 'compact';
      wrap.querySelectorAll('.speed-opt').forEach(function (btn) {
        var active = btn.getAttribute('data-speed') === cur;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    },

    intervene: async function (promptText, preferredSkill, responseMode) {
      if (!promptText) return;
      var prevMode = this._copilotResponseMode === 'deep' ? 'deep' : 'compact';
      this._copilotResponseMode = responseMode === 'deep' ? 'deep' : 'compact';
      this._syncCopilotSpeedToggle();
      this.openCopilot('docked', true);
      if (preferredSkill) {
        this.setSkill(preferredSkill);
        if (this.renderSkillsBar) this.renderSkillsBar();
      }
      var stream = this.convId ? Streams[this.convId] : null;
      if (stream && stream.active) {
        this._copilotResponseMode = prevMode;
        this._syncCopilotSpeedToggle();
        if (this._toast) this._toast('当前会话正在推演中，请稍候或点击停止后再发起新介入');
        return;
      }
      var inp = document.getElementById('input');
      if (!inp) return;
      if (this.msgs && this.msgs.length > 0) {
        await this.newConv(true);
      }
      inp.value = promptText;
      inp.dispatchEvent(new Event('input'));
      await this.send();
      this._copilotResponseMode = prevMode;
      this._syncCopilotSpeedToggle();
    },

    /* ═══ Persona Engine & Human-Confirmed Trait UI ═══ */
    loadPersona: async function () {
      if (!this.user) return;
      try {
        var r = await this.api('/api/preferences/persona');
        if (!r.ok) return;
        this._personaData = await r.json();
        var prof = (this._personaData && this._personaData.profile) || {};
        this._selectedArchetype = prof.archetype || (prof.configured ? 'custom' : 'cycle');
        this._selectedNegativeRules = Array.isArray(prof.negative_rules) ? prof.negative_rules.slice() : [];
        this._syncPersonaBadges();
        this._renderProposalBanner();
        var dlg = document.getElementById('personaDialog');
        if (dlg && dlg.open) this._renderPersonaDialogBody();
      } catch (e) {}
    },

    _syncPersonaBadges: function () {
      var btn = document.getElementById('copilotPersonaBtn');
      var topBtn = document.getElementById('topbarPersonaBtn');
      var prof = (this._personaData && this._personaData.profile) || {};
      var archText = ARCHETYPE_LABELS[prof.archetype] || '已启用';
      var label = prof.configured ? (archText + ' ▾') : '画像 ▾';
      if (btn) btn.textContent = label;
      if (topBtn) {
        var span = topBtn.querySelector ? topBtn.querySelector('span') : null;
        if (span) span.textContent = prof.configured ? ('画像 · ' + archText) : '投研画像';
      }
      if (!this.msgs || !this.msgs.length) {
        if (this.showWelcome) this.showWelcome();
      }
    },

    _renderProposalBanner: function () {
      var bar = document.getElementById('copilotProposalBar');
      if (!bar) return;
      var pending = (this._personaData && this._personaData.pending_proposals) || [];
      if (!pending.length) {
        bar.hidden = true;
        bar.innerHTML = '';
        return;
      }
      var p = pending[0];
      bar.hidden = false;
      bar.innerHTML =
        '<div class="copilot-proposal-row">' +
          '<span>提议记入你的专属画像：<b>' + esc(p.trait_text) + '</b></span>' +
          '<span class="copilot-proposal-btns">' +
            '<button type="button" class="primary" onclick="App.decidePersonaProposal(\'' + escAttr(p.id) + '\',\'accept\')">✓ 记入画像</button>' +
            '<button type="button" onclick="App.decidePersonaProposal(\'' + escAttr(p.id) + '\',\'dismiss\')">忽略</button>' +
          '</span>' +
        '</div>';
    },

    decidePersonaProposal: async function (proposalId, decision) {
      if (!proposalId) return;
      try {
        var r = await this.api('/api/preferences/proposals/' + encodeURIComponent(proposalId) + '/decide', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision: decision })
        });
        if (r.ok) {
          if (this._toast) this._toast(decision === 'accept' ? '已写入你的独立投研画像，后续分析将自动遵循' : '已忽略该画像提议');
          await this.loadPersona();
        }
      } catch (e) {
        if (this._toast) this._toast('操作未完成，请稍后重试');
      }
    },

    openPersonaDialog: async function () {
      var dlg = document.getElementById('personaDialog');
      if (!dlg) return;
      if (!dlg.open) dlg.showModal();
      await this.loadPersona();
      this._renderPersonaDialogBody();
      var details = document.getElementById('portfolioDetails');
      if (details && !details._hubWired) {
        details._hubWired = true;
        details.addEventListener('toggle', function () {
          if (details.open && window.PrismPreferences) window.PrismPreferences.activate();
        });
      }
    },

    closePersonaDialog: function () {
      var dlg = document.getElementById('personaDialog');
      if (dlg && dlg.open) dlg.close();
    },

    selectPersonaArchetype: function (archId) {
      this._selectedArchetype = archId;
      var archetypes = (this._personaData && this._personaData.archetypes) || [];
      var found = archetypes.find(function (a) { return a.id === archId; });
      if (found && found.defaults) {
        var d = found.defaults;
        var form = document.getElementById('personaProfileForm');
        if (form) {
          form.elements.style.value = d.style;
          form.elements.horizon.value = d.horizon;
          form.elements.max_position_pct.value = d.max_position_pct;
          form.elements.max_drawdown_pct.value = d.max_drawdown_pct;
          form.elements.allow_leverage.checked = !!d.allow_leverage;
        }
      }
      var cards = document.querySelectorAll('.persona-arch-card');
      cards.forEach(function (c) {
        c.classList.toggle('active', c.getAttribute('data-arch') === archId);
      });
    },

    toggleNegativeRule: function (ruleText) {
      var idx = this._selectedNegativeRules.indexOf(ruleText);
      if (idx >= 0) this._selectedNegativeRules.splice(idx, 1);
      else if (this._selectedNegativeRules.length < 12) this._selectedNegativeRules.push(ruleText);
      this._renderNegativeRuleChips();
    },

    _renderNegativeRuleChips: function () {
      var wrap = document.getElementById('personaNegChips');
      if (!wrap) return;
      var allRules = PRESET_NEGATIVE_RULES.slice();
      this._selectedNegativeRules.forEach(function (r) {
        if (allRules.indexOf(r) < 0) allRules.push(r);
      });
      var self = this;
      wrap.innerHTML = allRules.map(function (r) {
        var on = self._selectedNegativeRules.indexOf(r) >= 0;
        return '<button type="button" class="persona-neg-chip' + (on ? ' active' : '') + '" data-neg="' + escAttr(r) + '">' +
          '<i aria-hidden="true">' + (on ? '✓' : '＋') + '</i>' + esc(r) + '</button>';
      }).join('');
      wrap.querySelectorAll('[data-neg]').forEach(function (btn) {
        btn.onclick = function () { self.toggleNegativeRule(btn.getAttribute('data-neg')); };
      });
    },

    applyPersonaTemplate: function (tplId) {
      var tpl = PERSONA_TEMPLATES.find(function (t) { return t.id === tplId; });
      if (!tpl) return;
      this._selectedTemplateId = tpl.id;
      this._selectedArchetype = tpl.archetype;
      this._selectedNegativeRules = tpl.negative_rules.slice();
      this._pendingTemplateTraits = (tpl.traits || []).slice();

      var form = document.getElementById('personaProfileForm');
      if (form) {
        form.elements.style.value = tpl.style;
        form.elements.horizon.value = tpl.horizon;
        form.elements.max_position_pct.value = tpl.max_position_pct;
        form.elements.max_drawdown_pct.value = tpl.max_drawdown_pct;
        form.elements.allow_leverage.checked = !!tpl.allow_leverage;
        form.elements.decision_style_note.value = tpl.note || '';
      }
      document.querySelectorAll('.persona-arch-card').forEach(function (c) {
        c.classList.toggle('active', c.getAttribute('data-arch') === tpl.archetype);
      });
      document.querySelectorAll('.persona-tpl-card').forEach(function (c) {
        c.classList.toggle('active', c.getAttribute('data-tpl-id') === tpl.id);
      });
      this._renderNegativeRuleChips();
      var hint = document.getElementById('personaSaveHint');
      if (hint) {
        hint.textContent = '已套用「' + tpl.name + '」参数与 ' + this._pendingTemplateTraits.length + ' 条配套准则 · 点击右侧保存立即生效';
      }
      if (this._toast) {
        this._toast('已填入「' + tpl.name + '」模板，可微调任意项后点击保存');
      }
    },

    quickAddPresetTrait: async function (traitText) {
      var text = String(traitText || '').trim();
      if (!text) return;
      var existing = ((this._personaData && this._personaData.traits) || []).some(function (t) {
        return t.trait_text === text;
      });
      if (existing) {
        if (this._toast) this._toast('该条准则已在你的记忆卡中');
        return;
      }
      try {
        var r = await this.api('/api/preferences/traits', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trait_text: text, category: 'discipline' })
        });
        if (r.ok) {
          if (this._toast) this._toast('✓ 已加入投研记忆卡');
          await this.loadPersona();
          this._renderPersonaDialogBody();
        }
      } catch (e) {}
    },

    _renderPersonaDialogBody: function () {
      var mount = document.getElementById('personaCoreMount');
      if (!mount) return;
      var data = this._personaData || {};
      var prof = data.profile || { configured: false, version: 0 };
      var archetypes = data.archetypes || [
        { id: 'value', name: '价值基本面派', tagline: '重财报现金流、ROE 与估值安全边际' },
        { id: 'cycle', name: '产业周期与供需派', tagline: '重上下游产能瓶颈、期现基差与库存利润传导' },
        { id: 'trend', name: '趋势动量与量化派', tagline: '重均线量价共振、波动率带宽与右侧止损纪律' },
        { id: 'defensive', name: '稳健绝对收益 / 套利派', tagline: '回避高分位追涨，偏好低分位安全垫与期现套利' }
      ];
      var traits = data.traits || [];
      var existingTraitTexts = traits.map(function (t) { return t.trait_text; });
      var jstats = data.journal_stats || { total: 0, bias_hint: '' };
      var curArch = this._selectedArchetype || prof.archetype || 'cycle';
      var curStyle = prof.configured ? prof.style : 'balanced';
      var curHorizon = prof.configured ? prof.horizon : 'medium';
      var curPos = prof.configured ? prof.max_position_pct : 20;
      var curDd = prof.configured ? prof.max_drawdown_pct : 12;
      var curLev = prof.configured ? !!prof.allow_leverage : false;
      var curNote = prof.decision_style_note || '';
      var curTpl = this._selectedTemplateId || '';

      mount.innerHTML =
        '<section class="persona-section persona-tpl-section">' +
          '<div class="persona-section__hd">' +
            '<div><span class="persona-step-num">模板</span><h3>一键套用成熟画像模板（新手免手填）</h3></div>' +
            '<span class="persona-section__sub">点选即自动填好流派、仓位回撤红线、排雷项与推荐记忆准则，支持二次微调</span>' +
          '</div>' +
          '<div class="persona-tpl-grid">' +
            PERSONA_TEMPLATES.map(function (tp) {
              var on = tp.id === curTpl ? ' active' : '';
              return (
                '<button type="button" class="persona-tpl-card' + on + '" data-tpl-id="' + escAttr(tp.id) + '" onclick="App.applyPersonaTemplate(\'' + escAttr(tp.id) + '\')">' +
                  '<strong>' + esc(tp.name) + '</strong>' +
                  '<span>' + esc(tp.fit) + '</span>' +
                '</button>'
              );
            }).join('') +
          '</div>' +
        '</section>' +

        '<form id="personaProfileForm" class="persona-sections" style="margin-top:14px;" onsubmit="App.savePersonaProfile(event)">' +
          '<section class="persona-section">' +
            '<div class="persona-section__hd">' +
              '<div><span class="persona-step-num">01</span><h3>主导投资流派</h3></div>' +
              '<span class="persona-section__sub">决定 Agent 切入视角与指标权重，不降低证据核验标准</span>' +
            '</div>' +
            '<div class="persona-archetype-grid">' +
              archetypes.map(function (a) {
                var active = a.id === curArch ? ' active' : '';
                return '<button type="button" class="persona-arch-card' + active + '" data-arch="' + escAttr(a.id) + '" onclick="App.selectPersonaArchetype(\'' + escAttr(a.id) + '\')">' +
                  '<div class="persona-arch-card__top"><strong>' + esc(a.name) + '</strong><em class="persona-arch-check">当前流派</em></div>' +
                  '<span>' + esc(a.tagline) + '</span>' +
                '</button>';
              }).join('') +
            '</div>' +
          '</section>' +

          '<section class="persona-section">' +
            '<div class="persona-section__hd">' +
              '<div><span class="persona-step-num">02</span><h3>风控红线与负面排雷清单</h3></div>' +
              '<span class="persona-section__sub">仓位与回撤约束将在情景比较中强制校验</span>' +
            '</div>' +
            '<div class="persona-form-grid">' +
              '<label class="persona-field"><span>风险偏好基调</span><select name="style">' +
                '<option value="conservative"' + (curStyle === 'conservative' ? ' selected' : '') + '>保守 · 优先控制本金回撤</option>' +
                '<option value="balanced"' + (curStyle === 'balanced' ? ' selected' : '') + '>均衡 · 权衡赔率与确定性</option>' +
                '<option value="aggressive"' + (curStyle === 'aggressive' ? ' selected' : '') + '>积极 · 承受波动博取弹性</option>' +
              '</select></label>' +
              '<label class="persona-field"><span>主力持仓周期</span><select name="horizon">' +
                '<option value="short"' + (curHorizon === 'short' ? ' selected' : '') + '>短期波段（数日 ~ 2 周）</option>' +
                '<option value="medium"' + (curHorizon === 'medium' ? ' selected' : '') + '>中期趋势（数周 ~ 季度）</option>' +
                '<option value="long"' + (curHorizon === 'long' ? ' selected' : '') + '>长期配置（季度 ~ 年度）</option>' +
              '</select></label>' +
              '<label class="persona-field"><span>单标的仓位上限</span><div class="persona-num-wrap"><input name="max_position_pct" type="number" min="1" max="100" value="' + Number(curPos) + '" required><b>%</b></div></label>' +
              '<label class="persona-field"><span>账户最大回撤红线</span><div class="persona-num-wrap"><input name="max_drawdown_pct" type="number" min="1" max="50" value="' + Number(curDd) + '" required><b>%</b></div></label>' +
            '</div>' +
            '<label class="persona-toggle-row">' +
              '<div class="persona-toggle-copy"><strong>允许讨论期货杠杆、做空或套保对冲策略</strong><span>关闭时 Agent 仅提供现货/正股多头与观望情景，不推荐衍生品杠杆方向</span></div>' +
              '<span class="persona-switch"><input name="allow_leverage" type="checkbox"' + (curLev ? ' checked' : '') + '><i></i></span>' +
            '</label>' +
            '<div class="persona-subblock">' +
              '<span class="persona-sublabel">负面回避清单（点击激活，选股与诊股时自动排雷）</span>' +
              '<div class="persona-neg-chips" id="personaNegChips"></div>' +
            '</div>' +
            '<label class="persona-field persona-field--wide" style="margin-top:12px;">' +
              '<span>输出与决策风格备注（选填）</span>' +
              '<input name="decision_style_note" maxlength="300" value="' + escAttr(curNote) + '" placeholder="例如：先给明确多空倾向与失效价格线，再列核心证据，不写空泛宏观套话">' +
            '</label>' +
            '<div class="persona-form-footer">' +
              '<span class="persona-save-hint" id="personaSaveHint">' + (prof.configured ? ('当前生效版本 v' + prof.version + ' · 保存后下轮立即采用') : '可点上方「基础模板」一键填好，再点右侧保存启用') + '</span>' +
              '<button type="submit" class="market-primary-btn">保存画像与风控设置</button>' +
            '</div>' +
          '</section>' +
        '</form>' +

        '<section class="persona-section" style="margin-top:14px;">' +
          '<div class="persona-section__hd">' +
            '<div><span class="persona-step-num">03</span><h3>个体化投研记忆卡（人工确认制）</h3></div>' +
            '<span class="persona-section__sub">已记录 ' + traits.length + ' / 30 条</span>' +
          '</div>' +
          (jstats.total > 0
            ? '<div class="journal-bias-banner" style="margin-bottom:10px;"><span>复盘账本联动：已登记 <b>' + jstats.total + '</b> 笔判断（印证 ' + jstats.supported + ' / 证伪 ' + jstats.contradicted + '）· ' + esc(jstats.bias_hint) + '</span></div>'
            : '') +
          '<div class="persona-subblock" style="margin-bottom:10px;">' +
            '<span class="persona-sublabel">常用实战纪律模板（点击即可一键加入记忆卡）</span>' +
            '<div class="persona-neg-chips">' +
              PRESET_PERSONA_TRAITS.map(function (pt) {
                var added = existingTraitTexts.indexOf(pt) >= 0;
                return '<button type="button" class="persona-neg-chip' + (added ? ' active' : '') + '" ' +
                  (added ? 'disabled ' : '') +
                  'onclick="App.quickAddPresetTrait(\'' + escAttr(pt) + '\')">' +
                  '<i aria-hidden="true">' + (added ? '✓' : '＋') + '</i>' + esc(pt) + '</button>';
              }).join('') +
            '</div>' +
          '</div>' +
          '<div class="persona-trait-list" id="personaTraitList">' +
            (traits.length
              ? traits.map(function (t) {
                  return '<div class="persona-trait-item">' +
                    '<span>' + esc(t.trait_text) + ' <small>(' + (t.source === 'agent_confirmed' ? '对话提议确认' : '手动添加') + ')</small></span>' +
                    '<button type="button" class="copilot-icon-btn" title="移除此准则" onclick="App.deletePersonaTrait(\'' + escAttr(t.id) + '\')">×</button>' +
                  '</div>';
                }).join('')
              : '<div class="persona-trait-empty">暂无个体化记忆条目。可点击上方「常用实战纪律模板」一键添加，或在下方自定义输入。</div>') +
          '</div>' +
          '<form class="persona-trait-add" onsubmit="App.addPersonaTrait(event)">' +
            '<input id="newPersonaTraitInput" maxlength="200" autocomplete="off" placeholder="自定义长期研究准则，如：看消费股必查经营现金流与合同负债">' +
            '<button type="submit" class="market-secondary-btn">＋ 添加准则</button>' +
          '</form>' +
        '</section>';

      this._renderNegativeRuleChips();
    },

    savePersonaProfile: async function (ev) {
      if (ev) ev.preventDefault();
      var form = document.getElementById('personaProfileForm');
      if (!form) return;
      var curVersion = (this._personaData && this._personaData.profile && this._personaData.profile.version) || 0;
      var payload = {
        expected_version: Number(curVersion),
        style: form.elements.style.value,
        horizon: form.elements.horizon.value,
        max_position_pct: Number(form.elements.max_position_pct.value),
        max_drawdown_pct: Number(form.elements.max_drawdown_pct.value),
        allow_leverage: !!form.elements.allow_leverage.checked,
        archetype: this._selectedArchetype || 'cycle',
        focus_markets: ['ashare', 'futures', 'cross_asset'],
        negative_rules: this._selectedNegativeRules.slice(),
        decision_style_note: String(form.elements.decision_style_note.value || '').trim()
      };
      try {
        var r = await this.api('/api/preferences/research', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!r.ok) {
          var err = await r.json().catch(function () { return {}; });
          if (this._toast) this._toast(err.detail || '画像保存未完成，请重新打开核对');
          return;
        }
        var pendingTraits = (this._pendingTemplateTraits || []).slice();
        this._pendingTemplateTraits = [];
        if (pendingTraits.length) {
          var existing = ((this._personaData && this._personaData.traits) || []).map(function (t) { return t.trait_text; });
          for (var i = 0; i < pendingTraits.length; i++) {
            if (existing.indexOf(pendingTraits[i]) < 0) {
              await this.api('/api/preferences/traits', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trait_text: pendingTraits[i], category: 'discipline' })
              }).catch(function () {});
            }
          }
        }
        if (this._toast) this._toast('✓ 投研画像已保存生效，下一轮 Agent 推演将立即采用');
        await this.loadPersona();
        var dlg = document.getElementById('personaDialog');
        if (dlg && dlg.open) this._renderPersonaDialogBody();
      } catch (e) {
        if (this._toast) this._toast('网络异常，画像保存未确认');
      }
    },

    addPersonaTrait: async function (ev) {
      if (ev) ev.preventDefault();
      var inp = document.getElementById('newPersonaTraitInput');
      var text = inp ? inp.value.trim() : '';
      if (text.length < 2) return;
      try {
        var r = await this.api('/api/preferences/traits', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trait_text: text, category: 'focus' })
        });
        if (r.ok) {
          inp.value = '';
          await this.loadPersona();
        } else {
          var eb = await r.json().catch(function () { return {}; });
          if (this._toast) this._toast(eb.detail || '添加失败');
        }
      } catch (e) {}
    },

    deletePersonaTrait: async function (id) {
      if (!id) return;
      try {
        var r = await this.api('/api/preferences/traits/' + encodeURIComponent(id), { method: 'DELETE' });
        if (r.ok) await this.loadPersona();
      } catch (e) {}
    },

    /* ═══ Stock Judgment Quick-Recorder ═══ */
    openEquityJudgmentModal: function () {
      if (!this._currentTicker) {
        if (this._toast) this._toast('请先选择一只股票');
        return;
      }
      var dlg = document.getElementById('equityJudgmentDialog');
      if (!dlg) return;
      document.getElementById('eqJudgeStockLabel').textContent = (this._currentName || '') + ' (' + this._currentTicker + ')';
      var lastClose = (this._lastKline && this._lastKline.length) ? this._lastKline[this._lastKline.length - 1].close : '';
      document.getElementById('eqJudgeRefPrice').value = this._currentLatestPrice || lastClose || '';
      document.getElementById('eqJudgeInvPrice').value = '';
      document.getElementById('eqJudgeTargetPrice').value = '';
      document.getElementById('eqJudgeThesis').value = '';
      if (!dlg.open) dlg.showModal();
    },

    submitEquityJudgment: async function (ev) {
      if (ev) ev.preventDefault();
      var refVal = Number(document.getElementById('eqJudgeRefPrice').value);
      var invRaw = document.getElementById('eqJudgeInvPrice').value.trim();
      var tgtRaw = document.getElementById('eqJudgeTargetPrice').value.trim();
      var dir = document.getElementById('eqJudgeDirection').value;
      var horizon = Number(document.getElementById('eqJudgeHorizon').value || 10);
      var thesis = document.getElementById('eqJudgeThesis').value.trim();
      if (!refVal || refVal <= 0 || thesis.length < 2) {
        if (this._toast) this._toast('请填写参考价格与核心逻辑');
        return;
      }
      var payload = {
        ticker: this._currentTicker,
        name: this._currentName || this._currentTicker,
        direction: dir,
        reference_price: refVal,
        invalidation_price: invRaw ? Number(invRaw) : null,
        target_price: tgtRaw ? Number(tgtRaw) : null,
        horizon_days: horizon,
        thesis: thesis
      };
      var r = await this.api('/api/journal/equity', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (!r.ok) {
        var eb = await r.json().catch(function () { return {}; });
        if (this._toast) this._toast(eb.detail || '保存判断未成功，请核对止损价方向');
        return;
      }
      var dlg = document.getElementById('equityJudgmentDialog');
      if (dlg && dlg.open) dlg.close();
      if (this._toast) this._toast('已记入全域复盘账本，可在「研究与复盘」随时一键行情对账');
      this.loadPersona();
    }
  });

  if (typeof origNewConv === 'function') {
    App.newConv = async function () {
      if (this._copilotAutoWidened && !this._copilotUserWidth) {
        this._copilotAutoWidened = false;
        setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
        var appShell = document.getElementById('appShell');
        if (appShell) appShell.classList.remove('copilot-auto-wide');
      }
      return origNewConv.apply(this, arguments);
    };
  }
}());
