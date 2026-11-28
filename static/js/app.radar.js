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
            '<button type="button" class="radar-chip" data-tier="T0" onclick="window.PrismRadar.setTier(\'T0\')"><i class="radar-dot t0"></i>T0 顶层重磅</button>' +
            '<button type="button" class="radar-chip" data-tier="T1" onclick="window.PrismRadar.setTier(\'T1\')"><i class="radar-dot t1"></i>T1 强催化</button>' +
            '<button type="button" class="radar-chip" data-tier="GLOBAL" onclick="window.PrismRadar.setTier(\'GLOBAL\')"><i class="radar-dot global"></i>全球一手</button>' +
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

  function fmtTapePrice(val) {
    var n = Number(val);
    if (!isFinite(n)) return String(val == null ? '-' : val);
    if (Math.abs(n) >= 1000) return String(Math.round(n));
    if (Math.abs(n) >= 100) return n.toFixed(1).replace(/\.0$/, '');
    return n.toFixed(2).replace(/\.00$/, '');
  }

  function renderTapePillHtml(it) {
    var chg = Number(it.change_pct || 0);
    var cls = chg > 0 ? 'up' : (chg < 0 ? 'down' : 'flat');
    var kindCls = it.source_kind === 'watch' ? ' is-watch' : '';
    var sign = chg > 0 ? '+' : '';
    var pctTxt = sign + chg.toFixed(Math.abs(chg) >= 10 ? 1 : 2) + '%';
    var priceTxt = fmtTapePrice(it.price);
    var shortName = String(it.name || '').replace(/主力$/, '').replace(/^离岸人民币$/, '离岸汇率').replace(/^纳斯达克$/, '纳指');
    var clickable = !!(it.jump_kind && it.jump_code);
    var tip = escAttr(it.reason || '');
    var watchDot = it.source_kind === 'watch' ? '<i class="radar-watch-dot" title="自选/底牌命中"></i>' : '';
    if (clickable) {
      return (
        '<button type="button" class="radar-tape-pill ' + cls + kindCls + ' is-clickable" title="' + tip + '" onclick="window.PrismRadar.openTapeTarget(\'' + escAttr(it.jump_kind) + '\',\'' + escAttr(it.jump_code) + '\')">' +
          watchDot +
          '<em>' + esc(shortName) + '</em>' +
          '<b>' + esc(priceTxt) + '</b>' +
          '<small>' + pctTxt + '</small>' +
        '</button>'
      );
    }
    return (
      '<span class="radar-tape-pill ' + cls + kindCls + '"' + (tip ? ' title="' + tip + '"' : '') + '>' +
        watchDot +
        '<em>' + esc(shortName) + '</em>' +
        '<b>' + esc(priceTxt) + '</b>' +
        '<small>' + pctTxt + '</small>' +
      '</span>'
    );
  }

  function renderTapeStripHtml(tape) {
    if (!Array.isArray(tape) || !tape.length) return '';
    var macros = tape.filter(function (x) { return !x.source_kind || x.source_kind === 'macro'; });
    var hot = tape.filter(function (x) { return x.source_kind && x.source_kind !== 'macro'; });
    return (
      '<div class="radar-tape-strip" aria-label="全球跨市场与风向联动价格锚">' +
        '<div class="radar-tape-row">' +
          '<span class="radar-tape-lbl" title="常驻全球跨资产风险温度计">市场基准</span>' +
          '<div class="radar-tape-pills">' + macros.map(renderTapePillHtml).join('') + '</div>' +
        '</div>' +
        (hot.length
          ? '<div class="radar-tape-row radar-tape-row--hot">' +
              '<span class="radar-tape-lbl radar-tape-lbl--hot" title="根据近端 T0/T1 快讯高频提及与你的自选/持仓自动抽取，点击直达行情">热点联动</span>' +
              '<div class="radar-tape-pills">' + hot.map(renderTapePillHtml).join('') + '</div>' +
            '</div>'
          : '') +
      '</div>'
    );
  }

  function renderBriefingBarHtml(briefing) {
    if (!briefing) {
      return (
        '<div class="radar-briefing-bar radar-briefing-bar--empty">' +
          '<div>' +
            '<strong>全球隔夜 & 午间作战早午报</strong>' +
            '<span>每个交易日 08:45（竞价前）与 12:45（午盘前）自动汇总全球宏观/算力链/大宗异动，也可随时手动刷新</span>' +
          '</div>' +
          '<button type="button" class="market-secondary-btn" id="radarGenBriefBtn" onclick="window.PrismRadar.generateBriefingNow()">立即合成全球简报</button>' +
        '</div>'
      );
    }
    var bullets = Array.isArray(briefing.bullets) ? briefing.bullets : [];
    var sources = Array.isArray(briefing.sources) ? briefing.sources : [];
    return (
      '<section class="radar-briefing-bar">' +
        '<div class="radar-briefing-bar__top">' +
          '<div class="radar-briefing-bar__title">' +
            '<span class="radar-global-pill">GLOBAL · ' + esc(briefing.title || '全球作战简报') + '</span>' +
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
    var tierLbl = ev.tier === 'T0' ? 'T0 · 顶层重磅' : (ev.tier === 'T1' ? 'T1 · 强催化' : 'T2 · 快讯');
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
            (isGlobal ? '<span class="radar-global-pill">GLOBAL · 全球一手</span>' : '') +
            '<span class="radar-theme-tag">' + esc(ev.theme || '市场风向') + '</span>' +
            (watchHits.length ? '<span class="radar-watch-hit">命中自选 · ' + esc(watchHits.join('、')) + '</span>' : '') +
            (ai ? '<span class="radar-ai-pill">✓ 全球溯源已核验</span>' : '') +
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
              '<div class="radar-playbook__win">窗口 · ' + esc(pb.window_tag || ev.window_tag || '实时窗口') + '</div>' +
              (ai && ai.verdict ? '<div class="radar-playbook__verdict"><b>全球核验定性：</b>' + esc(ai.verdict) + '</div>' : '') +
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
            ? '<button type="button" class="market-secondary-btn" id="radarEnrichBtn-' + escAttr(ev.id) + '" style="height:29px;font-size:11.5px;" onclick="window.PrismRadar.enrichEvent(\'' + escAttr(ev.id) + '\')">全球搜索溯源</button>'
            : '') +
          '<button type="button" class="copilot-trigger-chip" onclick="window.PrismRadar.askAgent(\'' + escAttr(ev.id) + '\')">交给随行 Agent 结合画像推演</button>' +
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

    var filters = [
      ['all', '全部', counts.total || 0],
      ['T0', '<i class="radar-dot t0"></i>T0', counts.T0 || 0],
      ['T1', '<i class="radar-dot t1"></i>T1', counts.T1 || 0],
      ['GLOBAL', '<i class="radar-dot global"></i>全球', counts.global || 0],
      ['watchlist', '自选', watchCount],
      ['T2', '<i class="radar-dot t2"></i>T2', counts.T2 || 0]
    ];
    if (sideEl) {
      var sideFilters = [
        ['all', '全部快讯', counts.total || 0],
        ['T0', '<i class="radar-dot t0"></i>T0 顶层重磅', counts.T0 || 0],
        ['T1', '<i class="radar-dot t1"></i>T1 产业催化', counts.T1 || 0],
        ['GLOBAL', '<i class="radar-dot global"></i>全球一手源', counts.global || 0],
        ['watchlist', '命中自选', watchCount],
        ['T2', '<i class="radar-dot t2"></i>T2 常规动态', counts.T2 || 0]
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
          sideFilters.map(function (f) {
            return '<button type="button" class="radar-side-filter' + (state.tierFilter === f[0] ? ' active' : '') + '" onclick="window.PrismRadar.setTier(\'' + f[0] + '\')">' +
              '<span>' + f[1] + '</span><b>' + f[2] + '</b></button>';
          }).join('') +
        '</div>';
    }

    var mobileFilterHtml =
      '<div class="radar-mobile-filters" aria-label="手机端风向筛选">' +
        filters.map(function (f) {
          return '<button type="button" class="radar-chip' + (state.tierFilter === f[0] ? ' active' : '') + '" onclick="window.PrismRadar.setTier(\'' + f[0] + '\')">' +
            f[1] + ' ' + f[2] + '</button>';
        }).join('') +
        '<button type="button" class="radar-chip" onclick="window.PrismRadar.refreshNow()">↻ 巡检</button>' +
      '</div>';

    var headerHtml =
      '<div class="radar-warroom__top">' +
        mobileFilterHtml +
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
            (ev.region === 'GLOBAL' ? '<span class="radar-global-tag" title="全球一手源">GLOBAL</span>' : '') +
            '<span class="radar-feed-row__theme">' + esc(ev.source || ev.theme || '') + '</span>' +
            (hasWatch ? '<span class="radar-feed-row__star" title="命中自选">自选</span>' : '') +
            (ai ? '<span class="radar-feed-row__ai" title="已全球溯源">✓</span>' : '') +
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

    openTapeTarget: function (kind, code) {
      if (!code) return;
      this.closeDrawer();
      if (kind === 'futures') {
        window.location.href = '/futures?symbol=' + encodeURIComponent(code);
        return;
      }
      if (kind === 'stock') {
        if (window.App && typeof window.App.switchMode === 'function') {
          window.App._marketWorkspaceTab = 'quote';
          window.App.switchMode('market');
          if (typeof window.App._selectStock === 'function') window.App._selectStock(code);
        } else {
          window.location.href = '/prism';
        }
      }
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
      if (btn) { btn.disabled = true; btn.textContent = '溯源推演中…'; }
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
        if (btn) { btn.disabled = false; btn.textContent = '全球搜索溯源'; }
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

  /* ── Global Per-User Model Preference, Quota & Spend Dialog (shared by /prism & /futures) ── */
  var umState = { data: null, saving: false };

  function ensureUserModelDialog() {
    var dlg = document.getElementById('userModelDialog');
    if (dlg) return dlg;
    dlg = document.createElement('dialog');
    dlg.id = 'userModelDialog';
    dlg.className = 'persona-dialog um-dialog';
    dlg.setAttribute('aria-labelledby', 'userModelDialogTitle');
    dlg.innerHTML =
      '<header class="persona-dialog__hd">' +
        '<div class="persona-dialog__hd-copy">' +
          '<span class="persona-dialog__kicker">GLOBAL MODEL & QUOTA · 全局模型与配额账本</span>' +
          '<h2 id="userModelDialogTitle">全局默认模型与个人开销</h2>' +
          '<p>设置跨板块（行情、期货、随行 Agent）生效的默认模型，并实时查看个人配额与本月开销</p>' +
        '</div>' +
        '<button type="button" class="copilot-icon-btn" onclick="window.PrismUserModel.close()" aria-label="关闭模型与配额窗口">✕</button>' +
      '</header>' +
      '<div class="persona-dialog__body" id="userModelDialogBody"><div class="radar-empty">正在读取可用模型与个人配额…</div></div>';
    dlg.addEventListener('click', function (ev) {
      if (ev.target === dlg && dlg.open) dlg.close();
    });
    document.body.appendChild(dlg);
    return dlg;
  }

  function renderUserModelBody() {
    var body = document.getElementById('userModelDialogBody');
    if (!body) return;
    var d = umState.data;
    if (!d) {
      body.innerHTML = '<div class="radar-empty">暂无配额数据，请稍后重试。</div>';
      return;
    }
    var q = d.quota || {};
    var u = d.usage || {};
    var td = u.today || { calls: 0, cost_usd: 0, cost_cny: 0, input_tokens: 0, output_tokens: 0 };
    var mo = u.month || { calls: 0, cost_usd: 0, cost_cny: 0, input_tokens: 0, output_tokens: 0 };
    var models = Array.isArray(d.available_models) ? d.available_models : [];
    var pref = d.preferred_model || '';

    var quotaMainHtml = q.unlimited
      ? '<strong class="um-unlimited">∞ 无上限</strong><small>特权账号 · 不受日调用与月度预算硬限制（仍透明记录开销）</small>'
      : (
          '<strong>' + Number(q.daily_calls_used || 0) + ' / ' + Number(q.effective_daily_calls_limit || 200) + ' 次</strong>' +
          '<small>' +
            (q.daily_calls_limit ? '专属每日限额' : '跟随全站每日限额') +
            (q.monthly_budget_usd != null ? (' · 月预算 $' + Number(q.monthly_budget_usd).toFixed(2)) : ' · 月预算无上限') +
          '</small>'
        );

    var progressHtml = '';
    if (!q.unlimited && q.effective_daily_calls_limit) {
      var pct = Math.min(100, Math.round((Number(q.daily_calls_used || 0) / Number(q.effective_daily_calls_limit)) * 100));
      progressHtml = '<div class="um-progress"><i style="width:' + pct + '%"></i></div>';
    }

    var cardsHtml =
      '<button type="button" class="um-model-card' + (!pref ? ' active' : '') + '" onclick="window.PrismUserModel.selectModel(\'\')">' +
        '<div class="um-model-card__top"><strong>跟随平台默认推荐</strong><span class="um-vendor-pill">' + (!pref ? '当前生效' : '默认') + '</span></div>' +
        '<div class="um-model-card__meta">当前推荐：<code>' + esc(d.platform_default_model || '自动') + '</code></div>' +
        '<div class="um-model-card__note">自动跟随管理员在模型路由中指定的当前主力模型</div>' +
      '</button>' +
      models.map(function (m) {
        var on = pref === m.id;
        var priceTxt = (m.input_usd_per_1m != null) ? ('$' + m.input_usd_per_1m + ' / $' + m.output_usd_per_1m + ' per 1M') : '按官方计费';
        return (
          '<button type="button" class="um-model-card' + (on ? ' active' : '') + '" onclick="window.PrismUserModel.selectModel(\'' + escAttr(m.id) + '\')">' +
            '<div class="um-model-card__top"><strong>' + esc(m.name || m.id) + '</strong><span class="um-vendor-pill">' + esc(m.vendor || '') + '</span></div>' +
            '<div class="um-model-card__meta"><code>' + esc(m.id) + '</code> · ' + esc(m.context_window || '—') + ' · ' + esc(priceTxt) + '</div>' +
            (m.note ? '<div class="um-model-card__note">' + esc(m.note) + '</div>' : '') +
          '</button>'
        );
      }).join('');

    var byModel = Array.isArray(u.by_model_month) ? u.by_model_month : [];
    var breakdownHtml = byModel.length
      ? '<div class="um-breakdown">' +
          '<div class="um-section-lbl">本月各模型调用明细</div>' +
          '<div class="um-breakdown-list">' +
            byModel.map(function (b) {
              return '<div class="um-breakdown-row"><code>' + esc(b.model_id) + '</code><span>' + Number(b.calls || 0) + ' 次 · $' + Number(b.cost_usd || 0).toFixed(4) + '</span></div>';
            }).join('') +
          '</div>' +
        '</div>'
      : '';

    body.innerHTML =
      '<div class="um-kpi-grid">' +
        '<div class="um-kpi-card"><span>我的配额级别</span>' + quotaMainHtml + progressHtml + '</div>' +
        '<div class="um-kpi-card"><span>今日调用与估算开销</span><strong>$' + Number(td.cost_usd || 0).toFixed(4) + '</strong><small>约 ¥' + Number(td.cost_cny || 0).toFixed(2) + ' · 今日 ' + Number(td.calls || 0) + ' 次</small></div>' +
        '<div class="um-kpi-card"><span>本月累计估算开销</span><strong>$' + Number(mo.cost_usd || 0).toFixed(4) + '</strong><small>约 ¥' + Number(mo.cost_cny || 0).toFixed(2) + ' · 共 ' + Number(mo.calls || 0) + ' 次</small></div>' +
      '</div>' +
      '<div class="um-section-hd">' +
        '<div class="um-section-lbl">选择我的全局默认模型（共 ' + models.length + ' 个已激活可用模型）</div>' +
        '<span class="um-effective-tag">当前生效：<code>' + esc(d.effective_model || '未配置') + '</code></span>' +
      '</div>' +
      (models.length
        ? '<div class="um-model-grid">' + cardsHtml + '</div>'
        : '<div class="radar-empty">管理员尚未配置可用模型密钥，请先在运维台配置服务商凭证。</div>') +
      breakdownHtml;
  }

  window.PrismUserModel = {
    open: async function () {
      var menu = document.getElementById('futuresAccountMenu');
      if (menu) menu.hidden = true;
      var dlg = ensureUserModelDialog();
      if (!dlg.open) dlg.showModal();
      var body = document.getElementById('userModelDialogBody');
      if (body && !umState.data) body.innerHTML = '<div class="radar-empty">正在读取可用模型与个人配额…</div>';
      await this.refresh();
    },
    close: function () {
      var dlg = document.getElementById('userModelDialog');
      if (dlg && dlg.open) dlg.close();
    },
    refresh: async function () {
      try {
        var r = await api('/api/user/model-settings');
        if (!r.ok) return;
        umState.data = await r.json();
        renderUserModelBody();
      } catch (e) {}
    },
    selectModel: async function (modelId) {
      if (umState.saving) return;
      umState.saving = true;
      try {
        var r = await api('/api/user/model-settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ preferred_model: modelId || '' })
        });
        var data = await r.json().catch(function () { return {}; });
        if (!r.ok) {
          toast(data.detail || '模型保存失败');
          return;
        }
        umState.data = data;
        renderUserModelBody();
        var eff = data.effective_model || '';
        if (window.App && eff) {
          window.App.model = eff;
          var sel = document.getElementById('modelSelect');
          if (sel) sel.value = eff;
        }
        if (window.PrismFuturesCopilot && typeof window.PrismFuturesCopilot.setModel === 'function' && eff) {
          window.PrismFuturesCopilot.setModel(eff);
        }
        toast(modelId ? ('全局默认模型已切换为 ' + eff) : ('已恢复跟随平台默认模型（' + eff + '）'));
      } catch (e) {
        toast('网络异常，模型偏好未保存');
      } finally {
        umState.saving = false;
      }
    }
  };

  /* ═══════════════════════════════════════════════════════════
     Prism Plain-Language Hover Glossary (/prism + /futures)
     Zero-mutation attribute annotator + instant floating popover
     ═══════════════════════════════════════════════════════════ */
  var PRISM_GLOSSARY = {
    'PE(TTM)': { t: 'PE(TTM) · 滚动市盈率', d: '市值 ÷ 最近连续 4 个季度净利润总和，反映“按现在赚钱速度，回本需要几年”。比静态市盈率更能反映最新业绩变化。' },
    'PE(静态)': { t: 'PE(静态) · 静态市盈率', d: '总市值 ÷ 上一完整财年净利润。遇到业绩大增或大降的年份容易失真，建议结合 PE(TTM) 一起看。' },
    '市盈率': { t: '市盈率（PE）', d: '股价 ÷ 每股收益，常用来衡量股票“贵不贵”。一般同行业横向对比最有意义。' },
    'PB': { t: 'PB · 市净率', d: '总市值 ÷ 净资产（公司账面家底）。银行、地产、周期股常看 PB；PB < 1 俗称“破净”。' },
    '市净率': { t: '市净率（PB）', d: '股价 ÷ 每股净资产，衡量买入公司账面净资产的溢价倍数。' },
    '总市值': { t: '总市值', d: '当前股价 × 公司全部股份总数，代表按现价买下整家公司的总价格，用来区分大盘股/中小盘股。' },
    '流通市值': { t: '流通市值', d: '当前股价 × 可在二级市场自由买卖的股份数（剔除限售股），反映真实筹码盘子大小。' },
    '换手率': { t: '换手率', d: '当日成交股数 ÷ 流通总股数。越高说明资金买卖越活跃、筹码交换越剧烈；极高换手往往意味着分歧加大。' },
    '振幅': { t: '日内振幅', d: '（最高价 − 最低价）÷ 昨收价，衡量当天价格上下波动有多剧烈。' },
    '量比': { t: '量比', d: '当前成交速度 ÷ 过去 5 日平均成交速度。大于 1 说明放量，大于 2~3 说明资金明显异动。' },
    '成交量': { t: '成交量', d: '一段时间内买卖双方成交的股票手数（1 手 = 100 股）或期货合约手数。' },
    '成交额': { t: '成交额', d: '成交量 × 成交价格的总金额，最直观反映真金白银的活跃度与流动性。' },
    '最低成交额': { t: '最低成交额过滤', d: '剔除日成交额太低、流动性差的冷门股，避免选出买不进或卖不出的标的。' },
    'MA5': { t: 'MA5 · 5 日均线', d: '最近 5 个交易日收盘价的平均值，代表极短线（1 周）资金的平均持仓成本。' },
    'MA10': { t: 'MA10 · 10 日均线', d: '最近 10 个交易日收盘价平均值，常作为短线强势股的回调支撑参考线。' },
    'MA20': { t: 'MA20 · 20 日均线', d: '约 1 个月的平均成本线（俗称生命线），股价站稳其上通常视为中期偏多。' },
    'MA60': { t: 'MA60 · 60 日均线', d: '约 1 个季度的平均成本线（季线），用来判断中长期大趋势方向。' },
    '前复权': { t: '前复权（看盘推荐）', d: '保持当前最新股价不变，把历史上因分红/送股造成的跳空缺口向前平滑处理，最适合技术分析和画均线。' },
    '后复权': { t: '后复权', d: '保持上市初期价格不变，把历年分红送股累加上去，最适合看长线持有至今的真实累计涨幅。' },
    '不复权': { t: '不复权', d: '直接展示当年原始成交价，分红除权日会出现真实价格跳空缺口。' },
    '52周区间': { t: '52 周价格区间', d: '过去一年里的最低价到最高价范围，帮你一眼判断当前股价处于年内高位还是低位。' },
    '股息率': { t: '股息率', d: '年度每股分红 ÷ 当前股价，类似“把钱买成股票吃分红的年化利息”。' },
    'ROE': { t: 'ROE · 净资产收益率', d: '净利润 ÷ 净资产，衡量公司“用股东的钱赚钱的效率”。长期稳定 >15% 通常是优质白马特征。' },
    '回看 K 线': { t: '回看 K 线根数', d: '指标计算与选股扫描时向前读取的历史交易日数量（例如 120 根约等于半年行情）。' },
    '触发窗口': { t: '信号触发窗口', d: '只筛选最近 N 个交易日内刚刚出现买入/异动信号的股票，过滤掉很久以前的老信号。' },
    '排除 ST 与退市整理股票': { t: '排除 ST 风险警示股', d: 'ST / *ST 代表公司财务或经营存在退市风险警示，勾选后全市场选股会自动跳过这类高风险标的。' },
    '主板': { t: '沪深主板', d: '上交所（60 开头）与深交所（00 开头）成熟大型企业板块，日涨跌幅限制一般为 ±10%。' },
    '创业板': { t: '深交所创业板（300 开头）', d: '以成长型创新企业为主的板块，日涨跌幅限制为 ±20%。' },
    '科创板': { t: '上交所科创板（688 开头）', d: '硬科技与半导体等科创企业板块，日涨跌幅限制为 ±20%。' },
    '北交所': { t: '北京证券交易所（8/4/92 开头）', d: '服务专精特新中小企业的板块，日涨跌幅限制为 ±30%。' },
    '现货基差': { t: '基差（现货价 − 期货价）', d: '正数代表“期货贴水”（期货比现货便宜，多头有贴水保护垫）；负数代表“期货升水”（期货比现货贵）。' },
    '基差': { t: '基差（现货价 − 期货价）', d: '衡量期货和现货的价差：基差 > 0 叫期货贴水（期货偏便宜），基差 < 0 叫期货升水（期货偏贵）。' },
    '基差率（基差 / 现货价）': { t: '基差率（百分比）', d: '把基差除以现货价折成百分比，方便跨品种、跨年份对比期货贴水或升水有多深。' },
    '仓单库存': { t: '交易所注册仓单', d: '已存放在交易所指定仓库、随时可用于交割的现货数量。仓单大幅减少（去库）往往偏利多，激增（累库）偏承压。' },
    '仓单': { t: '交易所仓单', d: '交易所仓库里注册登记、可用于交割的现货库存量，是观察实物紧缺与否的高频窗口。' },
    '期限结构': { t: '期限结构（近月 vs 远月价差）', d: '对比近月与远月合约价格：近高远低（远月贴水）常说明当下现货紧缺；近低远高（远月升水）说明当下供应偏宽松。' },
    '会员持仓': { t: '交易所前 20 会员持仓排名', d: '交易所每日盘后披露的前 20 大期货公司多空持仓与增减变化，反映主力机构资金动向（注意不等于散户净流向）。' },
    '多头持仓': { t: '多头持仓', d: '看涨并持有买入合约的总手数。' },
    '空头持仓': { t: '空头持仓', d: '看跌（或套期保值卖出）并持有卖出合约的总手数。' },
    '持仓量变化': { t: '持仓量日增减', d: '增仓上涨说明新多头主动入场，减仓上涨多为空头止损平仓，是判断行情持续性的关键指标。' },
    '合约乘数': { t: '合约乘数（每手对应实物量）', d: '1 手期货合约代表的现货单位数（如螺纹钢 10 吨/手、黄金 1000 克/手），用来算合约总价值和盈亏。' },
    '投机多头保证金（%）': { t: '投机保证金比例', d: '交易所要求的最低押金比例（例如 10% ≈ 10 倍杠杆）。实际期货公司通常还会在此基础上加收 2%~5%。' },
    '投机空头保证金（%）': { t: '投机保证金比例', d: '做空 1 手合约按交易所标准需要冻结的最低保证金占比。' },
    '历史高位拥挤区（≥82%分位）': { t: '历史高位拥挤区（≥82% 分位）', d: '当前价格高于过去 5 年里 82% 以上的交易日，追多性价比偏低，需警惕高位回撤或基本面证伪。' },
    '历史低位安全垫（≤18%分位）': { t: '历史低位安全垫（≤18% 分位）', d: '当前价格低于过去 5 年里 82% 的交易日，估值接近历史底部区间，往往具备较高的赔率与安全边际。' },
    'T0 顶层核弹': { t: 'T0 · 顶层重磅级信号', d: '央行、国务院、美联储、关税/出口管制等可能改变大盘或行业大方向的最高级别事件，会优先推送强提醒。' },
    'T1 产业催化': { t: 'T1 · 产业强催化信号', d: '停产、涨价、业绩大幅预增、巨头资本开支/新品突破等具体行业和龙头催化事件。' },
    'T2 常规动态': { t: 'T2 · 常规市场快讯', d: '日常滚动财经资讯，适合盘中顺手浏览线索，不建议单独作为追涨杀跌依据。' },
    '全球一手源': { t: '全球一手权威信源', d: '直采美联储（Fed）、CNBC 全球与科技、美国 SEC 8-K 公告等海外一手消息源，提前捕捉隔夜外盘映射。' },
    '离岸人民币': { t: '离岸人民币（USD/CNH）', d: '1 美元兑离岸人民币汇率。数值下跌代表人民币升值（通常利好外资回流 A 股/港股）；上涨代表人民币贬值。' },
    '沪深300': { t: '沪深 300 指数', d: 'A 股最具代表性的 300 只大盘蓝筹股综合指数，用来看全市场核心权重风向。' },
    '恒生科指': { t: '恒生科技指数', d: '港股核心互联网与科技龙头指数，对中国科技股与风险偏好反应最敏感。' },
    '纳斯达克': { t: '纳斯达克综合指数', d: '美股科技成长股核心风向标，直接影响次日 A 股 AI 算力、光模块与半导体开盘情绪。' },
    '已印证支持': { t: '已印证支持', d: '后续真实市场行情达到了你当初设定的目标方向或涨幅门槛，证明本次判断正确。' },
    '触发失效证伪': { t: '触发失效证伪', d: '真实行情跌破（看多时）或突破（看空时）你预设的失效止损线，系统自动标记为证伪并反哺复盘画像。' },
    '区间观察中': { t: '区间观察中', d: '已拉取最新报价核对，价格目前还在目标位与失效线之间震荡，尚未决出胜负。' },
    '待对账': { t: '待对账', d: '尚未触发自动收盘核验或还未到观察窗口，可随时点击「按最新行情对账」立即核查。' },
    '已对账印证率': { t: '已对账印证率（胜率）', d: '已印证支持次数 ÷（已印证 + 已证伪总次数），用来客观衡量长期多空判断准确率。' },
    '投研画像': { t: '个人投研画像', d: '系统记住你的投资风格、仓位纪律和历史偏差，让随行 Agent 每次分析都贴合你的真实风险偏好。' },
    '∞ 无上限': { t: '∞ 无上限特权配额', d: '管理员为你的账号开通了免限流特权：不限制每日调用次数和月度预算，同时仍透明展示消耗明细。' },
    '上下文': { t: '上下文记忆长度', d: '模型单次对话最多能同时读取多少字（如 1M ≈ 50~70 万汉字），越大越适合一次分析长财报和长对话。' }
  };

  function matchPrismGlossary(rawText) {
    var txt = String(rawText || '').replace(/\s+/g, ' ').trim();
    if (!txt || txt.length > 34) return null;
    if (PRISM_GLOSSARY[txt]) return PRISM_GLOSSARY[txt];
    var clean = txt
      .replace(/^[\u2713\u2715\u25ce\u2605\u2726\u23f3\ud83d\udd34\ud83d\udfe0\ud83c\udf10\u26a0\s]+/, '')
      .replace(/[：:]\s*$/, '')
      .trim();
    if (PRISM_GLOSSARY[clean]) return PRISM_GLOSSARY[clean];
    if (/^T0(\s|$)/.test(clean)) return PRISM_GLOSSARY['T0 顶层核弹'];
    if (/^T1(\s|$)/.test(clean)) return PRISM_GLOSSARY['T1 产业催化'];
    if (/^T2(\s|$)/.test(clean)) return PRISM_GLOSSARY['T2 常规动态'];
    if (/分位$/.test(clean) && /\d+%/.test(clean)) {
      return { t: '历史价格分位', d: '把当前价格放进过去 5 年所有交易日里排队：0% 是 5 年最低点，100% 是 5 年最高点，50% 在中位数附近。' };
    }
    if (/^同期\s*\d+%/.test(clean)) {
      return { t: '历年同期季节性分位', d: '只和过去几年“同一个月份/季节”的历史价格比高低，专门剔除季节性淡旺季干扰。' };
    }
    if (/^持仓\s*\d+%/.test(clean)) {
      return { t: '持仓拥挤分位', d: '当前全市场总持仓量在近 5 年里的排名分位。越高说明资金博弈越拥挤，波动往往更大。' };
    }
    if (/基差贴水|期货贴水/.test(clean)) {
      return { t: '期货贴水（现货 > 期货）', d: '期货价格比现货便宜，到期交割逻辑下多头往往有“贴水修复”的安全垫保护。' };
    }
    if (/基差升水|期货升水/.test(clean)) {
      return { t: '期货升水（期货 > 现货）', d: '期货价格比现货更贵，说明市场提前透支了涨价预期，追多需防现货跟不上引发回调。' };
    }
    if (/仓单去库|去库/.test(clean)) {
      return { t: '仓单去库（库存下降）', d: '交易所注册仓单持续减少，通常意味着现货提货积极或供应偏紧，对价格偏利多。' };
    }
    if (/仓单累库|累库/.test(clean)) {
      return { t: '仓单累库（库存上升）', d: '交易所注册仓单持续增加，意味着可交割货源充裕或下游承接偏弱，对价格偏压制。' };
    }
    if (/远月升水|Contango/i.test(clean)) {
      return { t: '远月升水（Contango）', d: '远期合约比近期合约贵，通常说明当下现货供应宽松、仓库充裕。' };
    }
    if (/远月贴水|Backwardation/i.test(clean)) {
      return { t: '远月贴水（Backwardation）', d: '近期合约比远期合约贵，通常说明眼下现货紧缺，买方愿意加价抢近月货。' };
    }
    return null;
  }

  var _gPop = null, _gAnchor = null, _gTimer = null;
  var _SKIP_TAGS = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, OPTION: 1, SVG: 1, CANVAS: 1, CODE: 1, PRE: 1 };

  function ensurePrismTipPop() {
    if (_gPop && _gPop.parentNode) return _gPop;
    if (!document.body) return null;
    _gPop = document.createElement('div');
    _gPop.id = 'prismTipPop';
    _gPop.setAttribute('role', 'tooltip');
    _gPop.setAttribute('aria-hidden', 'true');
    document.body.appendChild(_gPop);
    return _gPop;
  }

  function hidePrismTip() {
    if (_gPop) {
      _gPop.classList.remove('show');
      _gPop.setAttribute('aria-hidden', 'true');
    }
    _gAnchor = null;
  }

  function showPrismTip(target) {
    if (!target) return;
    var desc = target.getAttribute('data-prism-tip-d');
    if (!desc) return;
    var pop = ensurePrismTipPop();
    if (!pop) return;
    _gAnchor = target;
    var title = target.getAttribute('data-prism-tip-t') || target.textContent || '';
    pop.innerHTML = '<div class="prism-tip-hd">' + esc(title) + '</div><div class="prism-tip-bd">' + esc(desc) + '</div>';
    pop.style.left = '-9999px';
    pop.style.top = '-9999px';
    pop.classList.add('show');
    pop.setAttribute('aria-hidden', 'false');
    var rect = target.getBoundingClientRect();
    var pw = pop.offsetWidth || 260, ph = pop.offsetHeight || 72;
    var left = rect.left + rect.width / 2 - pw / 2;
    left = Math.max(10, Math.min((window.innerWidth || 1024) - pw - 10, left));
    var top = rect.top - ph - 8;
    if (top < 10) top = rect.bottom + 8;
    if (top + ph > (window.innerHeight || 768) - 8) top = Math.max(10, (window.innerHeight || 768) - ph - 8);
    pop.style.left = Math.round(left) + 'px';
    pop.style.top = Math.round(top) + 'px';
  }

  function annotatePrismGlossary(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    var nodes = root.querySelectorAll('span,th,dt,label,h2,h3,h4,em,b,strong,button');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!el || el.children.length > 0 || _SKIP_TAGS[el.tagName]) continue;
      if (el.id === 'prismTipPop') continue;
      if (el.closest && el.closest('.msg-body,.reports-lib-md,#prismTipPop')) continue;
      var hit = matchPrismGlossary(el.textContent);
      if (hit) {
        if (!el.classList.contains('prism-tip-host')) el.classList.add('prism-tip-host');
        if (el.getAttribute('data-prism-tip-t') !== hit.t) {
          el.setAttribute('data-prism-tip-t', hit.t);
          el.setAttribute('data-prism-tip-d', hit.d);
        }
      } else if (el.classList.contains('prism-tip-host')) {
        el.classList.remove('prism-tip-host');
        el.removeAttribute('data-prism-tip-t');
        el.removeAttribute('data-prism-tip-d');
      }
    }
  }

  function initPrismGlossary() {
    if (!document.body || typeof document.addEventListener !== 'function') return;
    if (window.__prismGlossaryBound) return;
    window.__prismGlossaryBound = true;
    annotatePrismGlossary(document.body);
    document.addEventListener('mouseover', function (ev) {
      var t = ev.target && ev.target.closest ? ev.target.closest('.prism-tip-host') : null;
      if (t) { if (t !== _gAnchor) showPrismTip(t); }
      else if (_gAnchor) hidePrismTip();
    });
    document.addEventListener('mouseout', function (ev) {
      if (!_gAnchor) return;
      var rel = ev.relatedTarget;
      if (!rel || !rel.closest || rel.closest('.prism-tip-host') !== _gAnchor) hidePrismTip();
    });
    document.addEventListener('focusin', function (ev) {
      var t = ev.target && ev.target.closest ? ev.target.closest('.prism-tip-host') : null;
      if (t) showPrismTip(t);
    });
    document.addEventListener('focusout', function () { hidePrismTip(); });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') hidePrismTip(); });
    window.addEventListener('scroll', hidePrismTip, true);
    if (typeof MutationObserver === 'function') {
      var mo = new MutationObserver(function () {
        if (_gTimer) return;
        _gTimer = setTimeout(function () {
          _gTimer = null;
          annotatePrismGlossary(document.body);
        }, 160);
      });
      mo.observe(document.body, { childList: true, subtree: true });
    }
  }

  window.PrismGlossary = { scan: annotatePrismGlossary, hide: hidePrismTip };

  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    window.PrismRadar.init();
    initPrismGlossary();
  } else {
    document.addEventListener('DOMContentLoaded', function () {
      window.PrismRadar.init();
      initPrismGlossary();
    });
  }
})();
