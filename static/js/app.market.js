/* ═══════════════════════════════════════
   Prism — Application Logic (v0.8 split)
   ═══════════════════════════════════════ */

Object.assign(App, {
  _marketSearchDebounce: null,
  _currentKlineLimit: 120,
  _klinePeriodLimits: {'1d':120, '1w':104, '1m':60},
  _klineMaVisible: {MA5:true, MA10:true, MA20:true, MA60:false},
  openMarket: function() {
    var modal = document.getElementById('marketModal');
    if (!modal) return;
    modal.style.display = 'flex';
    var q = document.getElementById('marketQ');
    if (q) { q.value = ''; q.focus(); }
    var ul = document.getElementById('marketResults');
    if (ul) ul.innerHTML = '';
    var qv = document.getElementById('marketQuoteView');
    if (qv) qv.style.display = 'none';
  },
  closeMarket: function() {
    var modal = document.getElementById('marketModal');
    if (modal) modal.style.display = 'none';
  },
  _marketSearchInput: function(e) {
    var self = this;
    clearTimeout(this._marketSearchDebounce);
    var q = (e.target.value || '').trim();
    var ul = document.getElementById('marketResults');
    var qv = document.getElementById('marketQuoteView');
    if (qv) qv.style.display = 'none';
    if (!q) { if (ul) ul.innerHTML = ''; return; }
    this._marketSearchDebounce = setTimeout(function() { self._marketRunSearch(q); }, 150);
  },
  _marketRunSearch: async function(q) {
    var ul = document.getElementById('marketResults');
    if (!ul) return;
    var r = await this.api('/api/search?q=' + encodeURIComponent(q) + '&limit=10');
    ul.innerHTML = '';
    if (!r.ok) { ul.innerHTML = '<li class="market-empty">搜索失败，请重试</li>'; return; }
    var data = await r.json();
    var hits = data.hits || [];
    if (!hits.length) { ul.innerHTML = '<li class="market-empty">无匹配结果</li>'; return; }
    var self = this;
    // Cache ticker→name for quote-chip click handler
    for (var i = 0; i < hits.length; i++) { self._tickerNames[hits[i].ticker] = hits[i].name; }
    for (var i = 0; i < hits.length; i++) {
      (function(h) {
        var li = document.createElement('li');
        li.className = 'market-hit';
        li.innerHTML = '<span class="market-hit__code">' + esc(h.ticker) + '</span>' +
                       '<span class="market-hit__name">' + esc(h.name) + '</span>' +
                       (h.market ? '<span class="market-hit__mkt">' + esc(h.market) + '</span>' : '');
        li.addEventListener('click', function() { self._marketShowQuote(h.ticker, h.name); });
        ul.appendChild(li);
      })(hits[i]);
    }
  },
  _marketShowQuote: async function(ticker, name) {
    var qv = document.getElementById('marketQuoteView');
    if (!qv) return;
    qv.style.display = 'block';
    qv.innerHTML = '<div class="market-loading">查询 ' + esc(ticker) + ' 中…</div>';
    var r = await this.api('/api/quote/' + encodeURIComponent(ticker));
    if (!r.ok) {
      qv.innerHTML = '<div class="market-error">暂无 ' + esc(ticker) + ' 的行情数据</div>';
      return;
    }
    var q = await r.json();
    var price = (q.price == null) ? '—' : Number(q.price).toFixed(q.price >= 100 ? 2 : 3);
    var chg = (q.change == null) ? '—' : Number(q.change).toFixed(3);
    var pct = (q.change_pct == null) ? '—' : Number(q.change_pct).toFixed(2);
    var cls = (q.change_pct != null && Number(q.change_pct) >= 0) ? 'up' : 'down';
    var staleTag = q.stale ? '<span class="market-stale">·过期数据</span>' : '';
    qv.innerHTML = '<div class="market-qv-row"><span class="market-qv-code">' + esc(ticker) + '</span>' +
                   '<span class="market-qv-name">' + esc(name || '') + '</span></div>' +
                   '<div class="market-qv-row"><span class="market-qv-price">' + price + '</span>' +
                   '<span class="market-qv-chg market-qv-chg--' + cls + '">' + chg + ' / ' + pct + '%</span>' +
                   staleTag + '</div>' +
                   '<div class="market-qv-row market-qv-src">来源: ' + esc(q.source || '—') + ' · ' +
                   esc(this._quoteTimeLabel(q)) + '</div>';
  },

  /* ═══ v0.7 — 独立行情面板 ═══ */
  _mode: 'chat',
  _watchlist: [],
  _currentTicker: null,
  _currentName: '',
  _currentPeriod: '1d',
  _klineChart: null,
  _watchlistTimer: null,
  _marketSidebarDebounce: null,
  _klineResizeBound: false,
  _tickerNames: {},   // ticker → name cache, populated from search results

  switchMode: function(mode) {
    if (mode === this._mode) return;
    var prev = this._mode;
    this._mode = mode;
    var telemetryFeature = mode === 'reports' ? 'report' : mode;
    if (this._trackUiEvent && (telemetryFeature !== 'admin' || this.isAdmin)) {
      this._trackUiEvent(telemetryFeature, 'open');
    }
    if (prev === 'industry' && mode !== 'industry') this._leaveIndustry();
    var shell = document.getElementById('appShell');
    if (shell) shell.setAttribute('data-mode', mode);
    var compact = this._isCompactViewport && this._isCompactViewport();
    var needsDrawer = mode !== 'chat' && mode !== 'industry';
    var btns = document.querySelectorAll('.icon-rail-btn');
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle('active', btns[i].getAttribute('data-mode') === mode);
    }
    var adminEntry = document.getElementById('adminEntry');
    if (adminEntry) adminEntry.classList.toggle('active', mode === 'admin');
    if (mode === 'market') {
      if (this._initMarketWorkbench) this._initMarketWorkbench();
      this._loadWatchlist();
      this._renderWatchlist();
      this._startWatchlistRefresh();
      this._loadIndicators();
      var sq = document.getElementById('marketSidebarQ');
      if (sq) setTimeout(function() { sq.focus(); }, 50);
      if (!this._currentTicker) this._showMarketEmpty();
      this._warmupSearch();
    } else if (mode === 'analysis') {
      this._stopWatchlistRefresh();
      // Ensure sidebar is visible (don't auto-collapse).
      if (shell && !compact) shell.classList.remove('collapsed');
      var aq = document.getElementById('analysisQ');
      if (aq) setTimeout(function() { aq.focus(); }, 50);
      // Try to restore a running/completed analysis (survives page refresh)
      this._restoreAnalysisState();
      if (!this._analysisId && !this._analysisTicker) {
        document.getElementById('analysisPanelInner').innerHTML = '<div class="market-empty-state">从左侧搜索股票，点击"一键分析"生成结构化分析报告</div>';
      }
      this._warmupSearch();
    } else if (mode === 'reports') {
      this._stopWatchlistRefresh();
      if (shell && !compact) shell.classList.remove('collapsed');
      this._initReports();
    } else if (mode === 'vibe') {
      this._stopWatchlistRefresh();
      if (shell && !compact) shell.classList.remove('collapsed');
      this._initVibe();
    } else if (mode === 'industry') {
      this._stopWatchlistRefresh();
      this._initIndustry();
    } else if (mode === 'admin') {
      this._stopWatchlistRefresh();
      if (shell && !compact) shell.classList.remove('collapsed');
      this._initAdmin();
    } else {
      this._stopWatchlistRefresh();
    }
    if (shell && compact) shell.classList.toggle('mobile-sidebar-open', needsDrawer);
  },

  _showMarketEmpty: function() {
    var inner = document.getElementById('marketPanelInner');
    if (inner) inner.innerHTML = '<div class="market-empty-workbench"><strong>选择一只股票开始复盘</strong><span>搜索股票或选择已有自选，也可以从全市场指标选股开始。</span><div><button type="button" onclick="App._openMarketSearch()">搜索或选择股票</button><button type="button" onclick="App._showMarketWorkspace(\'screener\')">打开全市场选股</button><button type="button" onclick="App._showMarketWorkspace(\'library\')">配置指标库</button></div></div>';
  },

  _openMarketSearch: function() {
    var shell = document.getElementById('appShell');
    if (this._isCompactViewport()) this.toggleSidebar(true);
    else if (shell) shell.classList.remove('collapsed');
    var input = document.getElementById('marketSidebarQ');
    if (input) input.focus();
  },

  // ── 自选列表（localStorage） ──
  _normalizeTicker: function(ticker) {
    var t = String(ticker || '').trim().toUpperCase().replace(/\s+/g, '');
    var prefixed = t.match(/^(SH|SZ|BJ)(\d{6})$/);
    if (prefixed) return prefixed[2];
    var suffixed = t.match(/^(\d{6})\.(SH|SZ|BJ)$/);
    if (suffixed) return suffixed[1];
    return t;
  },
  _normalizeWatchEntry: function(item) {
    if (!item) return null;
    var ticker = this._normalizeTicker(item.ticker || item.code || item.symbol || item);
    if (!ticker) return null;
    return {
      ticker: ticker,
      name: String(item.name || item.title || ticker).trim() || ticker,
      price: item.price == null ? null : item.price,
      pct: item.pct == null ? (item.change_pct == null ? null : item.change_pct) : item.pct
    };
  },
  _loadWatchlist: async function() {
    var owner = this.user || '';
    if (this._watchlistOwner !== owner) { this._watchlist = []; this._watchlistOwner = owner; }
    var sequence = this._watchlistReadSeq = (this._watchlistReadSeq || 0) + 1;
    try {
      var response = await this.api('/api/watchlist');
      if (!response.ok) throw new Error('unavailable');
      var data = await response.json();
      if (owner !== this.user || sequence !== this._watchlistReadSeq) return false;
      this._watchlist = (data.items || []).map(this._normalizeWatchEntry.bind(this)).filter(Boolean);
      this._renderWatchlist(); this._refreshWatchlist(); return true;
    } catch (error) {
      if (owner === this.user && sequence === this._watchlistReadSeq) this._toast('账号自选暂时无法同步，请稍后重试');
      return false;
    }
  },
  _mutateWatchlist: function(mutate, activityAction) {
    var self = this, owner = this.user;
    var task = (this._watchlistMutationQueue || Promise.resolve()).catch(function() {}).then(async function() {
      if (owner !== self.user) return null;
      // Read the account version immediately before editing; never import another user's browser list.
      var response = await self.api('/api/watchlist');
      if (!response.ok) throw new Error('unavailable');
      var current = (await response.json()).items || [];
      if (owner !== self.user) return null;
      var changed = mutate(current);
      if (changed === false) { self._watchlist = current; self._renderWatchlist(); return false; }
      var saved = await self.api('/api/watchlist', {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:current.map(function(item){return {ticker:item.ticker,name:item.name};})})});
      if (!saved.ok) throw new Error('save failed');
      var data = await saved.json();
      if (owner !== self.user) return null;
      self._watchlistReadSeq = (self._watchlistReadSeq || 0) + 1;
      self._watchlist = data.items || []; self._watchlistOwner = owner;
      self._renderWatchlist(); self._refreshWatchlist();
      // Count confirmed actions only; stock codes, names and the account list stay private.
      if (self._trackUiEvent && activityAction) {
        try { self._trackUiEvent('market', activityAction); } catch (error) {}
      }
      return true;
    }).catch(function() { if (owner === self.user) self._toast('自选未保存成功；服务端原记录保留，请重试'); return null; });
    this._watchlistMutationQueue = task; return task;
  },
  _findWatchIndex: function(ticker) {
    ticker = this._normalizeTicker(ticker);
    for (var i = 0; i < this._watchlist.length; i++) {
      if (this._watchlist[i].ticker === ticker) return i;
    }
    return -1;
  },
  _addToWatchlist: function(ticker, name) {
    var row = this._normalizeWatchEntry({ticker:ticker,name:name});
    if (!row) return Promise.resolve(false);
    return this._mutateWatchlist(function(items) {
      if (items.some(function(item){return item.ticker === row.ticker;})) return false;
      items.push({ticker:row.ticker,name:row.name}); return true;
    }, 'watchlist_add');
  },
  _removeFromWatchlist: function(ticker, ev) {
    if (ev) ev.stopPropagation();
    ticker = this._normalizeTicker(ticker);
    return this._mutateWatchlist(function(items) {
      var index = items.findIndex(function(item){return item.ticker === ticker;});
      if (index < 0) return false;
      items.splice(index,1); return true;
    }, 'watchlist_remove');
  },
  _renderWatchlist: function() {
    var el = document.getElementById('watchlist');
    if (!el) return;
    var count = document.getElementById('watchlistCount');
    if (count) count.textContent = this._watchlist.length ? '(' + this._watchlist.length + ')' : '';
    if (!this._watchlist.length) {
      el.innerHTML = '<div class="watchlist-empty">搜索股票代码，<br>点击 ⊕ 加入自选</div>';
      return;
    }
    var self = this;
    el.innerHTML = '';
    for (var i = 0; i < this._watchlist.length; i++) {
      (function(w) {
        var active = (self._currentTicker === w.ticker) ? ' active' : '';
        var pctTxt = (w.pct == null) ? '—' : (w.pct >= 0 ? '+' : '') + Number(w.pct).toFixed(2) + '%';
        var pctCls = (w.pct == null) ? '' : (Number(w.pct) >= 0 ? 'watchlist-item__pct--up' : 'watchlist-item__pct--down');
        var price = (w.price == null) ? '—' : Number(w.price).toFixed(Number(w.price) >= 100 ? 2 : 3);
        var div = document.createElement('div');
        div.className = 'watchlist-item' + active;
        div.innerHTML =
          '<span class="watchlist-item__code">' + esc(w.ticker) + '</span>' +
          '<span class="watchlist-item__name">' + esc(w.name || '') + '</span>' +
          '<span class="watchlist-item__price">' + price + '</span>' +
          '<span class="watchlist-item__pct ' + pctCls + '">' + pctTxt + '</span>' +
          '<button class="watchlist-item__del" title="移除自选" aria-label="移除 ' + escAttr(w.ticker) + '">×</button>';
        div.addEventListener('click', function() { self._selectStock(w.ticker, w.name); });
        var del = div.querySelector('.watchlist-item__del');
        if (del) del.addEventListener('click', function(e) { self._removeFromWatchlist(w.ticker, e); });
        el.appendChild(div);
      })(this._watchlist[i]);
    }
  },
  _startWatchlistRefresh: function() {
    this._stopWatchlistRefresh();
    this._refreshWatchlist();
    var self = this;
    // Adaptive refresh: 30s when visible, 120s when hidden
    var _schedule = function() {
      var interval = document.hidden ? 120000 : 30000;
      self._watchlistTimer = setTimeout(function() { self._refreshWatchlist(); _schedule(); }, interval);
    };
    _schedule();
    // Listen for visibility changes
    if (!this._visibilityBound) {
      this._visibilityBound = true;
      document.addEventListener('visibilitychange', function() {
        if (self._mode === 'market' && self._watchlistTimer) {
          clearTimeout(self._watchlistTimer);
          _schedule();
        }
      });
    }
  },
  _stopWatchlistRefresh: function() {
    if (this._watchlistTimer) { clearTimeout(this._watchlistTimer); this._watchlistTimer = null; }
  },
  _refreshWatchlist: function() {
    if (!this._watchlist.length) return;
    var self = this;
    var tickers = this._watchlist.slice(0, 20).map(function(w) { return w.ticker; });
    this.api('/api/quotes?tickers=' + encodeURIComponent(tickers.join(','))).then(function(r) {
      if (!r.ok) return;
      return r.json().then(function(data) {
        var quotes = data.quotes || {};
        var changed = false;
        for (var t in quotes) {
          var q = quotes[t];
          if (q.error) continue;
          var quoteTicker = self._normalizeTicker(q.ticker || q.code || q.symbol || t);
          for (var wi = 0; wi < self._watchlist.length; wi++) {
            if (self._watchlist[wi].ticker === quoteTicker) {
              self._watchlist[wi].price = q.price;
              self._watchlist[wi].pct = q.change_pct;
              if (q.name && q.name !== quoteTicker) self._watchlist[wi].name = q.name;
              changed = true;
              break;
            }
          }
        }
        if (changed) {
          self._renderWatchlist();
        }
      });
    }).catch(function() {});
  },

  // ── 侧边栏搜索 ──
  _marketSidebarSearchInput: function(e) {
    var self = this;
    clearTimeout(this._marketSidebarDebounce);
    var q = (e.target.value || '').trim();
    var box = document.getElementById('marketSidebarResults');
    this._marketSearchSeq = (this._marketSearchSeq || 0) + 1;
    var seq = this._marketSearchSeq;
    if (!q) { if (box) box.innerHTML = ''; return; }
    this._marketSidebarDebounce = setTimeout(function() { self._marketSidebarRunSearch(q, seq); }, 250);
  },
  _marketSidebarRunSearch: async function(q, seq) {
    var box = document.getElementById('marketSidebarResults');
    if (!box) return;
    var r = await this.api('/api/search?q=' + encodeURIComponent(q) + '&limit=10');
    if (seq && seq !== this._marketSearchSeq) return;
    var input = document.getElementById('marketSidebarQ');
    if (seq && input && (input.value || '').trim() !== q) return;
    if (!r.ok) { box.innerHTML = '<div class="market-search-empty">搜索失败</div>'; return; }
    var data = await r.json();
    if (seq && seq !== this._marketSearchSeq) return;
    var hits = data.hits || [];
    if (!hits.length) { box.innerHTML = '<div class="market-search-empty">未找到匹配股票</div>'; return; }
    var self = this;
    box.innerHTML = '';
    // Cache ticker→name for quote-chip click handler
    for (var i = 0; i < hits.length; i++) { self._tickerNames[hits[i].ticker] = hits[i].name; }
    for (var i = 0; i < hits.length; i++) {
      (function(h) {
        var added = self._findWatchIndex(h.ticker) >= 0;
        var li = document.createElement('div');
        li.className = 'market-hit';
        li.innerHTML =
          '<span class="market-hit__code">' + esc(h.ticker) + '</span>' +
          '<span class="market-hit__name">' + esc(h.name) + '</span>' +
          '<button class="market-hit__add' + (added ? ' added' : '') + '">' + (added ? '✓ 已加' : '⊕ 自选') + '</button>';
        li.addEventListener('click', async function(ev) {
          var addBtn = ev.target.closest ? ev.target.closest('.market-hit__add') : null;
          if (addBtn) {
            ev.stopPropagation();
            if (addBtn.classList.contains('added')) return;
            addBtn.disabled = true;
            var didAdd = await self._addToWatchlist(h.ticker, h.name);
            if (didAdd === null) { addBtn.disabled = false; return; }
            addBtn.classList.add('added');
            addBtn.textContent = '✓ 已加';
            addBtn.disabled = true;
            if (self._toast) self._toast((didAdd ? '已加入自选：' : '已在自选：') + self._normalizeTicker(h.ticker));
          } else {
            self._selectStock(h.ticker, h.name);
          }
        });
        box.appendChild(li);
      })(hits[i]);
    }
  },

  // ── 主面板：选股 + 渲染 ──
  _selectStock: function(ticker, name) {
    if (this._showMarketWorkspace) this._showMarketWorkspace('quote');
    this._marketSelectionSeq = (this._marketSelectionSeq || 0) + 1;
    this._invalidateKline();
    this._currentTicker = ticker;
    this._currentName = name || '';
    this._renderWatchlist();
    this._renderQuoteCard(ticker, name);
    this._renderKline(ticker, this._currentPeriod);
    this._renderFundamentals(ticker);
    this._closeMobileSidebar();
  },
  _invalidateKline: function() {
    this._lastKline = null;
    this._klineIdentity = null;
    this._indicatorOverlays = []; this._indicatorMarkers = []; this._indicatorTracks = []; this._indicatorZones = [];
    this._indicatorEvalSeq = (this._indicatorEvalSeq || 0) + 1;
  },
  _fmtVolume: function(v) {
    var n = Number(v);
    if (isNaN(n)) return '—';
    if (n >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (n >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toFixed(0);
  },
  _renderQuoteCard: async function(ticker, name) {
    var sequence = this._marketSelectionSeq;
    var inner = document.getElementById('marketPanelInner');
    if (!inner) return;
    inner.innerHTML =
      '<div class="market-detail-layout"><div class="market-detail-primary">' +
        '<div class="market-quote-card"><div class="market-loading-state">加载 ' + esc(ticker) + ' 行情中…</div></div>' +
        '<div class="market-kline-wrap"></div>' +
      '</div><aside class="market-detail-aside"><div class="market-fundamentals"></div><div class="market-indicator-dock" id="marketIndicatorDock"></div></aside></div>';
    if (this._renderIndicatorDock) this._renderIndicatorDock();
    var r;
    try { r = await this.api('/api/quote/' + encodeURIComponent(ticker)); }
    catch (error) { r = {ok:false}; }
    if (sequence !== this._marketSelectionSeq || (this._currentTicker && ticker !== this._currentTicker)) return;
    var card = inner.querySelector('.market-quote-card');
    if (!r.ok) {
      card.innerHTML = '<div class="market-error-state">暂无 ' + esc(ticker) + ' 的行情数据<button onclick="App._selectStock(' + JSON.stringify(ticker).replace(/"/g,'&quot;') + ',' + JSON.stringify(name||'').replace(/"/g,'&quot;') + ')">重试</button></div>';
      return;
    }
    var q = await r.json();
    if (sequence !== this._marketSelectionSeq || (this._currentTicker && ticker !== this._currentTicker)) return;
    var officialName = String(q.name || '').trim();
    if (officialName && officialName !== ticker) {
      this._currentName = officialName;
      var watchIndex = this._findWatchIndex(ticker);
      if (watchIndex >= 0 && this._watchlist[watchIndex].name !== officialName) {
        this._watchlist[watchIndex].name = officialName;
        this._renderWatchlist();
      }
    }
    var big = (q.price != null && Number(q.price) >= 100);
    var price = (q.price == null) ? '—' : Number(q.price).toFixed(big ? 2 : 3);
    var chgNum = q.change;
    var chg = (chgNum == null) ? '—' : (Number(chgNum) >= 0 ? '+' : '') + Number(chgNum).toFixed(Math.abs(Number(chgNum)) >= 100 ? 2 : 3);
    var pct = (q.change_pct == null) ? '—' : (Number(q.change_pct) >= 0 ? '+' : '') + Number(q.change_pct).toFixed(2) + '%';
    var cls = (q.change_pct != null && Number(q.change_pct) >= 0) ? 'up' : 'down';
    var stale = q.stale ? '<span class="market-qc-stale">·过期数据</span>' : '';
    var self = this;
    function cell(label, val) {
      var v = (val == null || val === '') ? '—' : esc(String(val));
      return '<div class="market-qc-cell"><span class="market-qc-cell__label">' + label + '</span><span class="market-qc-cell__val">' + v + '</span></div>';
    }
    var f2 = function(v) { return (v == null) ? null : Number(v).toFixed(2); };
    card.innerHTML =
      '<div class="market-qc-head">' +
        '<span class="market-qc-code">' + esc(ticker) + '</span>' +
        '<span class="market-qc-name">' + esc(officialName || name || '') + '</span>' +
        '<button class="market-qc-ask" onclick="App._askAboutStock()">问 AI</button>' +
      '</div>' +
      '<div class="market-qc-price-row">' +
        '<span class="market-qc-price">' + price + '</span>' +
        '<span class="market-qc-chg market-qc-chg--' + cls + '">' + chg + ' (' + pct + ')</span>' +
        stale +
      '</div>' +
      '<div class="market-qc-grid">' +
        cell('今开', f2(q.open)) + cell('最高', f2(q.high)) + cell('最低', f2(q.low)) +
        cell('昨收', f2(q.prev_close)) + cell('成交量', q.volume != null ? self._fmtVolume(q.volume) : null) +
        cell('成交额', q.turnover != null ? self._fmtVolume(q.turnover) : null) +
      '</div>' +
      '<div class="market-qc-src">数据来源: ' + esc(q.source || '—') + ' · ' +
        esc(this._quoteTimeLabel(q)) + '</div>';
  },
  _quoteTimeLabel: function(q) {
    var timestamp = q.data_timestamp;
    var label = '行情时间';
    if (timestamp && q.data_kind === 'daily_close') {
      return '行情日期（日线收盘数据）：' + new Date(timestamp * 1000).toLocaleDateString('zh-CN', {timeZone:'Asia/Shanghai'}) + ' 北京时间';
    }
    if (!timestamp) {
      timestamp = q.retrieved_at || q.timestamp;
      label = '采集时间（行情时刻未知）';
    }
    if (!timestamp) return '行情时间未知';
    return label + '：' + new Date(timestamp * 1000).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) + ' 北京时间';
  },
  _renderKline: async function(ticker, period, limit) {
    if (ticker !== this._currentTicker) return;
    var sequence = this._klineRequestSeq = (this._klineRequestSeq || 0) + 1;
    this._invalidateKline();
    this._currentPeriod = period;
    var defaults = {'1d':120, '1w':104, '1m':60};
    limit = Math.max(12, Math.min(500, Number(limit || this._klinePeriodLimits[period] || defaults[period] || 120)));
    this._currentKlineLimit = limit;
    this._klinePeriodLimits[period] = limit;
    var inner = document.getElementById('marketPanelInner');
    var wrap = inner ? inner.querySelector('.market-kline-wrap') : null;
    if (!wrap) return;
    var periods = [['1d', '日线'], ['1w', '周线'], ['1m', '月线']];
    var tabs = periods.map(function(p) {
      return '<button class="market-kline-period' + (p[0] === period ? ' active' : '') + '" onclick="App._renderKline(' + JSON.stringify(ticker).replace(/"/g,'&quot;') + ',' + JSON.stringify(p[0]).replace(/"/g,'&quot;') + ')">' + p[1] + '</button>';
    }).join('');
    var rangeSets = {
      '1d': [[60,'3月'],[120,'半年'],[250,'1年'],[500,'全部']],
      '1w': [[26,'半年'],[52,'1年'],[104,'2年'],[260,'5年']],
      '1m': [[12,'1年'],[24,'2年'],[60,'5年'],[120,'10年']]
    };
    var ranges = (rangeSets[period] || rangeSets['1d']).map(function(item) {
      return '<button class="market-kline-range' + (item[0] === limit ? ' active' : '') + '" onclick="App._renderKline(' + JSON.stringify(ticker).replace(/"/g,'&quot;') + ',' + JSON.stringify(period).replace(/"/g,'&quot;') + ',' + item[0] + ')">' + item[1] + '</button>';
    }).join('');
    var maNames = ['MA5','MA10','MA20','MA60'];
    var self = this;
    var maToggles = maNames.map(function(ma) {
      var checked = self._klineMaVisible[ma] !== false;
      return '<label class="market-kline-ma' + (checked ? ' active' : '') + '"><input type="checkbox" ' + (checked ? 'checked ' : '') + 'onchange="App._toggleKlineSeries(\'' + ma + '\',this.checked);this.parentNode.classList.toggle(\'active\',this.checked)"><span>' + ma + '</span></label>';
    }).join('');
    wrap.innerHTML = '<div class="market-kline-hd"><h4>K 线</h4><div class="market-kline-segment">' + tabs + '</div><div class="market-kline-segment range">' + ranges + '</div><div class="market-kline-tools"><div class="market-kline-ma-list">' + maToggles + '</div><button class="market-kline-icon" title="复位缩放" aria-label="复位缩放" onclick="App._resetKlineZoom()">↺</button><button class="market-kline-icon" title="全屏查看" aria-label="全屏查看" onclick="App._toggleKlineFullscreen()">⛶</button></div></div><div class="market-kline-empty">加载 K 线中…</div>';
    var r;
    try { r = await this.api('/api/quote/' + encodeURIComponent(ticker) + '/kline?period=' + encodeURIComponent(period) + '&limit=' + limit); }
    catch (error) { r = {ok:false}; }
    if (sequence !== this._klineRequestSeq || ticker !== this._currentTicker || period !== this._currentPeriod) return;
    var emptyEl = wrap.querySelector('.market-kline-empty');
    if (!r.ok) { if (emptyEl) emptyEl.textContent = 'K 线数据加载失败'; return; }
    var data = await r.json();
    if (sequence !== this._klineRequestSeq || ticker !== this._currentTicker || period !== this._currentPeriod) return;
    var kline = data.kline || [];
    if (!kline.length) { if (emptyEl) emptyEl.textContent = '暂无 K 线数据'; return; }
    if (emptyEl) emptyEl.remove();
    var chartDiv = document.createElement('div');
    chartDiv.id = 'marketKlineChart';
    wrap.appendChild(chartDiv);
    this._klineIdentity = {ticker:ticker,period:period,limit:limit,request_id:sequence,data_as_of:data.data_timestamp || null};
    this.renderKlineChart(chartDiv, kline);
  },
  _toggleKlineSeries: function(name, visible) {
    this._klineMaVisible[name] = !!visible;
    if (!this._klineChart) return;
    try {
      this._klineChart.dispatchAction({type: visible ? 'legendSelect' : 'legendUnSelect', name: name});
    } catch (e) {}
  },
  _resetKlineZoom: function() {
    if (!this._klineChart) return;
    try { this._klineChart.dispatchAction({type:'dataZoom', start:0, end:100}); } catch (e) {}
  },
  _toggleKlineFullscreen: function() {
    var inner = document.getElementById('marketPanelInner');
    var wrap = inner ? inner.querySelector('.market-kline-wrap') : null;
    if (!wrap) return;
    try {
      if (document.fullscreenElement === wrap) document.exitFullscreen();
      else if (wrap.requestFullscreen) wrap.requestFullscreen();
    } catch (e) {}
  },
  _chartPalette: function() {
    var dark = document.documentElement.classList.contains('dark');
    return dark
      ? {rise:'#ef5350', fall:'#26a69a', up:'#ef5350', dn:'#26a69a', ma1:'#fbbf24', ma2:'#a78bfa', grid:'#202228', axl:'#282a32', txt:'#706c64', cross:'#706c64', tipBg:'#18191e', tipBd:'#282a32', tipTxt:'#d8d5cd', zoomBd:'#282a32', zoomFill:'#202228'}
      : {rise:'#d6332f', fall:'#1f9d83', up:'#d6332f', dn:'#1f9d83', ma1:'#cf8a3a', ma2:'#7c5fd0', grid:'#d2cec3', axl:'#c8c2b4', txt:'#8a8478', cross:'#9d9890', tipBg:'#ffffff', tipBd:'#e2e0da', tipTxt:'#1c1a16', zoomBd:'#d5d2cb', zoomFill:'#ebe9e4'};
  },
  renderKlineChart: function(container, kline) {
    if (typeof echarts === 'undefined') { container.innerHTML = '图表库未加载'; return; }
    if (this._klineChart) { try { this._klineChart.dispose(); } catch (e) {} this._klineChart = null; }
    var chart = echarts.init(container, null, {renderer: 'canvas'});
    this._klineChart = chart;
    this._lastKline = kline;
    var dates = [], ohlc = [], vols = [], closes = [];
    for (var i = 0; i < kline.length; i++) {
      var k = kline[i];
      var ts = k.ts || k.timestamp || 0;
      var d = new Date(ts * 1000);
      var dateParts = new Intl.DateTimeFormat('en-CA', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d);
      var fields = {}; dateParts.forEach(function(part) { fields[part.type] = part.value; });
      var ds = fields.year + '-' + fields.month + '-' + fields.day;
      dates.push(ds);
      ohlc.push([+k.open, +k.close, +k.low, +k.high]);
      vols.push(+k.volume || 0);
      closes.push(+k.close);
    }
    function ma(arr, n) {
      var r = [];
      for (var i = 0; i < arr.length; i++) {
        if (i < n - 1) { r.push(null); continue; }
        var s = 0; for (var j = 0; j < n; j++) s += +arr[i - j];
        r.push(+(s / n).toFixed(2));
      }
      return r;
    }
    var ma5 = ma(closes, 5), ma20 = ma(closes, 20);
    var P = this._chartPalette();
    // Map timestamp → index so user indicator overlays/markers align to the axis.
    var tsIdx = {};
    for (var ti = 0; ti < kline.length; ti++) {
      tsIdx[(kline[ti].ts || kline[ti].timestamp || 0)] = ti;
    }
    var overlays = this._indicatorOverlays || [];
    var markers = this._indicatorMarkers || [];
    var legendData = ['日K', 'MA5', 'MA20'];
    var ocolors = ['#22c55e', '#fbbf24', '#60a5fa', '#f472b6', '#34d399'];
    var overlaySeries = [];
    for (var oi = 0; oi < overlays.length; oi++) {
      var ov = overlays[oi], arr = new Array(dates.length).fill(null);
      var pts = ov.points || [];
      for (var pi = 0; pi < pts.length; pi++) {
        var ix = tsIdx[pts[pi].t];
        if (ix != null) arr[ix] = pts[pi].v;
      }
      var col = ov.color || ocolors[oi % ocolors.length];
      legendData.push(ov.name);
      overlaySeries.push({name: ov.name, type: 'line', data: arr, smooth: true,
        showSymbol: false, lineStyle: {width: 1.5, color: col}, z: 6});
    }
    var markData = [];
    for (var mi = 0; mi < markers.length; mi++) {
      var midx = tsIdx[markers[mi].t];
      if (midx == null) continue;
      markData.push({coord: [dates[midx], (+kline[midx].low) * 0.99],
        value: markers[mi].label || '▲', itemStyle: {color: markers[mi].color || P.up}});
    }
    var candleSeries = {name: '日K', type: 'candlestick', data: ohlc,
      itemStyle: {color: P.up, color0: P.dn, borderColor: P.up, borderColor0: P.dn}};
    if (markData.length) candleSeries.markPoint = {data: markData, symbolSize: 30, label: {fontSize: 11, color: '#fff'}};
    chart.setOption({
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {trigger: 'axis', axisPointer: {type: 'cross', lineStyle: {color: P.cross, type: 'dashed'}},
        backgroundColor: P.tipBg, borderColor: P.tipBd, textStyle: {color: P.tipTxt, fontSize: 11}},
      axisPointer: {link: [{xAxisIndex: 'all'}]},
      legend: {data: legendData, top: 4, textStyle: {color: P.txt, fontSize: 10}, itemWidth: 12, itemHeight: 8},
      grid: [{left: 54, right: 18, top: 28, height: '56%'}, {left: 54, right: 18, top: '74%', height: '15%'}],
      xAxis: [
        {type: 'category', data: dates, boundaryGap: false, axisLine: {lineStyle: {color: P.axl}}, splitLine: {show: false}, axisLabel: {color: P.txt, fontSize: 9}},
        {type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: {show: false}, axisLabel: {show: false}, splitLine: {show: false}}
      ],
      yAxis: [
        {scale: true, splitLine: {lineStyle: {color: P.grid}}, axisLabel: {color: P.txt, fontSize: 9}},
        {gridIndex: 1, splitNumber: 2, axisLabel: {show: false}, splitLine: {show: false}}
      ],
      dataZoom: [
        {type: 'inside', xAxisIndex: [0, 1], start: 35, end: 100},
        {show: true, type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, start: 35, end: 100, borderColor: P.zoomBd, fillerColor: P.zoomFill}
      ],
      series: [
        candleSeries,
        {name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma1}, z: 5},
        {name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma2}, z: 5},
        {name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: vols.map(function(v, i) { return {value: v, itemStyle: {color: closes[i] >= +kline[i].open ? P.up : P.dn}}; })}
      ].concat(overlaySeries)
    });
    var self = this;
    if (!this._klineResizeBound) {
      this._klineResizeBound = true;
      window.addEventListener('resize', function() { if (self._klineChart) { try { self._klineChart.resize(); } catch (e) {} } });
    }
  },
  _renderFundamentals: async function(ticker) {
    var sequence = this._marketSelectionSeq;
    var inner = document.getElementById('marketPanelInner');
    var wrap = inner ? inner.querySelector('.market-fundamentals') : null;
    if (!wrap) return;
    wrap.innerHTML = '<h4>基本面</h4><div class="market-fund-unavailable">加载中…</div>';
    var r;
    try { r = await this.api('/api/fundamentals/' + encodeURIComponent(ticker)); }
    catch (error) { r = {ok:false}; }
    if (sequence !== this._marketSelectionSeq || ticker !== this._currentTicker) return;
    if (!r.ok) { wrap.innerHTML = '<h4>基本面</h4><div class="market-fund-unavailable">基本面数据暂不可用</div>'; return; }
    var f = await r.json();
    if (sequence !== this._marketSelectionSeq || ticker !== this._currentTicker) return;
    function cell(label, val) {
      var v = (val == null || val === '') ? '—' : esc(Number(val).toFixed(2));
      return '<div class="market-fund-cell"><span class="market-fund-cell__label">' + label + '</span><span class="market-fund-cell__val">' + v + '</span></div>';
    }
    var hasAny = (f.pe_ttm != null || f.pb != null || f.pe_static != null || f.ps_ttm != null);
    wrap.innerHTML = '<h4>基本面</h4>' +
      (hasAny
        ? '<div class="market-fund-grid">' + cell('PE(TTM)', f.pe_ttm) + cell('PE(静态)', f.pe_static) + cell('PB', f.pb) + cell('PS(TTM)', f.ps_ttm) + '</div>' +
          '<div class="market-qc-src">数据来源: ' + esc(f.source || '—') +
          '<br>财报期间：'+esc(f.reporting_period || '未知')+' · 币种/单位：'+esc((f.currency || '未知')+' / '+(f.monetary_unit || '未知'))+
          '<br>口径：'+esc(f.accounting_scope || '未知')+' · 采集：'+esc(ChatRender.formatTime(f.retrieved_at || f.timestamp))+'</div>'
        : '<div class="market-fund-unavailable">基本面数据暂不可用</div>');
  },
  _askAboutStock: function() {
    this.switchMode('chat');
    var input = document.getElementById('input');
    if (input) {
      input.value = '分析一下 ' + (this._currentTicker || '') + ' ' + (this._currentName || '');
      input.focus();
      try { input.dispatchEvent(new Event('input')); } catch (e) {}
    }
  },

  _bindMarketModal: function() {
    var self = this;
    var q = document.getElementById('marketQ');
    if (q && !q._wired) {
      q._wired = true;
      q.addEventListener('input', function(e) { self._marketSearchInput(e); });
      q.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') self.closeMarket();
      });
    }
  },

  _bindMarketPanel: function() {
    var self = this;
    // 侧边栏行情搜索框
    var sq = document.getElementById('marketSidebarQ');
    if (sq && !sq._wired) {
      sq._wired = true;
      sq.addEventListener('input', function(e) { self._marketSidebarSearchInput(e); });
    }
    // 聊天中的 [[quote:TICKER]] chip 点击 → 跳行情面板
    document.addEventListener('click', function(e) {
      var chip = e.target.closest('.quote-chip');
      if (chip) {
        var ticker = chip.getAttribute('data-ticker');
        if (ticker) {
          var name = self._tickerNames[ticker] || '';
          self.switchMode('market');
          self._selectStock(ticker, name);
        }
      }
    });
  },


  /* ═══ Analysis Mode (v0.9) ═══ */
  _analysisTicker: null,
  _analysisName: "",
  _analysisSidebarDebounce: null,
  _analysisSearchSeq: 0,
  _marketSearchSeq: 0,

  // Pre-warm the backend stock-list cache so the first real search is instant.
  _warmupSearch: function() {
    var self = this;
    if (this._warmed) return;
    this._warmed = true;
    setTimeout(function() {
      self.api('/api/search?q=a&limit=1').catch(function(){});
    }, 100);
  },

  _bindAnalysisSearch: function() {
    var self = this;
    var aq = document.getElementById("analysisQ");
    if (aq && !aq._wired) {
      aq._wired = true;
      aq.addEventListener("input", function(e) { self._analysisSearchInput(e); });
    }
  },
  _analysisSearchInput: function(e) {
    var self = this;
    clearTimeout(this._analysisSidebarDebounce);
    var q = (e.target.value || "").trim();
    var box = document.getElementById("analysisSearchResults");
    this._analysisSearchSeq = (this._analysisSearchSeq || 0) + 1;
    var seq = this._analysisSearchSeq;
    if (!q) { if (box) box.innerHTML = ""; return; }
    this._analysisSidebarDebounce = setTimeout(function() { self._analysisRunSearch(q, seq); }, 150);
  },
  _analysisRunSearch: async function(q, seq) {
    var box = document.getElementById("analysisSearchResults");
    if (!box) return;
    var r = await this.api("/api/search?q=" + encodeURIComponent(q) + "&limit=8");
    if (seq && seq !== this._analysisSearchSeq) return;
    var input = document.getElementById("analysisQ");
    if (seq && input && (input.value || "").trim() !== q) return;
    if (!r.ok) { box.innerHTML = "<div class=\"market-search-empty\">搜索失败</div>"; return; }
    var data = await r.json();
    if (seq && seq !== this._analysisSearchSeq) return;
    var hits = data.hits || [];
    if (!hits.length) { box.innerHTML = "<div class=\"market-search-empty\">未找到匹配股票</div>"; return; }
    var self = this;
    box.innerHTML = "";
    for (var i = 0; i < hits.length; i++) {
      (function(h) {
        self._tickerNames[h.ticker] = h.name;
        var div = document.createElement("div");
        div.className = "market-hit";
        div.innerHTML = "<span class=\"market-hit__code\">" + esc(h.ticker) + "</span>" +
          "<span class=\"market-hit__name\">" + esc(h.name) + "</span>";
        div.addEventListener("click", function() {
          self._analysisTicker = h.ticker;
          self._analysisName = h.name;
          document.getElementById("analysisStatus").innerHTML =
            "<div style=\"color:var(--accent);padding:4px 0\">已选: " + esc(h.name) + " (" + esc(h.ticker) + ")</div>";
          box.innerHTML = "";
          document.getElementById("analysisQ").value = "";
        });
        box.appendChild(div);
      })(hits[i]);
    }
  },
  _runAnalysis: async function() {
    if (!this._analysisTicker) { alert("请先搜索并选择一只股票"); return; }
    var ticker = this._analysisTicker;
    var name = this._analysisName;
    var status = document.getElementById("analysisStatus");
    status.innerHTML = "<div style=\"color:var(--accent);padding:4px 0\">已选: " + esc(name) + " (" + esc(ticker) + ") → 正在启动分析…</div>";
    // Switch to chat and auto-execute: create conversation + send prompt
    // all in one click — no manual Enter required.
    this.switchMode("chat");
    var input = document.getElementById("input");
    if (input) {
      input.value = "分析一下 " + ticker + " " + name + "\n1. 基本面\n2. 技术面\n3. 供应链\n4. 催化剂\n5. 风险";
      try { input.dispatchEvent(new Event("input")); } catch(e) {}
      // Auto-send: send() handles newConv() if needed, then streams the response
      var self = this;
      setTimeout(function() { self.send(); }, 100);
    }
  },
});
