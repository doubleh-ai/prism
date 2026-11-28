/* ═══════════════════════════════════════════════════════════════════════
   Prism Circle Intelligence Pool & Private Personal Agent Hub
   - window.PrismIntel: Zero-friction street chatter pool + market reality check
   - window.PrismPrivateAgent: 3-tab private cockpit (Position State, Watchman, Personal Push)
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function escAttr(s) {
    return esc(s).replace(/'/g, '&#39;');
  }
  function toast(msg) {
    if (window.App && typeof window.App._toast === 'function') {
      window.App._toast(msg);
      return;
    }
    var el = document.getElementById('prismIntelToast');
    if (!el && document.body) {
      el = document.createElement('div');
      el.id = 'prismIntelToast';
      el.className = 'radar-toast';
      document.body.appendChild(el);
    }
    if (!el) return;
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.remove('show'); }, 2800);
  }

  async function api(url, opts) {
    if (window.App && typeof window.App.api === 'function') {
      return window.App.api(url, opts);
    }
    var o = Object.assign({ credentials: 'same-origin' }, opts || {});
    o.headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, o.headers || {});
    return fetch(url, o);
  }

  function fmtTime(ts) {
    if (!ts) return '';
    try {
      var d = new Date(Number(ts) * 1000);
      var mm = String(d.getMonth() + 1).padStart(2, '0');
      var dd = String(d.getDate()).padStart(2, '0');
      var hh = String(d.getHours()).padStart(2, '0');
      var mi = String(d.getMinutes()).padStart(2, '0');
      return mm + '-' + dd + ' ' + hh + ':' + mi;
    } catch (e) {
      return '';
    }
  }

  var ICONS = {
    shield: '<svg class="ui-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3l7 3v6c0 4.6-3 8.4-7 9.8C8 20.4 5 16.6 5 12V6l7-3z"/><path d="M9.5 12l1.8 1.8 3.4-3.6"/></svg>',
    pulse: '<svg class="ui-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2.4-5 4.2 10 2.4-5H21"/></svg>',
    bell: '<svg class="ui-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9a6 6 0 0 1 12 0c0 5 2 6.5 2 6.5H4S6 14 6 9"/><path d="M10 19a2 2 0 0 0 4 0"/></svg>',
    lock: '<svg class="ui-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    nodes: '<svg class="ui-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6.5" cy="12" r="2.2"/><circle cx="17.5" cy="6.5" r="2.2"/><circle cx="17.5" cy="17.5" r="2.2"/><path d="M8.5 11l7-3.5M8.5 13l7 3.5"/></svg>'
  };
  function ico(name) {
    return ICONS[name] || '';
  }

  /* ───────────────────────────────────────────────────────────
     PART 1: Circle Intelligence Pool (window.PrismIntel)
     ─────────────────────────────────────────────────────────── */
  var intelState = {
    scope: 'active',
    category: '',
    query: '',
    items: [],
    counts: { active: 0, private_mine: 0 },
    loading: false,
    submitting: false,
    draftVis: 'circle',
    draftAnon: false,
    draftTtl: ''
  };

  var SAMPLE_CHATTER =
    '现在是政策预期拉动的快速反弹，不适合一把重仓追涨。如果手里已有底仓、风险承受力高，可以小仓位分批加，优先选业绩兑现的主线龙头或者 ETF；空仓的不急着冲，等后面看量能能不能稳住、细则落地情况，再找回调机会；如果本身仓位已经很重，反而要留意冲高分化风险，适当降低仓位（供参考）。';

  function renderIntelShell() {
    var root = document.getElementById('intelPanelInner');
    if (!root) return;
    if (!root.querySelector('#intelStudioForm')) {
      root.innerHTML =
        '<div class="intel-page">' +
          '<section class="intel-composer-card">' +
            '<div class="intel-composer-hd">' +
              '<div>' +
                '<h2>圈内情报与策略共享池</h2>' +
                '<p>原封不动粘贴微信群消息、调研线索或盘中应对思路——系统自动提炼条件分支剧本、交叉核验实时盘口并设置保鲜期，随行 Agent 会自动结合你的私人仓位底牌对号入座。</p>' +
              '</div>' +
              '<button type="button" class="market-qc-btn-secondary" id="intelFillSampleBtn">填入示例体验</button>' +
            '</div>' +
            '<form id="intelStudioForm">' +
              '<textarea id="intelRawInput" class="intel-textarea" rows="3" maxlength="4000" placeholder="直接粘贴群聊消息、产业传闻或交易应对策略（无需手动填标题或股票代码）…"></textarea>' +
              '<div class="intel-composer-ft">' +
                '<div class="intel-composer-opts">' +
                  '<div class="intel-vis-seg" role="group" aria-label="可见范围">' +
                    '<button type="button" class="active" data-vis="circle">' + ico('nodes') + '圈内共享</button>' +
                    '<button type="button" data-vis="private">' + ico('lock') + '仅自己可见</button>' +
                  '</div>' +
                  '<label class="intel-anon-chk" id="intelAnonWrap">' +
                    '<input type="checkbox" id="intelAnonChk"> <span>匿名分享</span>' +
                  '</label>' +
                  '<select id="intelTtlSel" class="intel-ttl-select" aria-label="保鲜期">' +
                    '<option value="">智能保鲜期（短线5天 / 调研30天）</option>' +
                    '<option value="3">3 天（盘中快反/短线预期）</option>' +
                    '<option value="7">7 天（周度催化/传闻核验）</option>' +
                    '<option value="30">30 天（产业调研/订单逻辑）</option>' +
                  '</select>' +
                '</div>' +
                '<button type="submit" class="market-primary-btn" id="intelSubmitBtn">结构化入库并体检盘口</button>' +
              '</div>' +
            '</form>' +
          '</section>' +
          '<div class="intel-toolbar">' +
            '<div class="intel-tabs" id="intelFilterTabs">' +
              '<button type="button" class="active" data-scope="active" data-cat="">鲜活有效 <span id="intelCntActive"></span></button>' +
              '<button type="button" data-scope="active" data-cat="strategy">策略剧本</button>' +
              '<button type="button" data-scope="active" data-cat="rumor">产业线索</button>' +
              '<button type="button" data-scope="active" data-cat="macro">宏观政策</button>' +
              '<button type="button" data-scope="active" data-cat="channel">调研纪要</button>' +
              '<button type="button" data-scope="private" data-cat="">我的私密 <span id="intelCntPriv"></span></button>' +
              '<button type="button" data-scope="expired" data-cat="">已到期 / 对账归档</button>' +
            '</div>' +
            '<div class="intel-search-box">' +
              '<input type="search" id="intelSearchInput" placeholder="搜代码、板块或关键词…" autocomplete="off">' +
            '</div>' +
          '</div>' +
          '<div id="intelFeedList" class="intel-feed-list"></div>' +
        '</div>';

      wireIntelEvents(root);
    }
    renderIntelFeedList();
  }

  function wireIntelEvents(root) {
    var sampleBtn = root.querySelector('#intelFillSampleBtn');
    var rawInp = root.querySelector('#intelRawInput');
    if (sampleBtn && rawInp) {
      sampleBtn.onclick = function () {
        rawInp.value = SAMPLE_CHATTER;
        rawInp.focus();
      };
    }

    root.querySelectorAll('.intel-vis-seg button').forEach(function (btn) {
      btn.onclick = function () {
        intelState.draftVis = btn.getAttribute('data-vis') || 'circle';
        root.querySelectorAll('.intel-vis-seg button').forEach(function (b) {
          b.classList.toggle('active', b === btn);
        });
        var anonWrap = root.querySelector('#intelAnonWrap');
        if (anonWrap) anonWrap.style.display = intelState.draftVis === 'private' ? 'none' : 'inline-flex';
      };
    });

    var form = root.querySelector('#intelStudioForm');
    if (form) {
      form.onsubmit = async function (ev) {
        ev.preventDefault();
        if (intelState.submitting) return;
        var text = (rawInp.value || '').trim();
        if (text.length < 4) {
          toast('请先粘贴至少一句完整的情报或观点');
          return;
        }
        intelState.submitting = true;
        var subBtn = root.querySelector('#intelSubmitBtn');
        if (subBtn) { subBtn.disabled = true; subBtn.textContent = '正在拆解并核验盘面…'; }
        try {
          var ttlVal = (root.querySelector('#intelTtlSel') || {}).value || '';
          var anonChk = root.querySelector('#intelAnonChk');
          var r = await api('/api/intel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              raw_text: text,
              visibility: intelState.draftVis,
              anonymous: intelState.draftVis === 'circle' && !!(anonChk && anonChk.checked),
              ttl_days: ttlVal ? Number(ttlVal) : null
            })
          });
          var data = await r.json().catch(function () { return {}; });
          if (!r.ok) {
            toast(data.detail || '提交失败，请重试');
            return;
          }
          rawInp.value = '';
          toast('✓ 已结构化入库并完成盘口交叉体检');
          await loadIntelFeed();
        } catch (e) {
          toast('网络异常，未能入库');
        } finally {
          intelState.submitting = false;
          if (subBtn) { subBtn.disabled = false; subBtn.textContent = '结构化入库并体检盘口'; }
        }
      };
    }

    root.querySelectorAll('#intelFilterTabs button').forEach(function (tb) {
      tb.onclick = function () {
        intelState.scope = tb.getAttribute('data-scope') || 'active';
        intelState.category = tb.getAttribute('data-cat') || '';
        root.querySelectorAll('#intelFilterTabs button').forEach(function (b) {
          b.classList.toggle('active', b === tb);
        });
        loadIntelFeed();
      };
    });

    var sInp = root.querySelector('#intelSearchInput');
    if (sInp) {
      var deb = null;
      sInp.oninput = function () {
        clearTimeout(deb);
        deb = setTimeout(function () {
          intelState.query = sInp.value.trim();
          loadIntelFeed();
        }, 260);
      };
    }
  }

  async function loadIntelFeed() {
    intelState.loading = true;
    renderIntelFeedList();
    try {
      var qs =
        'scope=' + encodeURIComponent(intelState.scope) +
        '&category=' + encodeURIComponent(intelState.category) +
        '&q=' + encodeURIComponent(intelState.query);
      var r = await api('/api/intel/feed?' + qs);
      if (!r.ok) throw new Error('failed');
      var data = await r.json();
      intelState.items = data.items || [];
      intelState.counts = data.counts || { active: 0, private_mine: 0 };
    } catch (e) {
      toast('读取圈内情报失败');
    } finally {
      intelState.loading = false;
      renderIntelFeedList();
    }
  }

  function renderIntelFeedList() {
    var listEl = document.getElementById('intelFeedList');
    if (!listEl) return;
    var cAct = document.getElementById('intelCntActive');
    var cPriv = document.getElementById('intelCntPriv');
    if (cAct) cAct.textContent = intelState.counts.active ? '(' + intelState.counts.active + ')' : '';
    if (cPriv) cPriv.textContent = intelState.counts.private_mine ? '(' + intelState.counts.private_mine + ')' : '';

    if (intelState.loading && !intelState.items.length) {
      listEl.innerHTML = '<div class="market-empty-state">正在读取鲜活情报与盘口体检…</div>';
      return;
    }
    if (!intelState.items.length) {
      listEl.innerHTML =
        '<div class="market-empty-state">' +
          '当前筛选下暂无情报记录。<br>在上方粘贴微信群里的策略观点或产业消息，或点右上角「填入示例体验」立即试试！' +
        '</div>';
      return;
    }

    listEl.innerHTML = intelState.items.map(function (it) {
      var mc = it.market_check || {};
      var mcCls = mc.verdict === 'warn_heated' ? 'warn' : (mc.verdict === 'calm' ? 'ok' : 'neutral');
      var cred = it.author_credibility || {};
      var credTxt = cred.hit_rate_pct != null ? (' · 历史印证 ' + cred.hit_rate_pct + '%') : '';
      var visBadge = it.visibility === 'private'
        ? '<span class="intel-pill priv">' + ico('lock') + '仅自己可见</span>'
        : '<span class="intel-pill author">分享者 · ' + esc(it.display_author + credTxt) + '</span>';
      var ttlTxt = it.is_expired
        ? '<span class="intel-ttl expired">已到期归档</span>'
        : '<span class="intel-ttl">保鲜剩 ' + esc(it.remaining_days) + ' 天</span>';

      var tagsHtml = '';
      (it.sectors || []).forEach(function (s) {
        tagsHtml += '<span class="intel-tag sector">' + esc(s) + '</span>';
      });
      (it.tickers || []).forEach(function (tk) {
        if (/^\d{6}$/.test(tk)) {
          tagsHtml += '<button type="button" class="intel-tag ticker" onclick="window.PrismIntel.openTicker(\'' + escAttr(tk) + '\')">' + esc(tk) + ' ↗</button>';
        } else {
          tagsHtml += '<a class="intel-tag ticker" href="/futures?symbol=' + escAttr(tk) + '">' + esc(tk) + ' ↗</a>';
        }
      });

      var branchesHtml = '';
      if (it.branches && it.branches.length) {
        branchesHtml =
          '<div class="intel-branches">' +
            it.branches.map(function (b) {
              return '<div class="intel-branch-item"><b>' + esc(b.audience) + '</b><span>' + esc(b.action) + '</span></div>';
            }).join('') +
          '</div>';
      }

      var verifyHtml = (it.verify_signals && it.verify_signals.length)
        ? '<div class="intel-verify-line"><b>后续核验路标：</b>' + esc(it.verify_signals.join(' ｜ ')) + '</div>'
        : '';

      var recMap = {
        supported: ['ok', '✓ 行情对账：已印证'],
        contradicted: ['bad', '✕ 行情对账：已证伪'],
        expired_neutral: ['neutral', '◎ 行情对账：中性结案']
      };
      var recHtml = '';
      if (it.reconcile_status && it.reconcile_status !== 'pending' && recMap[it.reconcile_status]) {
        var rm = recMap[it.reconcile_status];
        recHtml = '<div class="intel-reconcile-bar ' + rm[0] + '"><b>' + rm[1] + '</b> ' + esc(it.reconcile_summary || '') + '</div>';
      }

      return (
        '<article class="intel-card">' +
          '<div class="intel-card__hd">' +
            '<div class="intel-card__meta">' +
              '<span class="intel-pill cat">' + esc(it.category_label) + '</span>' +
              visBadge +
              (mc.badge ? '<span class="intel-pill mcheck ' + mcCls + '" title="' + escAttr(mc.summary || '') + '">' + esc(mc.badge) + '</span>' : '') +
            '</div>' +
            '<div class="intel-card__right">' + ttlTxt + '<span>' + esc(fmtTime(it.created_at)) + '</span></div>' +
          '</div>' +
          '<h3 class="intel-card__title">' + esc(it.headline) + '</h3>' +
          (tagsHtml ? '<div class="intel-tags-row">' + tagsHtml + '</div>' : '') +
          branchesHtml +
          '<div class="intel-raw-box">' + esc(it.raw_text) + '</div>' +
          verifyHtml +
          recHtml +
          '<div class="intel-card__ft">' +
            '<div class="intel-actions-left">' +
              (it.visibility === 'circle'
                ? '<button type="button" class="market-qc-btn-secondary' + (it.endorsed_by_me ? ' active-endorse' : '') + '" onclick="window.PrismIntel.endorse(\'' + escAttr(it.id) + '\')">' + (it.endorsed_by_me ? '✓ 已认同参考' : '有参考价值') + (it.endorse_count ? ' · ' + it.endorse_count : '') + '</button>'
                : '') +
              '<button type="button" class="copilot-trigger-chip" onclick="window.PrismIntel.askPersonalAgent(\'' + escAttr(it.id) + '\')">结合我的仓位底牌推演</button>' +
            '</div>' +
            '<div class="intel-actions-right">' +
              '<button type="button" class="market-qc-btn-secondary" onclick="window.PrismIntel.reconcile(\'' + escAttr(it.id) + '\')">按最新行情对账</button>' +
              (it.can_delete
                ? '<button type="button" class="market-qc-btn-secondary" onclick="window.PrismIntel.remove(\'' + escAttr(it.id) + '\')">删除</button>'
                : '') +
            '</div>' +
          '</div>' +
        '</article>'
      );
    }).join('');
  }

  window.PrismIntel = {
    renderWorkspace: function () {
      renderIntelShell();
      loadIntelFeed();
    },
    openTicker: function (code) {
      if (window.App && typeof window.App.switchMode === 'function') {
        window.App._marketWorkspaceTab = 'quote';
        window.App.switchMode('market');
        if (window.App._selectStock) window.App._selectStock(code);
      }
    },
    endorse: async function (id) {
      try {
        var r = await api('/api/intel/' + encodeURIComponent(id) + '/endorse', { method: 'POST' });
        if (r.ok) await loadIntelFeed();
      } catch (e) {}
    },
    reconcile: async function (id) {
      try {
        var r = await api('/api/intel/' + encodeURIComponent(id) + '/reconcile', { method: 'POST' });
        var d = await r.json().catch(function () { return {}; });
        if (!r.ok) {
          toast(d.detail || '暂无法获取对账报价');
          return;
        }
        toast('✓ 已按最新行情完成实盘对账');
        await loadIntelFeed();
      } catch (e) {}
    },
    remove: async function (id) {
      if (!window.confirm('确定删除这条情报记录吗？')) return;
      var r = await api('/api/intel/' + encodeURIComponent(id), { method: 'DELETE' });
      if (r.ok) {
        toast('已移除情报记录');
        await loadIntelFeed();
      }
    },
    askPersonalAgent: function (id) {
      var item = intelState.items.find(function (x) { return x.id === id; });
      if (!item || !window.App || typeof window.App.intervene !== 'function') return;
      var prompt =
        '请结合这条圈内情报与我当前的私人仓位底牌、历史复盘胜率，帮我对号入座给出具体操作建议，并附上反方证伪视角与观察位：\n' +
        '「' + item.raw_text.slice(0, 500) + '」';
      window.App.intervene(prompt, 'prism-research');
    }
  };

  /* ───────────────────────────────────────────────────────────
     PART 2: Private Personal Agent Cockpit (window.PrismPrivateAgent)
     Tabs: 1) 我的仓位底牌  2) 私人盯盘守望  3) 个人专属通知
     ─────────────────────────────────────────────────────────── */
  var paState = {
    tab: 'position',
    hub: null,
    draftHoldings: []
  };

  function ensurePrivateDialog() {
    var dlg = document.getElementById('privateAgentDialog');
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.id = 'privateAgentDialog';
    dlg.className = 'persona-dialog';
    dlg.setAttribute('aria-label', '私人投研助理与盯盘守望中心');
    dlg.innerHTML =
      '<div class="persona-dialog__hd">' +
        '<div>' +
          '<span class="persona-dialog__kicker">PRIVATE PERSONAL AGENT</span>' +
          '<h2>私人投研助理 · 底牌与守望中心</h2>' +
        '</div>' +
        '<button type="button" class="copilot-icon-btn" id="paCloseBtn" aria-label="关闭">✕</button>' +
      '</div>' +
      '<div class="pa-tab-bar" id="paTabBar">' +
        '<button type="button" class="active" data-pa-tab="position">' + ico('shield') + '我的仓位底牌</button>' +
        '<button type="button" data-pa-tab="watchman">' + ico('pulse') + '私人盯盘守望 <span id="paWatchCnt"></span></button>' +
        '<button type="button" data-pa-tab="push">' + ico('bell') + '个人专属通知</button>' +
      '</div>' +
      '<div class="persona-dialog__body" id="paDialogBody"></div>';
    document.body.appendChild(dlg);

    dlg.querySelector('#paCloseBtn').onclick = function () { dlg.close(); };
    dlg.addEventListener('click', function (ev) { if (ev.target === dlg) dlg.close(); });
    dlg.querySelectorAll('#paTabBar button').forEach(function (btn) {
      btn.onclick = function () {
        paState.tab = btn.getAttribute('data-pa-tab') || 'position';
        dlg.querySelectorAll('#paTabBar button').forEach(function (b) { b.classList.toggle('active', b === btn); });
        renderPrivateBody();
      };
    });
    return dlg;
  }

  function syncCopilotPrivatePill() {
    if (paState.hub) {
      window._prismPrivateHubSnapshot = paState.hub;
    }
    var pills = [document.getElementById('copilotPrivateBtn'), document.getElementById('futuresPrivateBtn')];
    if (paState.hub) {
      var pos = paState.hub.position_state || {};
      var activeWatches = (paState.hub.watchman_rules || []).filter(function (r) { return r.enabled; }).length;
      var txt = pos.configured
        ? ('仓位 ' + pos.position_pct + '%' + (activeWatches > 0 ? ' · 盯' + activeWatches : ''))
        : (activeWatches > 0 ? ('底牌 · 盯' + activeWatches) : '底牌');
      pills.forEach(function (pill) { if (pill) pill.textContent = txt; });
    }
    if (window.App && typeof window.App._refreshCopilotContextBar === 'function') {
      window.App._refreshCopilotContextBar();
    }
  }

  function renderPrivateBody() {
    var body = document.getElementById('paDialogBody');
    if (!body) return;
    if (!paState.hub) {
      body.innerHTML = '<div class="radar-empty">正在读取你的私人保险箱与守望规则…</div>';
      return;
    }
    var wCnt = document.getElementById('paWatchCnt');
    var activeN = (paState.hub.watchman_rules || []).filter(function (r) { return r.enabled; }).length;
    if (wCnt) wCnt.textContent = activeN ? '(' + activeN + ')' : '';

    if (paState.tab === 'position') renderPositionTab(body);
    else if (paState.tab === 'watchman') renderWatchmanTab(body);
    else renderPersonalPushTab(body);
  }

  function renderPositionTab(body) {
    var pos = paState.hub.position_state || { position_pct: 0, max_single_loss_pct: 8, cash_note: '', holdings: [] };
    paState.draftHoldings = (pos.holdings || []).map(function (h) { return Object.assign({}, h); });

    body.innerHTML =
      '<div class="pa-priv-banner">' +
        '<b>私有仓位保险箱：</b>你的总仓位与核心持仓仅在本人提问时注入你的私人 Agent（用于自动对号入座圈内策略与风控止损），圈内其他成员与运维后台界面均不可见。' +
      '</div>' +
      '<form id="paPosForm" class="pa-form">' +
        '<div class="pa-field">' +
          '<div class="pa-field-top">' +
            '<label>当前总仓位成数</label>' +
            '<strong id="paPctLabel" class="pa-pct-val">' + Number(pos.position_pct || 0) + '%</strong>' +
          '</div>' +
          '<input type="range" id="paPctRange" min="0" max="100" step="5" value="' + Number(pos.position_pct || 0) + '" style="width:100%;accent-color:var(--accent,#1f6f54);">' +
          '<div class="pa-quick-chips">' +
            [['空仓 0%', 0], ['轻仓 25%', 25], ['半仓 50%', 50], ['重仓 80%', 80], ['满仓 95%', 95]].map(function (p) {
              return '<button type="button" class="market-qc-btn-secondary" data-pct="' + p[1] + '">' + p[0] + '</button>';
            }).join('') +
          '</div>' +
        '</div>' +
        '<div class="pa-grid-2">' +
          '<label class="pa-field"><span>单笔最大容忍亏损 (%)</span><input type="number" id="paMaxLoss" min="1" max="50" step="0.5" value="' + Number(pos.max_single_loss_pct || 8) + '"></label>' +
          '<label class="pa-field"><span>仓位节奏备注（选填）</span><input type="text" id="paCashNote" maxlength="160" placeholder="如：留 2 成现金等缩量回调" value="' + escAttr(pos.cash_note || '') + '"></label>' +
        '</div>' +
        '<div class="pa-field">' +
          '<div class="pa-field-top">' +
            '<label>核心持仓标的（选填，方便 Agent 针对性排雷）</label>' +
            '<button type="button" class="market-qc-btn-secondary" id="paAddHoldingBtn">＋ 添加标的</button>' +
          '</div>' +
          '<div id="paHoldingsRows" class="pa-holdings-rows"></div>' +
        '</div>' +
        '<div class="pa-actions">' +
          '<button type="submit" class="market-primary-btn" id="paSavePosBtn">保存私人仓位底牌</button>' +
        '</div>' +
      '</form>';

    var range = body.querySelector('#paPctRange');
    var lbl = body.querySelector('#paPctLabel');
    range.oninput = function () { lbl.textContent = range.value + '%'; };
    body.querySelectorAll('[data-pct]').forEach(function (btn) {
      btn.onclick = function () {
        range.value = btn.getAttribute('data-pct');
        lbl.textContent = range.value + '%';
      };
    });

    function drawHoldings() {
      var wrap = body.querySelector('#paHoldingsRows');
      if (!paState.draftHoldings.length) {
        wrap.innerHTML = '<div class="muted" style="font-size:12px;padding:4px 0;">暂未填写具体持仓（只设总仓位百分比也完全可用）。</div>';
        return;
      }
      wrap.innerHTML = paState.draftHoldings.map(function (h, idx) {
        return (
          '<div class="pa-holding-row" data-idx="' + idx + '">' +
            '<input type="text" placeholder="代码 如 600519 / RB" value="' + escAttr(h.code || '') + '" data-hk="code">' +
            '<input type="text" placeholder="简称（选填）" value="' + escAttr(h.name || '') + '" data-hk="name">' +
            '<input type="number" step="any" placeholder="成本价（选填）" value="' + (h.cost_price != null ? escAttr(h.cost_price) : '') + '" data-hk="cost_price">' +
            '<input type="text" placeholder="备注 如 底仓/重仓" value="' + escAttr(h.weight_note || '') + '" data-hk="weight_note">' +
            '<button type="button" class="market-qc-btn-secondary" data-rm="' + idx + '">✕</button>' +
          '</div>'
        );
      }).join('');
      wrap.querySelectorAll('[data-hk]').forEach(function (inp) {
        inp.oninput = function () {
          var i = Number(inp.parentElement.getAttribute('data-idx'));
          var k = inp.getAttribute('data-hk');
          if (k === 'cost_price') paState.draftHoldings[i][k] = inp.value ? Number(inp.value) : null;
          else paState.draftHoldings[i][k] = inp.value;
        };
      });
      wrap.querySelectorAll('[data-rm]').forEach(function (b) {
        b.onclick = function () {
          paState.draftHoldings.splice(Number(b.getAttribute('data-rm')), 1);
          drawHoldings();
        };
      });
    }
    drawHoldings();

    body.querySelector('#paAddHoldingBtn').onclick = function () {
      if (paState.draftHoldings.length >= 15) return;
      paState.draftHoldings.push({ code: '', name: '', cost_price: null, weight_note: '' });
      drawHoldings();
    };

    body.querySelector('#paPosForm').onsubmit = async function (ev) {
      ev.preventDefault();
      var validHoldings = paState.draftHoldings.filter(function (h) { return h.code && String(h.code).trim(); });
      var btn = body.querySelector('#paSavePosBtn');
      btn.disabled = true;
      try {
        var r = await api('/api/private-agent/state', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            position_pct: Number(range.value || 0),
            max_single_loss_pct: Number(body.querySelector('#paMaxLoss').value || 8),
            cash_note: body.querySelector('#paCashNote').value.trim(),
            holdings: validHoldings
          })
        });
        var d = await r.json().catch(function () { return {}; });
        if (!r.ok) { toast(d.detail || '保存失败'); return; }
        paState.hub.position_state = d.position_state;
        syncCopilotPrivatePill();
        toast('✓ 私人仓位底牌已更新，后续 Agent 对话立即生效');
      } finally {
        btn.disabled = false;
      }
    };
  }

  function renderWatchmanTab(body) {
    var rules = paState.hub.watchman_rules || [];
    body.innerHTML =
      '<div class="pa-priv-banner">' +
        '<b>0-Token 全天候私人盯盘：</b>直接用一句大白话写下你要等的条件，后台每 45 秒用纯代码自动核对行情与雷达快讯（平时零模型消耗），条件触发瞬间推送到你绑定的个人微信或手机。' +
      '</div>' +
      '<form id="paWatchForm" class="pa-watch-bar">' +
        '<input type="text" id="paWatchInp" maxlength="240" placeholder="例如：沪深300 跌破 4000 提醒我 ／ 600519 站上 1580 ／ 风向命中 光模块">' +
        '<button type="submit" class="market-primary-btn">＋ 添加守望</button>' +
      '</form>' +
      '<div class="pa-quick-chips" style="margin:8px 0 14px;">' +
        '<span class="muted" style="font-size:12px;">点选示例：</span>' +
        [
          '沪深300 跌破 4000 提醒我',
          '600519 跌破 1480 提醒我',
          '601138 涨幅超过 5% 提醒我',
          '螺纹钢 跌破 3100 提醒我',
          '风向命中 政策细则 提醒我'
        ].map(function (ex) {
          return '<button type="button" class="market-qc-btn-secondary" data-wex="' + escAttr(ex) + '">' + esc(ex) + '</button>';
        }).join('') +
      '</div>' +
      '<div class="pa-field-top" style="margin-bottom:8px;">' +
        '<strong>我的盯盘守望列表 (' + rules.length + ')</strong>' +
        '<button type="button" class="market-qc-btn-secondary" id="paCheckNowBtn">↻ 立即巡检一次</button>' +
      '</div>' +
      '<div id="paWatchList" class="pa-watch-list">' +
        (rules.length
          ? rules.map(function (r) {
              var stBadge = r.triggered_at
                ? '<span class="intel-pill mcheck ok">✓ 已触发提醒</span>'
                : (r.enabled ? '<span class="intel-pill cat"><i class="status-dot live"></i>守望监听中</span>' : '<span class="intel-pill">已停用</span>');
              return (
                '<div class="pa-watch-item">' +
                  '<div class="pa-watch-item__main">' +
                    '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
                      stBadge +
                      '<b>' + esc(r.description) + '</b>' +
                    '</div>' +
                    '<div class="muted" style="font-size:12px;margin-top:4px;">' +
                      (r.triggered_at
                        ? ('触发回执：' + esc(r.trigger_summary) + (r.push_delivered ? ' · 已推送至手机' : ''))
                        : ('最新观测：' + esc(r.last_observed || '等待下一轮轮询') + ' · 创建于 ' + esc(fmtTime(r.created_at)))) +
                    '</div>' +
                  '</div>' +
                  '<button type="button" class="market-qc-btn-secondary" data-wdel="' + escAttr(r.id) + '">删除</button>' +
                '</div>'
              );
            }).join('')
          : '<div class="muted" style="padding:18px 0;text-align:center;">还没有私人盯盘规则，在上方输入一句话即可让助理替你盯盘。</div>') +
      '</div>';

    var wInp = body.querySelector('#paWatchInp');
    body.querySelectorAll('[data-wex]').forEach(function (b) {
      b.onclick = function () { wInp.value = b.getAttribute('data-wex'); wInp.focus(); };
    });

    body.querySelector('#paWatchForm').onsubmit = async function (ev) {
      ev.preventDefault();
      var val = wInp.value.trim();
      if (!val) return;
      var r = await api('/api/private-agent/watchman', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ raw_text: val })
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) { toast(d.detail || '规则解析失败'); return; }
      wInp.value = '';
      paState.hub.watchman_rules = d.rules || [];
      syncCopilotPrivatePill();
      toast('✓ 已启动后台守望：' + (d.rule && d.rule.description ? d.rule.description : ''));
      renderPrivateBody();
    };

    body.querySelector('#paCheckNowBtn').onclick = async function () {
      var r = await api('/api/private-agent/watchman/check-now', { method: 'POST' });
      var d = await r.json().catch(function () { return {}; });
      if (r.ok) {
        paState.hub.watchman_rules = d.rules || [];
        syncCopilotPrivatePill();
        toast('已完成实时核对（触发 ' + ((d.summary && d.summary.triggered) || 0) + ' 条）');
        renderPrivateBody();
      }
    };

    body.querySelectorAll('[data-wdel]').forEach(function (b) {
      b.onclick = async function () {
        var id = b.getAttribute('data-wdel');
        var r = await api('/api/private-agent/watchman/' + encodeURIComponent(id), { method: 'DELETE' });
        if (r.ok) {
          paState.hub.watchman_rules = paState.hub.watchman_rules.filter(function (x) { return x.id !== id; });
          syncCopilotPrivatePill();
          renderPrivateBody();
        }
      };
    });
  }

  function renderPersonalPushTab(body) {
    var p = paState.hub.push || {};
    var provs = paState.hub.push_providers || [];
    var curProv = p.provider || 'pushplus';
    var meta = provs.find(function (x) { return x.id === curProv; }) || provs[0] || {
      id: 'pushplus', label: 'PushPlus · 个人微信直推', secret_label: 'PushPlus Token', placeholder: '粘贴 32 位个人 Token', guide: '微信扫码登录 pushplus.plus 复制一对一 Token 即可。'
    };

    body.innerHTML =
      '<div class="pa-priv-banner">' +
        '<b>个人独立通知通道：</b>这里绑定的是<b>只属于你个人账号</b>的手机或微信，仅在你自己的「私人盯盘守望」触发时推送给你本人，不会打扰圈内其他人。' +
      '</div>' +
      '<form id="paPushForm" class="pa-form">' +
        '<label class="pa-field">' +
          '<span>选择通知方式</span>' +
          '<select id="paPushProv">' +
            provs.map(function (pr) {
              return '<option value="' + escAttr(pr.id) + '"' + (pr.id === curProv ? ' selected' : '') + '>' + esc(pr.label) + '</option>';
            }).join('') +
          '</select>' +
        '</label>' +
        '<div id="paPushGuide" class="pa-guide-box">' + esc(meta.guide || '') + '</div>' +
        '<label class="pa-field">' +
          '<span id="paPushSecLabel">' + esc(meta.secret_label || '推送凭证') + (p.has_secret ? ('（已存 ' + esc(p.masked_secret) + '）') : '') + '</span>' +
          '<input type="password" id="paPushSecret" autocomplete="off" placeholder="' + escAttr(p.has_secret ? '留空保持已存凭证，或输入新凭证覆盖' : (meta.placeholder || '')) + '">' +
        '</label>' +
        '<label class="intel-anon-chk" style="margin:4px 0;">' +
          '<input type="checkbox" id="paPushEnabled"' + (p.enabled !== false ? ' checked' : '') + '> <span>启用个人盯盘推送</span>' +
        '</label>' +
        '<div class="pa-actions" style="display:flex;gap:10px;flex-wrap:wrap;">' +
          '<button type="submit" class="market-primary-btn">保存个人通道</button>' +
          '<button type="button" class="market-qc-btn-secondary" id="paPushTestBtn">发送测试通知到我手机</button>' +
        '</div>' +
      '</form>';

    var provSel = body.querySelector('#paPushProv');
    provSel.onchange = function () {
      var m = provs.find(function (x) { return x.id === provSel.value; });
      if (!m) return;
      body.querySelector('#paPushGuide').textContent = m.guide || '';
      body.querySelector('#paPushSecLabel').textContent = m.secret_label || '推送凭证';
      body.querySelector('#paPushSecret').placeholder = m.placeholder || '';
    };

    body.querySelector('#paPushForm').onsubmit = async function (ev) {
      ev.preventDefault();
      var secVal = body.querySelector('#paPushSecret').value.trim();
      var modeDefault = provSel.value === 'bark' ? 'timeSensitive' : (provSel.value === 'ntfy' ? 'high' : 'markdown');
      var r = await api('/api/private-agent/push', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider: provSel.value,
          secret: secVal || null,
          enabled: body.querySelector('#paPushEnabled').checked,
          bark_level: modeDefault
        })
      });
      var d = await r.json().catch(function () { return {}; });
      if (!r.ok) { toast(d.detail || '保存失败'); return; }
      paState.hub.push = d.push;
      toast('✓ 个人通知通道已保存');
      renderPrivateBody();
    };

    body.querySelector('#paPushTestBtn').onclick = async function () {
      var btn = body.querySelector('#paPushTestBtn');
      btn.disabled = true;
      try {
        var r = await api('/api/private-agent/push/test', { method: 'POST' });
        var d = await r.json().catch(function () { return {}; });
        if (!r.ok) { toast(d.detail || '请先保存个人推送凭证后再测试'); return; }
        toast('✓ 测试通知已发出，请查看手机或微信！');
      } finally {
        btn.disabled = false;
      }
    };
  }

  window.PrismPrivateAgent = {
    open: async function (initialTab) {
      if (initialTab) paState.tab = initialTab;
      var dlg = ensurePrivateDialog();
      dlg.querySelectorAll('#paTabBar button').forEach(function (b) {
        b.classList.toggle('active', b.getAttribute('data-pa-tab') === paState.tab);
      });
      if (!dlg.open) dlg.showModal();
      renderPrivateBody();
      await this.refresh();
    },
    refresh: async function () {
      try {
        var r = await api('/api/private-agent/hub');
        if (!r.ok) return;
        paState.hub = await r.json();
        syncCopilotPrivatePill();
        var dlg = document.getElementById('privateAgentDialog');
        if (dlg && dlg.open) renderPrivateBody();
      } catch (e) {}
    }
  };

  /* Quietly hydrate pill badge on /prism and /futures after load */
  if (typeof window !== 'undefined' && typeof document !== 'undefined' && window.location && (window.location.pathname.indexOf('/prism') === 0 || window.location.pathname.indexOf('/futures') === 0)) {
    if (document.readyState === 'complete') {
      setTimeout(function () { window.PrismPrivateAgent.refresh(); }, 900);
    } else {
      window.addEventListener('load', function () {
        setTimeout(function () { window.PrismPrivateAgent.refresh(); }, 900);
      });
    }
  }
})();
