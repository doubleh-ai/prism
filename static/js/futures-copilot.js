/* Prism V3.1 — Unified Shell Chrome, Resizable Copilot, Zen Reader & Inline Persona Modal for /futures */
(function () {
  'use strict';

  var DEFAULT_COPILOT_W = 480;
  var AUTO_WIDE_COPILOT_W = 700;
  var MIN_COPILOT_W = 380;
  var MAX_COPILOT_W = 920;

  var ARCHETYPE_NAMES = {
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
      id: 'steady_core', name: '稳健底仓 · 龙头白马', fit: '适合稳健配置（新手推荐）',
      archetype: 'value', style: 'conservative', horizon: 'long', max_position_pct: 20, max_drawdown_pct: 10, allow_leverage: false,
      negative_rules: ['排除 ST 与退市整理风险标的', '回避近5年 >85% 分位的单边追高', '回避缺少现金流兑现的纯概念题材'],
      note: '结论先行，优先核查扣非净利与经营现金流匹配度；估值高于历史 70% 分位时主动提示回调风险。',
      traits: ['财报分析必查经营现金流净额是否持续覆盖净利润', '政策预期快速拉升时不重仓追涨，优先等缩量回调机会']
    },
    {
      id: 'growth_trend', name: '主线景气 · 右侧趋势', fit: '适合科技成长主线波段',
      archetype: 'trend', style: 'balanced', horizon: 'medium', max_position_pct: 25, max_drawdown_pct: 12, allow_leverage: false,
      negative_rules: ['排除 ST 与退市整理风险标的', '日成交额低于 2 亿元不参与', '触及预设止损失效位无条件离场，不逆势补仓'],
      note: '聚焦订单排产与行业景气边际加速，每次推演必须给出明确的均线支撑位与破位止损线。',
      traits: ['优先选择有真实订单与业绩兑现的主线龙头或核心 ETF', '高位放量滞涨或有效跌破 20 日均线时果断减仓保护利润']
    },
    {
      id: 'cycle_cross', name: '产业供需 · 股期联动', fit: '适合大宗周期与期现联动',
      archetype: 'cycle', style: 'balanced', horizon: 'medium', max_position_pct: 25, max_drawdown_pct: 15, allow_leverage: true,
      negative_rules: ['排除 ST 与退市整理风险标的', '回避近5年 >85% 分位的单边追高', '触及预设止损失效位无条件离场，不逆势补仓'],
      note: '必查近 5 年价格分位、期现基差与交易所仓单库存，严格区分供给收缩与终端需求真实回暖。',
      traits: ['看多周期品前先核对下游利润与开工率能否承接顺价', '商品价格处于近 5 年 85% 以上极高分位时重点防范利好兑现']
    },
    {
      id: 'catalyst_agile', name: '催化快反 · 小仓试错', fit: '适合风向与圈内情报短线应对',
      archetype: 'trend', style: 'aggressive', horizon: 'short', max_position_pct: 15, max_drawdown_pct: 8, allow_leverage: false,
      negative_rules: ['排除 ST 与退市整理风险标的', '回避近5年 >85% 分位的单边追高', '日成交额低于 2 亿元不参与', '触及预设止损失效位无条件离场，不逆势补仓'],
      note: '直奔核心催化增量与市场预期差；若盘口已提前抢跑大涨，必须第一时间警示冲高分化风险。',
      traits: ['圈内传闻必须交叉比对当日成交量能与前排龙头承接力度', '短线试错单笔亏损触及止损线立即离场，绝不拖成被动长线']
    },
    {
      id: 'defensive_yield', name: '低波红利 · 绝对收益', fit: '适合防守底仓与期现套保对冲',
      archetype: 'defensive', style: 'conservative', horizon: 'long', max_position_pct: 20, max_drawdown_pct: 6, allow_leverage: true,
      negative_rules: ['排除 ST 与退市整理风险标的', '回避近5年 >85% 分位的单边追高', '日成交额低于 2 亿元不参与', '回避缺少现金流兑现的纯概念题材'],
      note: '以本金安全与赔率确定性为第一优先级，重点评估股息率安全垫、自由现金流与极端情景压力测试。',
      traits: ['仅将连续三年分红稳定且自由现金流充沛的标的纳入底仓', '市场一致预期过热时主动分批降仓锁定确定性收益']
    }
  ];

  var state = {
    account: '',
    openState: 'closed',
    split: true,
    userWidth: 0,
    autoWidened: false,
    convId: '',
    model: '',
    models: [],
    persona: null,
    selectedArchetype: 'cycle',
    selectedNegativeRules: [],
    streaming: false,
    abortCtrl: null,
    lastHtml: '',
    lastQuestion: '',
    pageContext: { surface: 'futures_radar', summary: '期货极值与对账雷达' }
  };

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function setCssVar(name, val) {
    var root = document.documentElement;
    if (root && root.style && typeof root.style.setProperty === 'function') {
      root.style.setProperty(name, val);
    }
  }

  function renderSimpleMarkdown(raw) {
    var src = String(raw || '').replace(/\r\n/g, '\n');
    if (!src.trim()) return '';
    var lines = src.split('\n');
    var out = [];
    var inList = false;
    var inCode = false;
    var codeBuf = [];
    var tableRows = [];

    function flushList() {
      if (inList) { out.push('</ul>'); inList = false; }
    }
    function flushTable() {
      if (!tableRows.length) return;
      var header = tableRows[0];
      var body = tableRows.slice(1).filter(function (r) {
        return !r.every(function (c) { return /^[-:\s]+$/.test(c); });
      });
      var ths = header.map(function (c) { return '<th>' + inlineFmt(c) + '</th>'; }).join('');
      var trs = body.map(function (r) {
        return '<tr>' + r.map(function (c) { return '<td>' + inlineFmt(c) + '</td>'; }).join('') + '</tr>';
      }).join('');
      out.push('<table><thead><tr>' + ths + '</tr></thead><tbody>' + trs + '</tbody></table>');
      tableRows = [];
    }
    function inlineFmt(text) {
      return esc(text)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (line.trim().indexOf('```') === 0) {
        flushList(); flushTable();
        if (!inCode) { inCode = true; codeBuf = []; }
        else { out.push('<pre><code>' + esc(codeBuf.join('\n')) + '</code></pre>'); inCode = false; }
        continue;
      }
      if (inCode) { codeBuf.push(line); continue; }

      var trimmed = line.trim();
      if (/^\|.+\|$/.test(trimmed)) {
        flushList();
        tableRows.push(trimmed.slice(1, -1).split('|').map(function (c) { return c.trim(); }));
        continue;
      } else {
        flushTable();
      }

      if (!trimmed) { flushList(); continue; }
      if (/^#{1,4}\s+/.test(trimmed)) {
        flushList();
        var lvl = Math.min(4, trimmed.match(/^#+/)[0].length);
        out.push('<h' + lvl + '>' + inlineFmt(trimmed.replace(/^#{1,4}\s+/, '')) + '</h' + lvl + '>');
      } else if (/^>\s?/.test(trimmed)) {
        flushList();
        out.push('<blockquote><p>' + inlineFmt(trimmed.replace(/^>\s?/, '')) + '</p></blockquote>');
      } else if (/^[-*•]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push('<li>' + inlineFmt(trimmed.replace(/^([-*•]|\d+\.)\s+/, '')) + '</li>');
      } else {
        flushList();
        out.push('<p>' + inlineFmt(trimmed) + '</p>');
      }
    }
    flushList(); flushTable();
    if (inCode && codeBuf.length) out.push('<pre><code>' + esc(codeBuf.join('\n')) + '</code></pre>');
    return out.join('');
  }

  function api(url, opts) {
    opts = Object.assign({ credentials: 'same-origin', cache: 'no-store' }, opts || {});
    var method = (opts.method || 'GET').toUpperCase();
    var headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, opts.headers || {});
    if (method !== 'GET' && method !== 'HEAD' && state.account) {
      headers['X-Prism-Account'] = state.account;
    }
    opts.headers = headers;
    return fetch(url, opts);
  }

  function syncBodyLayout() {
    var shell = document.getElementById('futuresCopilotShell');
    var launcher = document.getElementById('futuresCopilotLauncher');
    var fShell = document.getElementById('futuresShell');
    if (!shell || !launcher) return;
    shell.setAttribute('data-copilot-state', state.openState);
    launcher.hidden = state.openState !== 'closed';
    var splitOn = state.openState !== 'closed' && state.split;
    document.body.classList.toggle('copilot-split', splitOn);
    document.body.classList.toggle('copilot-wide', state.openState === 'expanded');
    if (fShell) {
      fShell.classList.toggle('copilot-split', splitOn);
      fShell.classList.toggle('copilot-wide', state.openState === 'expanded');
    }
    try {
      sessionStorage.setItem('prism_copilot_state', state.openState);
    } catch (e) {}
  }

  function maybeAutoWiden(accText) {
    if (state.openState !== 'docked' || state.userWidth > 0 || state.autoWidened) return;
    if (accText.length > 560 || accText.indexOf('| ---') >= 0 || accText.indexOf('|---') >= 0) {
      state.autoWidened = true;
      var vw = window.innerWidth || 1440;
      setCssVar('--copilot-w', Math.min(AUTO_WIDE_COPILOT_W, Math.max(DEFAULT_COPILOT_W, vw - 280)) + 'px');
    }
  }

  function buildFuturesStarters(ctx) {
    if (ctx.symbol) {
      var nm = (ctx.name ? ctx.name + '（' + ctx.symbol + '）' : ctx.symbol);
      return [
        {
          title: '推演 ' + nm + ' 多空情景与失效位',
          sub: '结合近 5 年分位、期现基差、社会库存与我的风控纪律给出倾向',
          prompt: '请结合我的投研画像、私人仓位底牌与风控上限，核查 ' + nm + ' 当前分位、基差结构与库存变化，先给核心结论，再列出多空情景与明确失效条件。',
          mode: 'deep'
        },
        {
          title: '反方挑刺与压力测试',
          sub: '排查最容易忽视的基差背离、仓单异动或换月挤压风险',
          prompt: '请对 ' + nm + ' 做反方压力测试：找出当前逻辑下最容易被忽视的库存、基差或换月反证与证伪观察位。',
          mode: 'compact'
        },
        {
          title: '期股产业链利润传导',
          sub: '评估当前商品价格与盘面利润向 A 股上下游龙头的传导节奏',
          prompt: '请简明分析 ' + nm + ' 当前价格分位与盘面利润向 A 股上下游龙头的顺价或挤压影响。',
          mode: 'compact'
        }
      ];
    }
    return [
      {
        title: '扫描今日分位与利润极值品种',
        sub: '结合我的投研画像筛出处于历史高位或低位极值的核心观察方向',
        prompt: '请结合我的投研画像与风险约束，扫描当前处于历史分位极值或盘面利润极值的重点期货品种，先给核心结论再列出 2–3 个观察方向。',
        mode: 'compact'
      },
      {
        title: '复盘我的跨市场判断胜率',
        sub: '对照历史多空判断对账统计，总结优势环节与偏差来源',
        prompt: '请结合我的历史多空判断对账记录与投研画像，简明总结我近期的判断优势与可改进的纪律点。',
        mode: 'compact'
      }
    ];
  }

  function renderFuturesWelcome() {
    var list = document.getElementById('futuresCopilotMsgs');
    if (!list) return;
    if (list.querySelector('.msg')) return;
    var ctxBar = document.getElementById('futuresCopilotCtxBar');
    if (ctxBar) ctxBar.hidden = true;

    var ctx = state.pageContext || {};
    var desc = ctx.symbol
      ? ((ctx.name || '') + ' ' + ctx.symbol + (ctx.summary ? ' · ' + ctx.summary : '')).trim()
      : '全市场期货极值与对账雷达';
    var starters = buildFuturesStarters(ctx);
    var p = (state.persona && state.persona.profile) || {};
    var arch = p.configured ? (ARCHETYPE_NAMES[p.archetype] || '已定制') : '未定制';
    var hub = window._prismPrivateHubSnapshot || null;
    var pos = (hub && hub.position_state) || {};
    var posText = pos.configured ? ('仓位 ' + pos.position_pct + '%') : '仓位未设';

    list.innerHTML =
      '<div class="fcp-empty cp-welcome">' +
        '<div class="cp-welcome__ctx"><i class="status-dot live"></i><span>已感知视口 · ' + esc(desc) + '</span></div>' +
        '<div class="cp-welcome__mem">' +
          '<div class="cp-welcome__mem-info">' +
            '<span class="cp-mem-kicker">私人记忆</span>' +
            '<span>' + esc(posText) + '</span>' +
            '<span class="cp-mem-sep">·</span>' +
            '<span>画像：' + esc(arch) + '</span>' +
          '</div>' +
          '<div class="cp-welcome__mem-acts">' +
            '<button type="button" id="fcpMemPosBtn">校准底牌</button>' +
            '<button type="button" id="fcpMemPersonaBtn">定制画像</button>' +
          '</div>' +
        '</div>' +
        '<div class="cp-welcome__sec-lbl">针对当前品种的快捷提问</div>' +
        '<div class="cp-starter-list">' +
          starters.map(function (st, idx) {
            return (
              '<button type="button" class="cp-starter-card" data-fcp-starter="' + idx + '">' +
                '<div class="cp-starter-card__body"><strong>' + esc(st.title) + '</strong><span>' + esc(st.sub) + '</span></div>' +
                '<i class="cp-starter-card__arr" aria-hidden="true">→</i>' +
              '</button>'
            );
          }).join('') +
        '</div>' +
      '</div>';

    var posBtn = list.querySelector('#fcpMemPosBtn');
    if (posBtn) posBtn.onclick = function () { if (window.PrismPrivateAgent) window.PrismPrivateAgent.open('position'); };
    var perBtn = list.querySelector('#fcpMemPersonaBtn');
    if (perBtn) perBtn.onclick = openFuturesPersonaDialog;

    list.querySelectorAll('[data-fcp-starter]').forEach(function (btn, idx) {
      btn.onclick = function () {
        var st = starters[idx];
        if (!st) return;
        state.pageContext.response_mode = st.mode || 'compact';
        if (!state.model) {
          var inp = document.getElementById('futuresCopilotInput');
          if (inp) { inp.value = st.prompt; inp.focus(); }
          return;
        }
        openCopilot('docked');
        sendPrompt(st.prompt);
      };
    });
  }

  function renderContextBar() {
    var ctx = state.pageContext || {};
    var sub = document.getElementById('futuresCopilotLauncherSub');
    var label = document.getElementById('futuresCopilotCtxLabel');
    var ctxBar = document.getElementById('futuresCopilotCtxBar');
    var list = document.getElementById('futuresCopilotMsgs');
    var text = ctx.symbol
      ? ('已感知：' + (ctx.name || '') + ' ' + ctx.symbol + (ctx.summary ? ' · ' + ctx.summary : ''))
      : '已感知：全市场期货极值与对账雷达';
    if (sub) sub.textContent = text;
    if (label) label.textContent = text.replace(/^已感知：/, '当前关联：');
    var hasMsgs = !!(list && list.querySelector('.msg'));
    if (ctxBar) ctxBar.hidden = !hasMsgs;
    if (!hasMsgs) renderFuturesWelcome();
  }

  function updatePersonaBadge() {
    var btn = document.getElementById('futuresPersonaBtn');
    var topBtn = document.getElementById('futuresTopPersonaBtn');
    var p = (state.persona && state.persona.profile) || {};
    var arch = ARCHETYPE_NAMES[p.archetype] || '已启用';
    if (btn) btn.textContent = p.configured ? (arch + ' ▾') : '画像 ▾';
    if (topBtn) {
      var sp = topBtn.querySelector('span');
      if (sp) sp.textContent = p.configured ? ('画像 · ' + arch) : '投研画像';
    }
    renderFuturesWelcome();
  }

  function renderProposalBar() {
    var bar = document.getElementById('futuresProposalBar');
    if (!bar) return;
    var list = (state.persona && state.persona.pending_proposals) || [];
    if (!list.length) {
      bar.hidden = true;
      bar.innerHTML = '';
      return;
    }
    var item = list[0];
    bar.hidden = false;
    bar.innerHTML =
      '<div class="copilot-proposal-row">' +
        '<span>画像提议：<b>' + esc(item.trait_text) + '</b></span>' +
        '<span class="copilot-proposal-btns">' +
          '<button type="button" class="primary" data-prop-act="accept">✓ 记入画像</button>' +
          '<button type="button" data-prop-act="dismiss">忽略</button>' +
        '</span>' +
      '</div>';
    bar.querySelectorAll('[data-prop-act]').forEach(function (btn) {
      btn.onclick = async function () {
        var decision = btn.getAttribute('data-prop-act');
        var r = await api('/api/preferences/proposals/' + encodeURIComponent(item.id) + '/decide', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision: decision })
        });
        if (r.ok) await loadPersona();
      };
    });
  }

  async function loadPersona() {
    try {
      var r = await api('/api/preferences/persona');
      if (!r.ok) return;
      state.persona = await r.json();
      var prof = (state.persona && state.persona.profile) || {};
      state.selectedArchetype = prof.archetype || (prof.configured ? 'custom' : 'cycle');
      state.selectedNegativeRules = Array.isArray(prof.negative_rules) ? prof.negative_rules.slice() : [];
      updatePersonaBadge();
      renderProposalBar();
      var dlg = document.getElementById('futuresPersonaDialog');
      if (dlg && dlg.open) renderFuturesPersonaBody();
    } catch (e) {}
  }

  function openFuturesPersonaDialog() {
    var dlg = document.getElementById('futuresPersonaDialog');
    if (!dlg) return;
    if (!dlg.open) dlg.showModal();
    loadPersona().then(renderFuturesPersonaBody);
  }

  function renderFuturesPersonaBody() {
    var mount = document.getElementById('futuresPersonaMount');
    if (!mount) return;
    var data = state.persona || {};
    var prof = data.profile || { configured: false, version: 0 };
    var archetypes = data.archetypes || [
      { id: 'value', name: '价值基本面派', tagline: '重财报现金流、ROE 与估值安全边际' },
      { id: 'cycle', name: '产业周期与供需派', tagline: '重上下游产能瓶颈、期现基差与库存利润传导' },
      { id: 'trend', name: '趋势动量与量化派', tagline: '重均线量价共振、波动率带宽与右侧止损纪律' },
      { id: 'defensive', name: '稳健绝对收益 / 套利派', tagline: '回避高分位追涨，偏好低分位安全垫与期现套利' }
    ];
    var traits = data.traits || [];
    var curArch = state.selectedArchetype || prof.archetype || 'cycle';
    var curStyle = prof.configured ? prof.style : 'balanced';
    var curHorizon = prof.configured ? prof.horizon : 'medium';
    var curPos = prof.configured ? prof.max_position_pct : 20;
    var curDd = prof.configured ? prof.max_drawdown_pct : 12;
    var curLev = prof.configured ? !!prof.allow_leverage : false;
    var curNote = prof.decision_style_note || '';
    var allRules = PRESET_NEGATIVE_RULES.slice();
    state.selectedNegativeRules.forEach(function (r) { if (allRules.indexOf(r) < 0) allRules.push(r); });

    mount.innerHTML =
      '<section class="persona-section persona-tpl-section" style="margin-bottom:14px;">' +
        '<div class="persona-section__hd"><div><span class="persona-step-num">模板</span><h3>一键套用成熟画像模板（新手免手填）</h3></div><span class="persona-section__sub">点选自动填充全套参数与推荐纪律，支持二次微调</span></div>' +
        '<div class="persona-tpl-grid">' +
          PERSONA_TEMPLATES.map(function (tp) {
            return '<button type="button" class="persona-tpl-card' + (state.selectedTemplateId === tp.id ? ' active' : '') + '" data-f-tpl="' + esc(tp.id) + '"><strong>' + esc(tp.name) + '</strong><span>' + esc(tp.fit) + '</span></button>';
          }).join('') +
        '</div>' +
      '</section>' +
      '<form id="futuresPersonaForm" class="persona-sections">' +
        '<section class="persona-section">' +
          '<div class="persona-section__hd"><div><span class="persona-step-num">01</span><h3>主导投资流派</h3></div><span class="persona-section__sub">跨股票与期货全局同步生效</span></div>' +
          '<div class="persona-archetype-grid">' +
            archetypes.map(function (a) {
              return '<button type="button" class="persona-arch-card' + (a.id === curArch ? ' active' : '') + '" data-f-arch="' + esc(a.id) + '">' +
                '<div class="persona-arch-card__top"><strong>' + esc(a.name) + '</strong><em class="persona-arch-check">当前流派</em></div>' +
                '<span>' + esc(a.tagline) + '</span></button>';
            }).join('') +
          '</div>' +
        '</section>' +
        '<section class="persona-section">' +
          '<div class="persona-section__hd"><div><span class="persona-step-num">02</span><h3>风控红线与排雷清单</h3></div></div>' +
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
            '<div class="persona-toggle-copy"><strong>允许讨论期货杠杆、做空或套保对冲策略</strong><span>开启后可结合保证金与基差讨论套保或单边多空情景</span></div>' +
            '<span class="persona-switch"><input name="allow_leverage" type="checkbox"' + (curLev ? ' checked' : '') + '><i></i></span>' +
          '</label>' +
          '<div class="persona-subblock">' +
            '<span class="persona-sublabel">负面回避清单</span>' +
            '<div class="persona-neg-chips">' +
              allRules.map(function (r) {
                var on = state.selectedNegativeRules.indexOf(r) >= 0;
                return '<button type="button" class="persona-neg-chip' + (on ? ' active' : '') + '" data-f-neg="' + esc(r) + '"><i>' + (on ? '✓' : '＋') + '</i>' + esc(r) + '</button>';
              }).join('') +
            '</div>' +
          '</div>' +
          '<label class="persona-field persona-field--wide" style="margin-top:12px;"><span>输出与决策风格备注（选填）</span><input name="decision_style_note" maxlength="300" value="' + esc(curNote) + '" placeholder="例如：先给明确多空倾向与失效价格线，再列核心证据"></label>' +
          '<div class="persona-form-footer">' +
            '<span class="persona-save-hint" id="futuresPersonaHint">' + (prof.configured ? ('当前生效版本 v' + prof.version) : '尚未保存，点击右侧启用') + '</span>' +
            '<button type="submit" class="market-primary-btn">保存画像与风控设置</button>' +
          '</div>' +
        '</section>' +
      '</form>' +
      '<section class="persona-section" style="margin-top:14px;">' +
        '<div class="persona-section__hd"><div><span class="persona-step-num">03</span><h3>个体化投研记忆卡</h3></div><span class="persona-section__sub">已记录 ' + traits.length + ' / 30 条</span></div>' +
        '<div class="persona-trait-list">' +
          (traits.length
            ? traits.map(function (t) {
                return '<div class="persona-trait-item"><span>' + esc(t.trait_text) + '</span><button type="button" class="copilot-icon-btn" data-f-del-trait="' + esc(t.id) + '">×</button></div>';
              }).join('')
            : '<div class="persona-trait-empty">暂无个体化记忆条目。</div>') +
        '</div>' +
        '<form class="persona-trait-add" id="futuresTraitForm"><input id="futuresTraitInp" maxlength="200" autocomplete="off" placeholder="添加长期研究准则，如：做空高贴水品种前必查仓单变化"><button type="submit" class="market-secondary-btn">＋ 添加准则</button></form>' +
      '</section>';

    mount.querySelectorAll('[data-f-tpl]').forEach(function (btn) {
      btn.onclick = function () {
        var tid = btn.getAttribute('data-f-tpl');
        var tp = PERSONA_TEMPLATES.find(function (x) { return x.id === tid; });
        if (!tp) return;
        state.selectedTemplateId = tp.id;
        state.selectedArchetype = tp.archetype;
        state.selectedNegativeRules = tp.negative_rules.slice();
        state.pendingTemplateTraits = (tp.traits || []).slice();
        renderFuturesPersonaBody();
        var f = document.getElementById('futuresPersonaForm');
        if (f) {
          f.elements.style.value = tp.style;
          f.elements.horizon.value = tp.horizon;
          f.elements.max_position_pct.value = tp.max_position_pct;
          f.elements.max_drawdown_pct.value = tp.max_drawdown_pct;
          f.elements.allow_leverage.checked = !!tp.allow_leverage;
          f.elements.decision_style_note.value = tp.note || '';
        }
        var h = document.getElementById('futuresPersonaHint');
        if (h) h.textContent = '已套用「' + tp.name + '」全套参数 · 点击右侧保存立即生效';
      };
    });

    mount.querySelectorAll('[data-f-arch]').forEach(function (btn) {
      btn.onclick = function () {
        state.selectedArchetype = btn.getAttribute('data-f-arch');
        var found = archetypes.find(function (a) { return a.id === state.selectedArchetype; });
        var f = document.getElementById('futuresPersonaForm');
        if (found && found.defaults && f) {
          f.elements.style.value = found.defaults.style;
          f.elements.horizon.value = found.defaults.horizon;
          f.elements.max_position_pct.value = found.defaults.max_position_pct;
          f.elements.max_drawdown_pct.value = found.defaults.max_drawdown_pct;
          f.elements.allow_leverage.checked = !!found.defaults.allow_leverage;
        }
        mount.querySelectorAll('[data-f-arch]').forEach(function (c) {
          c.classList.toggle('active', c.getAttribute('data-f-arch') === state.selectedArchetype);
        });
      };
    });

    mount.querySelectorAll('[data-f-neg]').forEach(function (btn) {
      btn.onclick = function () {
        var r = btn.getAttribute('data-f-neg');
        var idx = state.selectedNegativeRules.indexOf(r);
        if (idx >= 0) state.selectedNegativeRules.splice(idx, 1);
        else state.selectedNegativeRules.push(r);
        renderFuturesPersonaBody();
      };
    });

    mount.querySelectorAll('[data-f-del-trait]').forEach(function (btn) {
      btn.onclick = async function () {
        var id = btn.getAttribute('data-f-del-trait');
        var res = await api('/api/preferences/traits/' + encodeURIComponent(id), { method: 'DELETE' });
        if (res.ok) await loadPersona();
      };
    });

    var pForm = document.getElementById('futuresPersonaForm');
    if (pForm) {
      pForm.onsubmit = async function (ev) {
        ev.preventDefault();
        var hint = document.getElementById('futuresPersonaHint');
        if (hint) hint.textContent = '正在保存…';
        var res = await api('/api/preferences/research', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expected_version: Number(prof.version || 0),
            style: pForm.elements.style.value,
            horizon: pForm.elements.horizon.value,
            max_position_pct: Number(pForm.elements.max_position_pct.value),
            max_drawdown_pct: Number(pForm.elements.max_drawdown_pct.value),
            allow_leverage: !!pForm.elements.allow_leverage.checked,
            archetype: state.selectedArchetype || 'cycle',
            focus_markets: ['ashare', 'futures', 'cross_asset'],
            negative_rules: state.selectedNegativeRules.slice(),
            decision_style_note: String(pForm.elements.decision_style_note.value || '').trim()
          })
        });
        if (res.ok) {
          var pTraits = (state.pendingTemplateTraits || []).slice();
          state.pendingTemplateTraits = [];
          var existT = ((state.persona && state.persona.traits) || []).map(function (t) { return t.trait_text; });
          for (var ti = 0; ti < pTraits.length; ti++) {
            if (existT.indexOf(pTraits[ti]) < 0) {
              await api('/api/preferences/traits', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ trait_text: pTraits[ti], category: 'discipline' })
              }).catch(function () {});
            }
          }
          await loadPersona();
          var h2 = document.getElementById('futuresPersonaHint');
          if (h2) h2.textContent = '✓ 已保存并全局生效';
        }
      };
    }

    var tForm = document.getElementById('futuresTraitForm');
    if (tForm) {
      tForm.onsubmit = async function (ev) {
        ev.preventDefault();
        var inp = document.getElementById('futuresTraitInp');
        var txt = inp ? inp.value.trim() : '';
        if (txt.length < 2) return;
        var res = await api('/api/preferences/traits', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ trait_text: txt, category: 'focus' })
        });
        if (res.ok) await loadPersona();
      };
    }
  }

  async function loadModels() {
    try {
      var r = await api('/api/models');
      if (!r.ok) return;
      var list = await r.json();
      state.models = Array.isArray(list) ? list : [];
      var configured = state.models.filter(function (m) { return m.configured; });
      var def = configured.find(function (m) { return m.default; }) || configured[0];
      state.model = def ? def.id : '';
      var sel = document.getElementById('futuresCopilotModel');
      var sendBtn = document.getElementById('futuresCopilotSend');
      var notice = document.getElementById('futuresModelNotice');
      if (sel) {
        sel.innerHTML = configured.length
          ? configured.map(function (m) {
              return '<option value="' + esc(m.id) + '"' + (m.id === state.model ? ' selected' : '') + '>' + esc(m.name) + '</option>';
            }).join('')
          : '<option value="">未配置模型</option>';
        sel.hidden = !configured.length;
        sel.onchange = function () { state.model = sel.value; };
      }
      if (sendBtn) sendBtn.classList.toggle('is-unconfigured', !configured.length);
      if (notice) {
        notice.hidden = !!configured.length;
        notice.textContent = configured.length ? '' : '当前未激活可用模型 · 请在运维控制台配置服务商密钥后再发起提问';
      }
    } catch (e) {}
  }

  function appendUserBubble(text) {
    var list = document.getElementById('futuresCopilotMsgs');
    if (!list) return;
    var empty = list.querySelector('.fcp-empty');
    if (empty) empty.remove();
    var ctxBar = document.getElementById('futuresCopilotCtxBar');
    if (ctxBar) ctxBar.hidden = false;
    var row = document.createElement('div');
    row.className = 'msg user';
    row.innerHTML = '<div class="msg-inner"><div class="bubble">' + esc(text) + '</div></div>';
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
  }

  function appendAssistantCard(question) {
    var list = document.getElementById('futuresCopilotMsgs');
    if (!list) return null;
    var empty = list.querySelector('.fcp-empty');
    if (empty) empty.remove();
    var row = document.createElement('div');
    row.className = 'msg assistant';
    row.innerHTML =
      '<div class="msg-inner"><div class="msg-card">' +
        '<div class="msg-card__hd"><span class="msg-card__who">PRISM</span>' +
          '<span class="msg-card__acts"><span class="fcp-read-btn" title="居中宽屏沉浸阅读">⤢ 精读</span></span>' +
        '</div>' +
        '<div class="bubble"><div class="fcp-tools-slot"></div><div class="fcp-prose-slot"><span class="muted">正在调取期货与产业链实证数据…</span></div></div>' +
      '</div></div>';
    var readBtn = row.querySelector('.fcp-read-btn');
    if (readBtn) {
      readBtn.onclick = function () {
        var prose = row.querySelector('.bubble');
        if (prose) openFuturesReader(prose.innerHTML, question);
      };
    }
    list.appendChild(row);
    list.scrollTop = list.scrollHeight;
    return row;
  }

  function openFuturesReader(html, titleText) {
    var dlg = document.getElementById('futuresReaderDialog');
    var body = document.getElementById('futuresReaderBody');
    var titleEl = document.getElementById('futuresReaderTitle');
    if (!dlg || !body) return;
    if (titleEl) titleEl.textContent = titleText ? (titleText.length > 42 ? titleText.slice(0, 42) + '…' : titleText) : '投研深度精读视图';
    body.innerHTML = '<article class="reader-prose bubble">' + html + '</article>';
    if (!dlg.open) dlg.showModal();
    body.scrollTop = 0;
  }

  async function ensureConv() {
    if (state.convId) return state.convId;
    var saved = '';
    try { saved = sessionStorage.getItem('prism_active_conv:' + state.account) || ''; } catch (e) {}
    if (saved) {
      var check = await api('/api/convs/' + encodeURIComponent(saved));
      if (check.ok) {
        state.convId = saved;
        return saved;
      }
    }
    var r = await api('/api/convs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skill: 'futures-research' })
    });
    if (!r.ok) throw new Error('无法创建随行会话');
    var data = await r.json();
    state.convId = data.id;
    try { sessionStorage.setItem('prism_active_conv:' + state.account, state.convId); } catch (e) {}
    return state.convId;
  }

  async function sendPrompt(overrideText) {
    if (state.streaming) return;
    var input = document.getElementById('futuresCopilotInput');
    var question = (overrideText != null ? overrideText : (input ? input.value : '')).trim();
    if (!question) return;
    if (!state.model) await loadModels();
    if (!state.model) {
      var fallbackCard = appendAssistantCard(question);
      if (fallbackCard) fallbackCard.querySelector('.fcp-prose-slot').textContent = '当前尚未配置可用研究模型，请先检查模型路由。';
      return;
    }
    if (input && overrideText == null) input.value = '';
    state.lastQuestion = question;
    appendUserBubble(question);
    var cardRow = appendAssistantCard(question);
    var toolsSlot = cardRow.querySelector('.fcp-tools-slot');
    var proseSlot = cardRow.querySelector('.fcp-prose-slot');
    var msgList = document.getElementById('futuresCopilotMsgs');
    var sendBtn = document.getElementById('futuresCopilotSend');
    var launcher = document.getElementById('futuresCopilotLauncher');
    state.streaming = true;
    if (sendBtn) sendBtn.classList.add('stop-mode');
    if (launcher) launcher.classList.add('is-running');

    try {
      var cid = await ensureConv();
      state.abortCtrl = new AbortController();
      var reqId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('fcp-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10));
      var res = await api('/api/agent/chat/stream', {
        method: 'POST',
        signal: state.abortCtrl.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          conv_id: cid,
          request_id: reqId,
          question: question,
          model: state.model,
          skill: 'futures-research',
          workflow: 'futures',
          page_context: state.pageContext
        })
      });
      if (!res.ok || !res.body) {
        var errBody = await res.json().catch(function () { return {}; });
        proseSlot.textContent = errBody.error || errBody.detail || ('请求未完成 (' + res.status + ')');
        return;
      }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = '';
      var accText = '';
      var toolNames = [];

      function updateToolsFold(running) {
        if (!toolNames.length || !toolsSlot) return;
        var wasOpen = !!(toolsSlot.querySelector('details') && toolsSlot.querySelector('details').open);
        var lead = running ? ('正在调用：' + toolNames[toolNames.length - 1]) : ('平台能力调用 · ' + toolNames.length + ' 项');
        var preview = !running ? toolNames.slice(0, 3).join(' · ') : '';
        toolsSlot.innerHTML =
          '<details class="platform-call-fold' + (running ? ' is-running' : '') + '"' + (wasOpen ? ' open' : '') + '>' +
            '<summary><span class="platform-call-fold__lead">' + esc(lead) + '</span>' +
              (preview ? '<span class="platform-call-fold__preview">' + esc(preview) + '</span>' : '') +
              '<span class="platform-call-fold__toggle">明细</span></summary>' +
            '<div class="platform-call-fold__body">' +
              toolNames.map(function (t) {
                return '<div class="platform-call-mini-row"><span class="platform-call-status completed">已调用</span><strong>' + esc(t) + '</strong></div>';
              }).join('') +
            '</div>' +
          '</details>';
      }

      while (true) {
        var step = await reader.read();
        if (step.done) break;
        buffer += decoder.decode(step.value, { stream: true });
        var lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line.startsWith('data:')) continue;
          var raw = line.slice(5).trim();
          if (!raw || raw === '[DONE]') continue;
          try {
            var ev = JSON.parse(raw);
            if (ev.tool) {
              if (toolNames.indexOf(ev.tool) < 0) toolNames.push(ev.tool);
              updateToolsFold(!accText);
              if (ev.tool === 'persona_propose_trait') setTimeout(loadPersona, 600);
            }
            if (typeof ev.text === 'string' && ev.text) {
              accText += ev.text;
              updateToolsFold(false);
              proseSlot.innerHTML = renderSimpleMarkdown(accText);
              state.lastHtml = cardRow.querySelector('.bubble').innerHTML;
              maybeAutoWiden(accText);
              if (msgList) msgList.scrollTop = msgList.scrollHeight;
            } else if (typeof ev.error === 'string' && ev.error) {
              proseSlot.innerHTML = renderSimpleMarkdown(accText) + '<p class="research-status-notice">提示：' + esc(ev.error) + '</p>';
            }
          } catch (e) {}
        }
      }
      updateToolsFold(false);
      if (!accText) {
        proseSlot.textContent = '已完成工具核查，可展开上方明细或继续追问。';
      }
      state.lastHtml = cardRow.querySelector('.bubble').innerHTML;
      await loadPersona();
    } catch (err) {
      if (err && err.name === 'AbortError') {
        proseSlot.innerHTML += '<p class="muted">[已停止本轮推演]</p>';
      } else {
        proseSlot.textContent = '连接中断：' + (err && err.message ? err.message : '请重试');
      }
    } finally {
      state.streaming = false;
      state.abortCtrl = null;
      state.pageContext.response_mode = 'compact';
      if (sendBtn) sendBtn.classList.remove('stop-mode');
      if (launcher) launcher.classList.remove('is-running');
    }
  }

  function openCopilot(nextState) {
    state.openState = nextState || 'docked';
    syncBodyLayout();
    var inp = document.getElementById('futuresCopilotInput');
    if (inp) setTimeout(function () { inp.focus(); }, 60);
  }

  function closeCopilot() {
    state.openState = 'closed';
    syncBodyLayout();
  }

  function wireShellChrome() {
    var fShell = document.getElementById('futuresShell');
    var railBtn = document.getElementById('futuresRailCollapseBtn');
    var themeBtn = document.getElementById('futuresThemeBtn');
    var topPersonaBtn = document.getElementById('futuresTopPersonaBtn');

    try {
      if (fShell && localStorage.getItem('prism-rail-collapsed') === '1') {
        fShell.classList.add('rail-collapsed');
      }
      var savedW = parseInt(localStorage.getItem('prism-copilot-width') || '', 10);
      if (savedW >= MIN_COPILOT_W && savedW <= MAX_COPILOT_W) {
        state.userWidth = savedW;
        setCssVar('--copilot-w', savedW + 'px');
      } else {
        setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
      }
    } catch (e) {
      setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
    }

    if (railBtn && fShell) {
      railBtn.onclick = function () {
        var collapsed = fShell.classList.toggle('rail-collapsed');
        try { localStorage.setItem('prism-rail-collapsed', collapsed ? '1' : '0'); } catch (e) {}
      };
    }

    if (themeBtn) {
      themeBtn.onclick = function () {
        var isDark = document.documentElement.classList.contains('dark') || document.documentElement.getAttribute('data-theme') === 'dark';
        var next = isDark ? 'light' : 'dark';
        document.documentElement.className = next;
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('cl-theme', next); } catch (e) {}
      };
    }

    if (topPersonaBtn) {
      topPersonaBtn.onclick = openFuturesPersonaDialog;
    }
    var menuPersonaBtn = document.getElementById('futuresMenuPersonaBtn');
    if (menuPersonaBtn) {
      menuPersonaBtn.addEventListener('click', function () {
        var menu = document.getElementById('futuresAccountMenu');
        if (menu) menu.hidden = true;
        openFuturesPersonaDialog();
      });
    }
  }

  function bindFuturesResizer(handle) {
    if (!handle) return;
    var dragging = false;
    handle.addEventListener('dblclick', function (ev) {
      ev.preventDefault();
      state.userWidth = 0;
      state.autoWidened = false;
      try { localStorage.removeItem('prism-copilot-width'); } catch (e) {}
      setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
      if (state.openState === 'expanded') openCopilot('docked');
    });
    handle.addEventListener('pointerdown', function (ev) {
      if (ev.button !== 0) return;
      ev.preventDefault();
      dragging = true;
      if (handle.setPointerCapture) { try { handle.setPointerCapture(ev.pointerId); } catch (e) {} }
      document.body.classList.add('copilot-resizing');
      if (state.openState === 'expanded') {
        state.openState = 'docked';
        syncBodyLayout();
      }
    });
    handle.addEventListener('pointermove', function (ev) {
      if (!dragging) return;
      var vw = window.innerWidth || 1440;
      var maxAllowed = Math.min(MAX_COPILOT_W, Math.max(MIN_COPILOT_W, vw - 240));
      var nextW = Math.round(Math.max(MIN_COPILOT_W, Math.min(maxAllowed, vw - ev.clientX)));
      state.userWidth = nextW;
      state.autoWidened = false;
      setCssVar('--copilot-w', nextW + 'px');
    });
    function endDrag(ev) {
      if (!dragging) return;
      dragging = false;
      if (handle.releasePointerCapture && ev && ev.pointerId != null) { try { handle.releasePointerCapture(ev.pointerId); } catch (e) {} }
      document.body.classList.remove('copilot-resizing');
      if (state.userWidth >= MIN_COPILOT_W) {
        try { localStorage.setItem('prism-copilot-width', String(state.userWidth)); } catch (e) {}
      }
    }
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);
  }

  function mountUI() {
    wireShellChrome();
    var content = document.getElementById('futures-content');
    state.account = (content && content.getAttribute('data-account')) || '';

    var cssLink = document.querySelector('link[href*="copilot.css?v="]');
    var vMatch = cssLink && cssLink.getAttribute('href') ? cssLink.getAttribute('href').match(/\?v=([^&#]+)/) : null;
    var agentMarkSrc = '/static/agent-mark.svg' + (vMatch ? '?v=' + vMatch[1] : '');

    var launcher = document.createElement('button');
    launcher.type = 'button';
    launcher.id = 'futuresCopilotLauncher';
    launcher.className = 'copilot-launcher';
    launcher.innerHTML =
      '<span class="copilot-launcher__spark"><img src="' + agentMarkSrc + '" width="30" height="30" alt="" aria-hidden="true"></span>' +
      '<span class="copilot-launcher__meta">' +
        '<span>随行投研 Agent</span>' +
        '<span class="copilot-launcher__sub" id="futuresCopilotLauncherSub">感知当前期货盘面…</span>' +
      '</span>' +
      '<span class="copilot-launcher__kbd">⌘K</span>';
    launcher.onclick = function () { openCopilot('docked'); };

    var shell = document.createElement('aside');
    shell.id = 'futuresCopilotShell';
    shell.className = 'copilot-shell';
    shell.setAttribute('data-copilot-state', 'closed');
    shell.setAttribute('aria-label', '随行投研 Agent');
    shell.innerHTML =
      '<div class="copilot-resizer" id="futuresCopilotResizer" role="separator" aria-orientation="vertical" title="左右拖拽调整 Agent 窗口宽度 · 双击恢复默认"></div>' +
      '<div class="copilot-hd">' +
        '<div class="copilot-hd__left">' +
          '<span class="copilot-brand"><img src="' + agentMarkSrc + '" class="copilot-brand__mark" width="19" height="19" alt="" aria-hidden="true">Prism Agent</span>' +
          '<button type="button" class="copilot-pill-btn" id="futuresPrivateBtn" title="管理私人仓位底牌与盯盘守望">底牌</button>' +
          '<button type="button" class="copilot-pill-btn" id="futuresPersonaBtn" title="配置我的投研画像与风控约束">画像 ▾</button>' +
        '</div>' +
        '<div class="copilot-hd__right">' +
          '<button type="button" class="copilot-icon-btn" id="futuresNewConvBtn" title="新建会话">＋</button>' +
          '<button type="button" class="copilot-icon-btn" id="futuresWideBtn" title="宽屏/标准切换">⇔</button>' +
          '<button type="button" class="copilot-icon-btn" id="futuresCloseCopilot" title="收起 (⌘K)">✕</button>' +
        '</div>' +
      '</div>' +
      '<div class="copilot-ctx-bar" id="futuresCopilotCtxBar" hidden>' +
        '<div class="copilot-ctx-top">' +
          '<span class="copilot-ctx-label" id="futuresCopilotCtxLabel">当前关联：期货研究</span>' +
          '<button type="button" class="cp-ctx-reset" id="futuresCtxResetBtn">新话题</button>' +
        '</div>' +
      '</div>' +
      '<div class="copilot-proposal-bar" id="futuresProposalBar" hidden></div>' +
      '<div class="msgs-wrap"><div id="futuresCopilotMsgs" class="msgs"></div></div>' +
      '<div class="input-zone">' +
        '<div id="futuresModelNotice" class="research-status-notice cp-model-notice" role="status" hidden></div>' +
        '<div class="composer">' +
          '<textarea id="futuresCopilotInput" rows="2" placeholder="输入当前品种问题，如：目前基差结构适合正套还是单边？"></textarea>' +
          '<div class="composer-bar">' +
            '<div class="composer-bar__left">' +
              '<select id="futuresCopilotModel" hidden></select>' +
            '</div>' +
            '<div class="composer-bar__right">' +
              '<button type="button" id="futuresCopilotSend" class="cmp-send" title="发送 (Enter)">' +
                '<span>发送</span>' +
                '<svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 13V3M3.5 7.5L8 3l4.5 4.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>' +
              '</button>' +
            '</div>' +
          '</div>' +
        '</div>' +
      '</div>';

    var personaDlg = document.createElement('dialog');
    personaDlg.id = 'futuresPersonaDialog';
    personaDlg.className = 'persona-dialog';
    personaDlg.innerHTML =
      '<header class="persona-dialog__hd">' +
        '<div class="persona-dialog__hd-copy">' +
          '<span class="persona-dialog__kicker">PERSONALIZED RESEARCH PERSONA</span>' +
          '<h2>我的独立投研画像与风控纪律</h2>' +
          '<p>按账号独立隔离 · 在股票与期货全工作台实时同步生效</p>' +
        '</div>' +
        '<button type="button" class="copilot-icon-btn" id="futuresPersonaClose" aria-label="关闭">✕</button>' +
      '</header>' +
      '<div class="persona-dialog__body"><div id="futuresPersonaMount"></div></div>';

    var readerDlg = document.createElement('dialog');
    readerDlg.id = 'futuresReaderDialog';
    readerDlg.className = 'persona-dialog reader-dialog';
    readerDlg.innerHTML =
      '<header class="persona-dialog__hd reader-dialog__hd">' +
        '<div class="persona-dialog__hd-copy">' +
          '<span class="persona-dialog__kicker">IMMERSIVE RESEARCH READER</span>' +
          '<h2 id="futuresReaderTitle">投研深度精读视图</h2>' +
        '</div>' +
        '<button type="button" class="copilot-icon-btn" id="futuresReaderClose" aria-label="关闭">✕</button>' +
      '</header>' +
      '<div class="reader-dialog__body" id="futuresReaderBody"></div>';

    document.body.append(launcher, shell, personaDlg, readerDlg);

    personaDlg.onclick = function (ev) { if (ev.target === personaDlg) personaDlg.close(); };
    readerDlg.onclick = function (ev) { if (ev.target === readerDlg) readerDlg.close(); };
    document.getElementById('futuresPersonaClose').onclick = function () { personaDlg.close(); };
    document.getElementById('futuresReaderClose').onclick = function () { readerDlg.close(); };
    document.getElementById('futuresPersonaBtn').onclick = openFuturesPersonaDialog;
    var fPrivBtn = document.getElementById('futuresPrivateBtn');
    if (fPrivBtn) {
      fPrivBtn.onclick = function () {
        if (window.PrismPrivateAgent) window.PrismPrivateAgent.open('position');
      };
    }

    bindFuturesResizer(document.getElementById('futuresCopilotResizer'));

    document.getElementById('futuresCloseCopilot').onclick = closeCopilot;
    document.getElementById('futuresWideBtn').onclick = function () {
      if (state.openState === 'expanded') {
        state.autoWidened = false;
        if (!state.userWidth) setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
        openCopilot('docked');
      } else {
        openCopilot('expanded');
      }
    };
    function resetFuturesConv() {
      state.convId = '';
      state.lastHtml = '';
      if (state.autoWidened && !state.userWidth) {
        state.autoWidened = false;
        setCssVar('--copilot-w', DEFAULT_COPILOT_W + 'px');
      }
      try { sessionStorage.removeItem('prism_active_conv:' + state.account); } catch (e) {}
      var list = document.getElementById('futuresCopilotMsgs');
      if (list) list.innerHTML = '';
      renderFuturesWelcome();
    }
    document.getElementById('futuresNewConvBtn').onclick = resetFuturesConv;
    var fResetBtn = document.getElementById('futuresCtxResetBtn');
    if (fResetBtn) fResetBtn.onclick = resetFuturesConv;
    document.getElementById('futuresCopilotSend').onclick = function () {
      if (state.streaming && state.abortCtrl) state.abortCtrl.abort();
      else sendPrompt();
    };
    var inp = document.getElementById('futuresCopilotInput');
    inp.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        sendPrompt();
      }
    });
    document.addEventListener('keydown', function (ev) {
      if ((ev.metaKey || ev.ctrlKey) && String(ev.key || '').toLowerCase() === 'k') {
        ev.preventDefault();
        if (state.openState === 'closed') openCopilot('docked');
        else closeCopilot();
      }
    });

    api('/api/me').then(function (r) { return r.ok ? r.json() : null; }).then(function (me) {
      if (me && me.user) state.account = me.user;
      loadPersona();
      loadModels();
      try {
        var savedOpen = sessionStorage.getItem('prism_copilot_state');
        if (savedOpen === 'docked' || savedOpen === 'expanded') openCopilot(savedOpen);
      } catch (e) {}
    }).catch(function () {});

    renderContextBar();
  }

  window.PrismFuturesCopilot = {
    open: openCopilot,
    close: closeCopilot,
    openPersona: openFuturesPersonaDialog,
    intervene: function (promptText) {
      openCopilot('docked');
      sendPrompt(promptText);
    },
    setContext: function (ctx) {
      state.pageContext = Object.assign({ surface: 'futures_detail', response_mode: 'compact' }, ctx || {});
      renderContextBar();
    },
    setModel: function (mid) {
      if (mid) state.model = mid;
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mountUI);
  } else {
    mountUI();
  }
}());
