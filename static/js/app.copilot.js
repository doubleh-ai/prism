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
          summary: rtab === 'journal' ? '全域多空判断与自动行情对账账本' : '精选投研报告归档库'
        };
      }
      return { surface: mode, response_mode: rmode, summary: 'Prism 投研工作台' };
    },

    _refreshCopilotContextBar: function () {
      var ctx = this._currentPageContext() || {};
      var subEl = document.getElementById('copilotLauncherSub');
      var labelEl = document.getElementById('copilotCtxLabel');
      var actionsEl = document.getElementById('copilotCtxActions');
      var desc = ctx.summary || '跨板块随行投研助手';
      if (subEl) subEl.textContent = '已感知：' + desc;
      if (labelEl) labelEl.textContent = '✦ ' + desc;
      if (!actionsEl) return;

      var triggers = [];
      if (ctx.surface === 'stock_quote' && ctx.ticker) {
        var labelName = (ctx.name ? ctx.name + '（' + ctx.ticker + '）' : ctx.ticker);
        triggers = [
          ['按画像深度诊股', '请结合我的投研画像与风控纪律，全面拆解 ' + labelName + '：先给 3 行核心结论，再核对最新估值、财报现金流、日周线趋势结构、上下游传导与风险失效条件。', 'serenity', 'deep'],
          ['快查公告与财报排雷', '请检索并读取 ' + labelName + ' 近期巨潮官方财报/核心公告原文，用精炼要点排查营收利润成色、经营现金流匹配度及潜在减持/解禁/商誉风险。', 'serenity', 'compact'],
          ['产业链与商品传导', '请结合产业链上下游与关联大宗商品期货表现，简明分析成本或产品价格波动对 ' + labelName + ' 毛利率与景气度的影响。', 'bottleneck-hunter', 'compact']
        ];
      } else if (ctx.surface === 'stock_screener') {
        triggers = [
          ['按画像优中选优', '请读取当前量化选股任务命中标的与我的投研画像（含负面回避清单），先给筛选结论，再筛出最契合我风格的 2-3 只核心观察标的并指出各自风险点。', 'prism-research', 'compact'],
          ['评估信号适用阶段', '请简明评估该技术指标扫描结果的适用行情阶段与常见假信号陷阱。', 'prism-research', 'compact']
        ];
      } else if (ctx.surface === 'indicator_lab') {
        triggers = [
          ['审视指标参数盲区', '请简明审视当前正在配置的量化指标逻辑，指出它在震荡市与单边市中的优缺点及建议过滤条件。', 'prism-research', 'compact']
        ];
      } else if (ctx.surface === 'decision_journal') {
        triggers = [
          ['复盘我的胜率与偏差', '请结合我的历史多空判断对账统计与投研画像，先给核心诊断，再给出 2 条具体的改进纪律建议。', 'prism-research', 'compact']
        ];
      } else {
        triggers = [
          ['扫描自选异动与排雷', '请调用 watchlist_get 读取我的自选股池，先给 3 行核心结论，再简明列出今日值得重点关注的异动标的与潜在风险。', 'prism-research', 'compact'],
          ['股期跨市场机会扫描', '请结合我的投研画像，对比当前大宗商品期货极值品种与对应 A 股产业链龙头的顺价或背离机会。', 'futures-research', 'compact']
        ];
      }

      var self = this;
      actionsEl.innerHTML = triggers.map(function (t, i) {
        return '<button type="button" class="copilot-trigger-chip" data-cp-trig="' + i + '">✦ ' + esc(t[0]) + '</button>';
      }).join('');
      actionsEl.querySelectorAll('[data-cp-trig]').forEach(function (btn, idx) {
        btn.onclick = function () {
          var item = triggers[idx];
          self.intervene(item[1], item[2], item[3]);
        };
      });
    },

    intervene: async function (promptText, preferredSkill, responseMode) {
      if (!promptText) return;
      this._copilotResponseMode = responseMode === 'deep' ? 'deep' : 'compact';
      this.openCopilot('docked', true);
      if (preferredSkill) {
        this.setSkill(preferredSkill);
        if (this.renderSkillsBar) this.renderSkillsBar();
      }
      var stream = this.convId ? Streams[this.convId] : null;
      if (stream && stream.active) {
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
      this._copilotResponseMode = 'compact';
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
      var label = prof.configured ? ('画像 · ' + archText + ' ▾') : '画像 · 待设置 ▾';
      if (btn) btn.textContent = label;
      if (topBtn) {
        var span = topBtn.querySelector ? topBtn.querySelector('span') : null;
        if (span) span.textContent = prof.configured ? ('画像 · ' + archText) : '投研画像';
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
          '<span>✦ 提议记入你的专属画像：<b>' + esc(p.trait_text) + '</b></span>' +
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
      var jstats = data.journal_stats || { total: 0, bias_hint: '' };
      var curArch = this._selectedArchetype || prof.archetype || 'cycle';
      var curStyle = prof.configured ? prof.style : 'balanced';
      var curHorizon = prof.configured ? prof.horizon : 'medium';
      var curPos = prof.configured ? prof.max_position_pct : 20;
      var curDd = prof.configured ? prof.max_drawdown_pct : 12;
      var curLev = prof.configured ? !!prof.allow_leverage : false;
      var curNote = prof.decision_style_note || '';

      mount.innerHTML =
        '<form id="personaProfileForm" class="persona-sections" onsubmit="App.savePersonaProfile(event)">' +
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
              '<span class="persona-save-hint" id="personaSaveHint">' + (prof.configured ? ('当前生效版本 v' + prof.version + ' · 保存后下轮立即采用') : '尚未保存，点击右侧启用专属画像') + '</span>' +
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
          '<div class="persona-trait-list" id="personaTraitList">' +
            (traits.length
              ? traits.map(function (t) {
                  return '<div class="persona-trait-item">' +
                    '<span>' + esc(t.trait_text) + ' <small>(' + (t.source === 'agent_confirmed' ? '对话提议确认' : '手动添加') + ')</small></span>' +
                    '<button type="button" class="copilot-icon-btn" title="移除此准则" onclick="App.deletePersonaTrait(\'' + escAttr(t.id) + '\')">×</button>' +
                  '</div>';
                }).join('')
              : '<div class="persona-trait-empty">暂无个体化记忆条目。可在下方手动添加，或在对话中确认 Agent 提议的研究习惯。</div>') +
          '</div>' +
          '<form class="persona-trait-add" onsubmit="App.addPersonaTrait(event)">' +
            '<input id="newPersonaTraitInput" maxlength="200" autocomplete="off" placeholder="添加长期研究准则，如：看消费股必查经营现金流与合同负债">' +
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
        if (this._toast) this._toast('投研画像已保存生效，下一轮 Agent 推演将立即采用');
        await this.loadPersona();
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
