/* Prism V4.2 — Global Event Radar War Room & Topbar Sentinel (Pure User Plane)
   Platform-level Gemini Search Grounding & Mobile Push are managed exclusively in Ops (/ops/). */
(function () {
  'use strict';

  var state = {
    feed: null,
    tierFilter: 'all',
    selectedId: '',
    pollTimer: null,
    briefingOpen: false,
    initialized: false
  };

  function esc(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function escAttr(str) {
    return esc(str).replace(/'/g, '&#39;');
  }

  function toast(msg) {
    if (window.App && typeof window.App._toast === 'function') {
      window.App._toast(msg);
      return;
    }
    var el = document.getElementById('radarMiniToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'radarMiniToast';
      el.className = 'app-toast';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(function () { el.classList.remove('show'); }, 3400);
  }

  function api(url, opts) {
    opts = Object.assign({ credentials: 'same-origin', cache: 'no-store' }, opts || {});
    var headers = Object.assign({ 'X-Requested-With': 'XMLHttpRequest' }, opts.headers || {});
    var method = String(opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      var u = (window.App && window.App.user) || (document.getElementById('futuresUserBadge') && document.getElementById('futuresUserBadge').textContent.trim()) || '';
      if (u && !headers['X-Prism-Account']) headers['X-Prism-Account'] = u;
    }
    opts.headers = headers;
    return fetch(url, opts);
  }

  function formatShortTime(iso) {
    if (!iso) return '';
    var m = String(iso).match(/(\d{2}-\d{2})T(\d{2}:\d{2})/);
    if (m) return m[1] + ' ' + m[2];
    return String(iso).slice(0, 16);
  }

  function ensureDialogs() {
    if (!document.getElementById('radarDrawerDialog')) {
      var drawer = document.createElement('dialog');
      drawer.id = 'radarDrawerDialog';
      drawer.className = 'persona-dialog radar-dialog';
      drawer.setAttribute('aria-labelledby', 'radarDialogTitle');
      drawer.innerHTML =
        '<header class="persona-dialog__hd">' +
          '<div class="persona-dialog__hd-copy">' +
            '<span class="persona-dialog__kicker">PRISM GLOBAL RADAR · 全球与盘中风向哨兵</span>' +
            '<h2 id="radarDialogTitle">突发风向速览与作战剧本</h2>' +
            '<p id="radarWindowSub">同步监控财联社/新浪/东财 + 美联储/SEC 8-K/CNBC 全球一手源 · 跨市场实时价格印证</p>' +
          '</div>' +
          '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">' +
            '<button type="button" class="market-secondary-btn" style="height:32px;font-size:12px;" onclick="window.PrismRadar.openWorkspace()">进入战情室 ↗</button>' +
            '<button type="button" class="market-secondary-btn" id="radarRefreshBtn" style="height:32px;font-size:12px;" onclick="window.PrismRadar.refreshNow()">↻ 立即巡检</button>' +
            '<button type="button" class="copilot-icon-btn" onclick="window.PrismRadar.closeDrawer()" aria-label="关闭风向雷达">✕</button>' +
          '</div>' +
        '</header>' +
        '<div class="radar-filter-bar" id="radarFilterBar">' +
          '<div class="radar-filter-chips">' +
            '<button type="button" class="radar-chip active" data-tier="all" onclick="window.PrismRadar.setTier(\'all\')">全部快讯</button>' +
            '<button type="button" class="radar-chip" data-tier="T0" onclick="window.PrismRadar.setTier(\'T0\')">🔴 T0 顶层核弹</button>' +
            '<button type="button" class="radar-chip" data-tier="T1" onclick="window.PrismRadar.setTier(\'T1\')">🟠 T1 强催化</button>' +
            '<button type="button" class="radar-chip" data-tier="GLOBAL" onclick="window.PrismRadar.setTier(\'GLOBAL\')">🌐 全球一手</button>' +
          '</div>' +
          '<span class="radar-status-meta" id="radarCollectorMeta">准备同步…</span>' +
        '</div>' +
        '<div class="persona-dialog__body radar-dialog__body" id="radarFeedList"></div>';
      drawer.addEventListener('click', function (ev) {
        if (ev.target === drawer && drawer.open) drawer.close();
      });
      document.body.appendChild(drawer);
    }
  }

  function updateTopbarPill(feed) {
    var pill = document.getElementById('radarTopbarPill');
    var txt = document.getElementById('radarTopbarText');
    if (!pill || !txt || !feed) return;
    pill.classList.remove('is-t0', 'is-t1');
    var counts = feed.counts || {};
    var t0 = Number(counts.T0 || 0);
    var t1 = Number(counts.T1 || 0);
    var hl = feed.headline;
    if (t0 > 0) {
      pill.classList.add('is-t0');
      txt.textContent = 'T0 突发 · ' + t0 + '条';
      pill.title = (hl && hl.title ? hl.title + ' — ' : '') + '点击速览或进入风向战情室';
    } else if (t1 > 0) {
      pill.classList.add('is-t1');
      txt.textContent = '风向 · ' + t1 + '条催化';
      pill.title = (hl && hl.title ? hl.title + ' — ' : '') + '点击速览或进入风向战情室';
    } else {
      txt.textContent = '风向 · 正常';
      pill.title = '查看全球与国内实时风向雷达';
    }
  }

  function getFilteredEvents() {
    var feed = state.feed;
    if (!feed || !Array.isArray(feed.events)) return [];
    return feed.events.filter(function (ev) {
      if (state.tierFilter === 'watchlist') return Array.isArray(ev.watchlist_hits) && ev.watchlist_hits.length > 0;
      if (state.tierFilter === 'GLOBAL') return ev.region === 'GLOBAL';
      return state.tierFilter === 'all' || ev.tier === state.tierFilter;
    });
  }

  function renderTapeStripHtml(tape) {
    if (!Array.isArray(tape) || !tape.length) return '';
    return (
      '<div class="radar-tape-strip" aria-label="全球跨市场实时温度计">' +
        '<span class="radar-tape-lbl">全球价格锚</span>' +
        tape.map(function (it) {
          var chg = Number(it.change_pct || 0);
          var cls = chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat');
          var sign = chg > 0 ? '+' : '';
          return (
            '<span class="radar-tape-pill ' + cls + '">' +
              '<em>' + esc(it.name) + '</em>' +
              '<b>' + esc(it.price) + '</b>' +
              '<small>' + sign + chg.toFixed(2) + '%</small>' +
            '</span>'
          );
        }).join('') +
      '</div>'
    );
  }

  function renderBriefingBarHtml(briefing) {
    if (!briefing) {
      return (
        '<div class="radar-briefing-bar radar-briefing-bar--empty">' +
          '<div>' +
            '<strong>🌐 全球隔夜 & 午间作战早午报</strong>' +
            '<span>每个交易日 08:45（竞价前）与 12:45（午盘前）自动汇总全球宏观/算力链/大宗异动，也可随时手动刷新</span>' +
          '</div>' +
          '<button type="button" class="market-secondary-btn" id="radarGenBriefBtn" onclick="window.PrismRadar.generateBriefingNow()">⚡ 立即合成全球简报</button>' +
        '</div>'
      );
    }
    var bullets = Array.isArray(briefing.bullets) ? briefing.bullets : [];
    var sources = Array.isArray(briefing.sources) ? briefing.sources : [];
    return (
      '<section class="radar-briefing-bar">' +
        '<div class="radar-briefing-bar__top">' +
          '<div class="radar-briefing-bar__title">' +
            '<span class="radar-global-pill">🌐 ' + esc(briefing.title || '全球作战简报') + '</span>' +
            '<span class="radar-briefing-meta">' + esc(formatShortTime(briefing.generated_at)) + ' · ' + esc(briefing.engine || '') + '</span>' +
          '</div>' +
          '<div style="display:flex;gap:8px;align-items:center;">' +
            '<button type="button" class="radar-link-btn" onclick="window.PrismRadar.toggleBriefingBullets()">' + (state.briefingOpen ? '收起要点 ▴' : '展开要点 ▾') + '</button>' +
            '<button type="button" class="market-secondary-btn" id="radarGenBriefBtn" style="height:28px;font-size:11.5px;" onclick="window.PrismRadar.generateBriefingNow()">↻ 刷新简报</button>' +
          '</div>' +
        '</div>' +
        '<p class="radar-briefing-overview">' + esc(briefing.overview || '') + '</p>' +
        (briefing.action_plan ? '<div class="radar-briefing-plan"><b>当前窗口纪律：</b>' + esc(briefing.action_plan) + '</div>' : '') +
        (state.briefingOpen && bullets.length
          ? '<ul class="radar-briefing-bullets">' + bullets.map(function (b) { return '<li>' + esc(b) + '</li>'; }).join('') + '</ul>' +
            (sources.length
              ? '<div class="radar-grounding-links">实时溯源：' + sources.map(function (s) {
                  return '<a href="' + escAttr(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + ' ↗</a>';
                }).join(' · ') + '</div>'
              : '')
          : '') +
      '</section>'
    );
  }

  function renderPlaybookCardHtml(ev, isWarroomDetail) {
    var tierCls = ev.tier === 'T0' ? 't0' : (ev.tier === 'T1' ? 't1' : 't2');
    var tierLbl = ev.tier === 'T0' ? '🔴 T0 顶层核弹' : (ev.tier === 'T1' ? '🟠 T1 强催化' : '⚪ T2 快讯');
    var pb = ev.playbook || {};
    var ai = pb.ai_enrichment || null;
    var aiReady = !!(state.feed && state.feed.ai_ready);
    var targets = Array.isArray(pb.targets) ? pb.targets : [];
    var futures = Array.isArray(pb.futures) ? pb.futures : [];
    var deltas = Array.isArray(pb.delta_points) ? pb.delta_points : [];
    var watchHits = Array.isArray(ev.watchlist_hits) ? ev.watchlist_hits : [];
    var isGlobal = ev.region === 'GLOBAL';

    var targetChips = targets.map(function (t) {
      var meta = [];
      if (t.change_pct != null) meta.push((Number(t.change_pct) >= 0 ? '+' : '') + Number(t.change_pct).toFixed(2) + '%');
      if (t.pe_ttm != null) meta.push('PE ' + Number(t.pe_ttm).toFixed(1));
      var suffix = meta.length ? (' · ' + meta.join(' / ')) : '';
      return '<button type="button" class="chain-jump-chip" onclick="window.PrismRadar.jumpStock(\'' + escAttr(t.ticker) + '\',\'' + escAttr(t.name || t.ticker) + '\')">' +
        esc((t.name || t.ticker) + ' ' + t.ticker + suffix) + '</button>';
    }).join('');

    var futChips = futures.map(function (sym) {
      return '<a class="chain-jump-chip futures" href="/futures?symbol=' + escAttr(sym) + '">期货锚：' + esc(sym) + ' ↗</a>';
    }).join('');

    var aiSources = (ai && Array.isArray(ai.sources)) ? ai.sources : [];
    var showPlaybook = isWarroomDetail || ev.tier === 'T0' || ev.tier === 'T1' || !!ai;
    return (
      '<article class="radar-card radar-card--' + tierCls + (isWarroomDetail ? ' radar-card--detail' : '') + '" data-radar-id="' + escAttr(ev.id) + '">' +
        '<div class="radar-card__hd">' +
          '<div class="radar-card__badges">' +
            '<span class="radar-tier-badge ' + tierCls + '">' + tierLbl + '</span>' +
            (isGlobal ? '<span class="radar-global-pill">🌐 全球一手</span>' : '') +
            '<span class="radar-theme-tag">' + esc(ev.theme || '市场风向') + '</span>' +
            (watchHits.length ? '<span class="radar-watch-hit">★ 命中自选 ' + esc(watchHits.join('、')) + '</span>' : '') +
            (ai ? '<span class="radar-ai-pill">✦ 全球溯源已核验</span>' : '') +
            (ev.echo_count > 1 ? '<span class="radar-echo-tag">已合并跟进 +' + (ev.echo_count - 1) + '</span>' : '') +
          '</div>' +
          '<span class="radar-card__time">' + esc(formatShortTime(ev.published_at)) + ' · ' + esc(ev.source || '') + '</span>' +
        '</div>' +
        '<h4 class="radar-card__title">' +
          (ev.url ? '<a href="' + escAttr(ev.url) + '" target="_blank" rel="noopener noreferrer">' + esc(ai && ai.cn_title ? (ai.cn_title + '（' + ev.title.slice(0, 60) + '）') : ev.title) + ' ↗</a>' : esc((ai && ai.cn_title) || ev.title)) +
        '</h4>' +
        (ev.body && ev.body !== ev.title ? '<p class="radar-card__body">' + esc(isWarroomDetail ? ev.body.slice(0, 800) : ev.body.slice(0, 240)) + (ev.body.length > (isWarroomDetail ? 800 : 240) ? '…' : '') + '</p>' : '') +
        (showPlaybook
          ? '<div class="radar-playbook">' +
              '<div class="radar-playbook__win">⏱ ' + esc(pb.window_tag || ev.window_tag || '实时窗口') + '</div>' +
              (ai && ai.verdict ? '<div class="radar-playbook__verdict"><b>🌐 全球核验定性：</b>' + esc(ai.verdict) + '</div>' : '') +
              (pb.tape_verdict ? '<div class="radar-playbook__tape">' + esc(pb.tape_verdict) + '</div>' : '') +
              (deltas.length ? '<div class="radar-playbook__row"><b>核心增量：</b>' + esc(deltas.join('；')) + '</div>' : '') +
              (pb.execution_rule ? '<div class="radar-playbook__row"><b>节奏纪律：</b>' + esc(pb.execution_rule) + '</div>' : '') +
              (pb.invalidation_rule ? '<div class="radar-playbook__row radar-playbook__inv"><b>失效红线：</b>' + esc(pb.invalidation_rule) + '</div>' : '') +
              (aiSources.length
                ? '<div class="radar-grounding-links">全球信源核验：' + aiSources.map(function (s) {
                    return '<a href="' + escAttr(s.url) + '" target="_blank" rel="noopener noreferrer">' + esc(s.title) + ' ↗</a>';
                  }).join(' · ') + '</div>'
                : '') +
              ((targetChips || futChips) ? '<div class="market-chain-mini__chips" style="margin-top:6px;">' + targetChips + futChips + '</div>' : '') +
            '</div>'
          : '') +
        '<div class="radar-card__ft" style="gap:8px;flex-wrap:wrap;">' +
          (aiReady && !ai
            ? '<button type="button" class="market-secondary-btn" id="radarEnrichBtn-' + escAttr(ev.id) + '" style="height:29px;font-size:11.5px;" onclick="window.PrismRadar.enrichEvent(\'' + escAttr(ev.id) + '\')">🌐 全球搜索溯源</button>'
            : '') +
          '<button type="button" class="copilot-trigger-chip" onclick="window.PrismRadar.askAgent(\'' + escAttr(ev.id) + '\')">✦ 交给随行 Agent 结合画像推演</button>' +
        '</div>' +
      '</article>'
    );
  }

  function renderWorkspaceView() {
    var sideEl = document.getElementById('radarSidebarInner');
    var roomEl = document.getElementById('radarWarroom');
    if (!roomEl) return;
    var feed = state.feed;
    if (!feed) {
      roomEl.innerHTML = '<div class="radar-empty" style="margin:32px auto;max-width:460px;">正在同步全球与国内风向电报…</div>';
      return;
    }
    var counts = feed.counts || {};
    var watchCount = (feed.events || []).filter(function (e) { return e.watchlist_hits && e.watchlist_hits.length; }).length;
    var winTag = (feed.window && feed.window.tag) || '实时巡检';
    var cadence = (feed.window && feed.window.cadence_seconds) || 25;

    if (sideEl) {
      var filters = [
        ['all', '全部快讯', counts.total || 0],
        ['T0', '🔴 T0 顶层核弹', counts.T0 || 0],
        ['T1', '🟠 T1 产业催化', counts.T1 || 0],
        ['GLOBAL', '🌐 全球一手源', counts.global || 0],
        ['watchlist', '★ 命中自选', watchCount],
        ['T2', '⚪ T2 常规动态', counts.T2 || 0]
      ];
      sideEl.innerHTML =
        '<div class="radar-side-card">' +
          '<div class="radar-side-card__kicker">当前交易时段</div>' +
          '<div class="radar-side-card__win">' + esc(winTag) + '</div>' +
          '<div class="radar-side-card__meta">6 路中英一手源并行 · ' + esc(cadence) + 's 轮询</div>' +
          '<div class="radar-side-card__actions">' +
            '<button type="button" class="market-secondary-btn" id="radarSideRefreshBtn" onclick="window.PrismRadar.refreshNow()">↻ 立即巡检</button>' +
          '</div>' +
        '</div>' +
        '<div class="radar-side-section-lbl">级别与地域筛选</div>' +
        '<div class="radar-side-filters">' +
          filters.map(function (f) {
            return '<button type="button" class="radar-side-filter' + (state.tierFilter === f[0] ? ' active' : '') + '" onclick="window.PrismRadar.setTier(\'' + f[0] + '\')">' +
              '<span>' + f[1] + '</span><b>' + f[2] + '</b></button>';
          }).join('') +
        '</div>';
    }

    var headerHtml =
      '<div class="radar-warroom__top">' +
        renderTapeStripHtml(feed.tape || []) +
        renderBriefingBarHtml(feed.briefing || null) +
      '</div>';

    var events = getFilteredEvents();
    if (!events.length) {
      roomEl.innerHTML =
        headerHtml +
        '<div class="radar-warroom__empty">' +
          '<h3>当前筛选下暂无风向快讯</h3>' +
          '<p>可点击左侧「立即巡检」拉取全球与国内最新一手电报，或切换至「全部快讯」查看。</p>' +
        '</div>';
      return;
    }

    var activeEv = events.find(function (e) { return e.id === state.selectedId; }) || events[0];
    state.selectedId = activeEv.id;

    var listHtml = events.map(function (ev) {
      var tierCls = ev.tier === 'T0' ? 't0' : (ev.tier === 'T1' ? 't1' : 't2');
      var isSel = ev.id === activeEv.id;
      var hasWatch = ev.watchlist_hits && ev.watchlist_hits.length;
      var ai = ev.playbook && ev.playbook.ai_enrichment;
      return (
        '<button type="button" class="radar-feed-row' + (isSel ? ' active' : '') + ' radar-feed-row--' + tierCls + '" onclick="window.PrismRadar.selectEvent(\'' + escAttr(ev.id) + '\')">' +
          '<div class="radar-feed-row__top">' +
            '<span class="radar-tier-badge ' + tierCls + '">' + esc(ev.tier) + '</span>' +
            (ev.region === 'GLOBAL' ? '<span class="radar-global-dot" title="全球一手源">🌐</span>' : '') +
            '<span class="radar-feed-row__theme">' + esc(ev.source || ev.theme || '') + '</span>' +
            (hasWatch ? '<span class="radar-feed-row__star" title="命中自选">★</span>' : '') +
            (ai ? '<span class="radar-feed-row__ai" title="已全球溯源">✦</span>' : '') +
            '<span class="radar-feed-row__time">' + esc(formatShortTime(ev.published_at).slice(6)) + '</span>' +
          '</div>' +
          '<div class="radar-feed-row__title">' + esc((ai && ai.cn_title) || ev.title) + '</div>' +
        '</button>'
      );
    }).join('');

    roomEl.innerHTML =
      headerHtml +
      '<div class="radar-warroom__list" aria-label="实时风向快讯流">' + listHtml + '</div>' +
      '<div class="radar-warroom__detail" aria-label="作战剧本详情">' +
        renderPlaybookCardHtml(activeEv, true) +
      '</div>';
  }

  function renderFeedList() {
    var listEl = document.getElementById('radarFeedList');
    var metaEl = document.getElementById('radarCollectorMeta');
    var subEl = document.getElementById('radarWindowSub');
    if (!listEl) return;

    var feed = state.feed;
    if (!feed) {
      listEl.innerHTML = '<div class="radar-empty">正在读取全球与国内最新风向电报…</div>';
      return;
    }

    if (subEl && feed.window && feed.window.tag) {
      subEl.textContent = '当前时段：' + feed.window.tag + ' · 6 路中英一手源（财联社/新浪/东财/Fed/SEC 8-K/CNBC）实时印证';
    }
    if (metaEl) {
      metaEl.textContent = '全球一手 ' + ((feed.counts && feed.counts.global) || 0) + ' 条 · T0 ' + ((feed.counts && feed.counts.T0) || 0) + ' · T1 ' + ((feed.counts && feed.counts.T1) || 0);
    }

    var events = getFilteredEvents();
    if (!events.length) {
      listEl.innerHTML = '<div class="radar-empty">当前筛选下暂无快讯 · 可点击右上角 <b>↻ 立即巡检</b> 拉取最新中英电报。</div>';
      return;
    }

    listEl.innerHTML = events.map(function (ev) { return renderPlaybookCardHtml(ev, false); }).join('');
  }

  function applyFuturesAccountName(name) {
    var clean = String(name || '').trim();
    if (!clean) return;
    var av = document.getElementById('futuresAvatarLetter');
    var nameEl = document.getElementById('futuresUserBadge');
    var menuName = document.getElementById('futuresMenuAccountName');
    if (nameEl) nameEl.textContent = clean;
    if (menuName) menuName.textContent = clean;
    if (av) av.textContent = clean.slice(0, 1).toUpperCase();
  }

  function wireFuturesAccountMenu() {
    if ((window.location.pathname || '').indexOf('/futures') !== 0) return;
    var btn = document.getElementById('futuresAccountBtn');
    var menu = document.getElementById('futuresAccountMenu');
    var nameEl = document.getElementById('futuresUserBadge');
    if (nameEl && nameEl.textContent.trim()) {
      applyFuturesAccountName(nameEl.textContent.trim());
    } else if (btn) {
      api('/api/me').then(function (r) { return r.ok ? r.json() : null; }).then(function (me) {
        var u = me && (me.user || me.username);
        if (u) applyFuturesAccountName(u);
      }).catch(function () {});
    }
    if (btn && menu && !btn._radarWired) {
      btn._radarWired = true;
      btn.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var open = menu.hidden;
        menu.hidden = !open;
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      document.addEventListener('click', function (ev) {
        if (!menu.hidden && !ev.target.closest('#accountMenuWrap')) {
          menu.hidden = true;
          btn.setAttribute('aria-expanded', 'false');
        }
      });
    }
    var logoutBtn = document.getElementById('futuresMenuLogoutBtn');
    if (logoutBtn && !logoutBtn._radarWired) {
      logoutBtn._radarWired = true;
      logoutBtn.addEventListener('click', async function () {
        await api('/api/logout', { method: 'POST' });
        window.location.href = '/login';
      });
    }
  }

  window.PrismRadar = {
    init: function () {
      if (state.initialized) return;
      state.initialized = true;
      ensureDialogs();
      wireFuturesAccountMenu();
      var params = new URLSearchParams(window.location.search);
      var linkedRadar = params.get('radar');
      if (linkedRadar) state.selectedId = linkedRadar;
      this.fetchFeed();
      state.pollTimer = setInterval(function () { window.PrismRadar.fetchFeed(); }, 30000);
    },

    renderWorkspace: async function () {
      renderWorkspaceView();
      if (!state.feed) {
        await this.fetchFeed();
      }
      renderWorkspaceView();
    },

    openWorkspace: function () {
      this.closeDrawer();
      if (window.App && typeof window.App.switchMode === 'function') {
        window.App.switchMode('radar');
      } else {
        window.location.href = '/prism?mode=radar';
      }
    },

    selectEvent: function (eventId) {
      state.selectedId = eventId || '';
      renderWorkspaceView();
    },

    toggleBriefingBullets: function () {
      state.briefingOpen = !state.briefingOpen;
      renderWorkspaceView();
    },

    fetchFeed: async function () {
      try {
        var r = await api('/api/radar/feed?limit=50&tier=all');
        if (!r.ok) return;
        state.feed = await r.json();
        updateTopbarPill(state.feed);
        var dlg = document.getElementById('radarDrawerDialog');
        if (dlg && dlg.open) renderFeedList();
        var panel = document.getElementById('radarPanel');
        if (panel && panel.style.display !== 'none') renderWorkspaceView();
      } catch (e) {}
    },

    refreshNow: async function () {
      var btns = [document.getElementById('radarRefreshBtn'), document.getElementById('radarSideRefreshBtn')];
      btns.forEach(function (b) { if (b) { b.disabled = true; b.textContent = '巡检中…'; } });
      try {
        var r = await api('/api/radar/refresh', { method: 'POST' });
        if (r.ok) {
          state.feed = await r.json();
          updateTopbarPill(state.feed);
          renderFeedList();
          var panel = document.getElementById('radarPanel');
          if (panel && panel.style.display !== 'none') renderWorkspaceView();
          var okSrc = (state.feed.collector && state.feed.collector.sources_ok) || [];
          toast('已完成全球与国内多路一手电报巡检（' + (okSrc.length || 1) + ' 路信源在线）');
        }
      } catch (e) {
        toast('巡检请求失败，请稍后重试');
      } finally {
        btns.forEach(function (b) { if (b) { b.disabled = false; b.textContent = '↻ 立即巡检'; } });
      }
    },

    generateBriefingNow: async function () {
      var btn = document.getElementById('radarGenBriefBtn');
      if (btn) { btn.disabled = true; btn.textContent = '合成中…'; }
      try {
        var r = await api('/api/radar/briefing/generate', { method: 'POST' });
        var data = await r.json();
        if (r.ok && data.briefing) {
          if (!state.feed) state.feed = {};
          state.feed.briefing = data.briefing;
          state.briefingOpen = true;
          renderWorkspaceView();
          toast('已生成最新《全球跨市场作战简报》');
        } else {
          toast(data.detail || '简报生成未完成');
        }
      } catch (e) {
        toast('简报生成请求异常');
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '↻ 刷新简报'; }
      }
    },

    enrichEvent: async function (eventId) {
      var btn = document.getElementById('radarEnrichBtn-' + eventId);
      if (btn) { btn.disabled = true; btn.textContent = '🌐 溯源推演中…'; }
      try {
        var r = await api('/api/radar/events/' + encodeURIComponent(eventId) + '/enrich', { method: 'POST' });
        var data = await r.json();
        if (!r.ok) {
          toast(data.detail || '全球溯源暂不可用');
          return;
        }
        await this.fetchFeed();
        toast('已完成全球检索求证与剧本更新');
      } catch (e) {
        toast('全球溯源请求异常');
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🌐 全球搜索溯源'; }
      }
    },

    openDrawer: async function () {
      ensureDialogs();
      var dlg = document.getElementById('radarDrawerDialog');
      if (dlg && !dlg.open) dlg.showModal();
      renderFeedList();
      await this.fetchFeed();
      renderFeedList();
    },

    closeDrawer: function () {
      var dlg = document.getElementById('radarDrawerDialog');
      if (dlg && dlg.open) dlg.close();
    },

    setTier: function (tier) {
      state.tierFilter = tier;
      document.querySelectorAll('#radarFilterBar .radar-chip').forEach(function (c) {
        c.classList.toggle('active', c.getAttribute('data-tier') === tier);
      });
      renderFeedList();
      var panel = document.getElementById('radarPanel');
      if (panel && panel.style.display !== 'none') renderWorkspaceView();
    },

    jumpStock: function (ticker, name) {
      this.closeDrawer();
      if (window.App && typeof window.App._selectStock === 'function') {
        window.App.switchMode('market');
        if (window.App._showMarketWorkspace) window.App._showMarketWorkspace('quote');
        window.App._selectStock(ticker, name);
      } else {
        window.location.href = '/prism?mode=market&ticker=' + encodeURIComponent(ticker);
      }
    },

    askAgent: function (eventId) {
      var ev = (state.feed && state.feed.events || []).find(function (item) { return item.id === eventId; });
      if (!ev) return;
      var prompt =
        '刚刚突发【' + ev.tier + ' · ' + ev.theme + '】消息（' + formatShortTime(ev.published_at) + '，来源：' + ev.source + '）：\n' +
        '「' + ev.title + '」\n' +
        '请结合我的投研画像、自选股池及当前盘面时段（' + (ev.window_tag || '') + '），按【核心增量 → 三梯队受益/受损映射 → 开盘/盘中执行节奏与失效红线】给我一份简洁可执行的交易剧本。';
      this.closeDrawer();
      if (window.App && typeof window.App.intervene === 'function') {
        window.App.intervene(prompt, 'prism-research', 'compact');
      } else {
        window.location.href = '/prism?mode=market&copilot=open';
      }
    }
  };

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    window.PrismRadar.init();
  } else {
    document.addEventListener('DOMContentLoaded', function () { window.PrismRadar.init(); });
  }
})();
