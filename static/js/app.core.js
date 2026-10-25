/* ═══════════════════════════════════════
   Prism — Application Logic (v0.8 split)
   ═══════════════════════════════════════ */

var Streams = {}; // Active SSE streams keyed by convId

function _stripObjectArtifactsText(text, trim) {
  var out = String(text || '').replace(/\[object Object\]/g, '').replace(/\n{3,}/g, '\n\n');
  return trim ? out.trim() : out;
}

function _sanitizeArtifactValue(value) {
  if (value === null || value === undefined) return value;
  if (typeof value === 'string') return _stripObjectArtifactsText(value, false);
  if (typeof value === 'number' || typeof value === 'boolean') return value;
  if (Array.isArray(value)) {
    var list = [];
    for (var i = 0; i < value.length; i++) {
      var item = _sanitizeArtifactValue(value[i]);
      if (item !== null && item !== undefined) list.push(item);
    }
    return list;
  }
  if (typeof value === 'object') {
    var out = {};
    for (var k in value) out[k] = _sanitizeArtifactValue(value[k]);
    if ((out.t === 'think' || out.t === 'text') && !_stripObjectArtifactsText(out.c || '', true)) return null;
    return out;
  }
  return value;
}

function _sanitizeConvPayload(data) {
  return _sanitizeArtifactValue(data);
}

/* IndexedDB helper for conversation cache (P5).
   Stores {id, conv} keyed by conversation ID so switching convs
   renders instantly from cache while the network fetch completes. */
var _idb = (function() {
  if (!window.indexedDB) return null;
  var _db = null;
  var DB_VERSION = 3; // Clear legacy shared caches; all current keys include the signed-in account.
  function cacheKey(id) { return String(App.user || '') + ':' + String(id); }
  function open() {
    return new Promise(function(resolve, reject) {
      if (_db) return resolve(_db);
      var req = indexedDB.open('prism_cache', DB_VERSION);
      req.onupgradeneeded = function(e) {
        var db = e.target.result;
        if (db.objectStoreNames.contains('convs')) db.deleteObjectStore('convs');
        db.createObjectStore('convs', {keyPath: 'id'});
      };
      req.onsuccess = function(e) { _db = e.target.result; resolve(_db); };
      req.onerror = function() { reject(); };
    });
  }
  return {
    getConv: function(id) {
      return open().then(function(db) {
        return new Promise(function(resolve) {
          var tx = db.transaction('convs', 'readonly');
          var req = tx.objectStore('convs').get(cacheKey(id));
          req.onsuccess = function() { resolve(req.result ? _sanitizeConvPayload(req.result.data) : null); };
          req.onerror = function() { resolve(null); };
        });
      }).catch(function() { return null; });
    },
    putConv: function(id, data) {
      return open().then(function(db) {
        var tx = db.transaction('convs', 'readwrite');
        tx.objectStore('convs').put({id: cacheKey(id), data: _sanitizeConvPayload(data), ts: Date.now()});
      }).catch(function(){});
    },
    clear: function() {
      return open().then(function(db) {
        return new Promise(function(resolve, reject) {
          var tx = db.transaction('convs', 'readwrite');
          tx.objectStore('convs').clear();
          tx.oncomplete = resolve; tx.onerror = reject;
        });
      });
    },
    listConvs: function() {
      return open().then(function(db) {
        return new Promise(function(resolve) {
          var tx = db.transaction('convs', 'readonly');
          var req = tx.objectStore('convs').getAll();
          req.onsuccess = function() { resolve(req.result || []); };
          req.onerror = function() { resolve([]); };
        });
      }).catch(function() { return []; });
    }
  };
})();

var App = {
  user: '', skill: 'prism-research', convId: '', msgs: [], acIdx: -1, acItems: [], skills: [],
  model: '',
  _userScrolledUp: false, // Smart scroll — true when user manually scrolls away from bottom
  _activeReader: null,    // AbortController for stop button
  _bulkMode: false,       // Bulk select mode — true when checkboxes are visible
  _bulkChecked: {},       // {convId: true} for checked items
  _activeSkills: new Set(['prism-research']), // One native research workflow.
  _telemetryRecent: {},

  /* ═══ Init ═══ */
  init: async function() {
    var r = await this.api('/api/me'); if (!r.ok) return;
    var me = await r.json();
    this.user = me.user;
    this.isAdmin = !!me.is_admin;
    if (this._initDiagnostics) this._initDiagnostics();
    document.getElementById('userBadge').textContent = this.user;
    var adminEntry = document.getElementById('adminEntry');
    if (adminEntry) adminEntry.style.display = this.isAdmin ? 'inline-flex' : 'none';
    // Theme & width
    var saved = localStorage.getItem('cl-theme');
    if (saved === 'dark') document.documentElement.className = 'dark';
    else if (saved === 'light') document.documentElement.className = 'light';
    else document.documentElement.className = 'light';
    if (localStorage.getItem('cl-wide') === 'true') document.documentElement.classList.add('wide');
    this.loadSkills(); this.loadModels(); this.loadConvs(); this.showWelcome();
    this.renderSkillsBar();
    if (this._initReleaseNotes) this._initReleaseNotes();
    this._bindScroll();
    this._bindGlossClicks();
    this._bindMarketModal();
    this._loadWatchlist();
    this._bindMarketPanel();
    this._bindAnalysisSearch();
    this._trackUiEvent('chat', 'open');
    if (this._initWorkbench) this._initWorkbench();
    // Register Service Worker for offline cache (P5). Bypass HTTP cache for
    // the SW script itself so deployments can replace stale app shells.
    if ('serviceWorker' in navigator) {
      var swReloading = false;
      navigator.serviceWorker.addEventListener('controllerchange', function() {
        if (swReloading) return;
        swReloading = true;
        window.location.reload();
      });
      navigator.serviceWorker.register('/static/sw.js', {updateViaCache: 'none'}).then(function(reg) {
        try { reg.update(); } catch(e) {}
      }).catch(function(){});
    }
    // If a previous analysis was running, auto-restore it (P5 persistence)
    var saved;
    try { saved = sessionStorage.getItem('prism_analysis'); } catch(e) {}
    if (saved) {
      var st;
      try { st = JSON.parse(saved); } catch(e) {}
      if (st && st.id) {
        this._analysisTicker = st.ticker;
        this._analysisName = st.name;
        this.switchMode('analysis');
      }
    }
  },

  api: async function(url, opts) {
    opts = Object.assign({}, opts || {});
    // Inject CSRF defense headers on every API call. The double-submit
    // cookie pattern: the server sets csrf_token (non-HttpOnly), JS mirrors
    // it in the X-CSRF-Token header. X-Requested-With additionally blocks
    // browser cross-site requests (browsers won't add custom headers to
    // cross-origin form submissions).
    opts.headers = Object.assign({
      'X-Requested-With': 'XMLHttpRequest',
    }, opts.headers || {});
    var method = (opts.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      var csrf = this._getCookie('csrf_token');
      if (csrf) opts.headers['X-CSRF-Token'] = csrf;
    }
    var r, deadline, expired = false, upstream = opts.signal;
    var controller = new AbortController();
    var cancel = function() { controller.abort(); };
    if (upstream) {
      if (upstream.aborted) cancel();
      else upstream.addEventListener('abort', cancel, {once:true});
    }
    opts.signal = controller.signal;
    deadline = setTimeout(function() { expired = true; controller.abort(); }, 30000);
    try {
      r = await fetch(url, opts);
    } catch (error) {
      if (expired) error = new Error('请求超时，执行结果尚未确认。请刷新相关历史后再决定是否重试。');
      if (this._captureDiagnostic && String(url).indexOf('/api/diagnostics/') < 0) {
        this._captureDiagnostic('api_network_error', error.message || 'API network error', {
          severity:'warning', component:'api', stack:error.stack || '',
          context:{route:String(url).split('?')[0], method:method}
        });
      }
      throw error;
    } finally {
      clearTimeout(deadline);
      if (upstream) upstream.removeEventListener('abort', cancel);
    }
    if (r.status >= 500 && this._captureDiagnostic && String(url).indexOf('/api/diagnostics/') < 0) {
      this._captureDiagnostic('api_http_5xx', 'HTTP ' + r.status + ' from API', {
        severity:'error', component:'api',
        context:{route:String(url).split('?')[0], method:method, status:r.status, request_id:r.headers.get('X-Request-ID') || ''}
      });
    }
    if (r.status === 401) window.location.href = '/login'; return r;
  },

  _trackUiEvent: function(feature, action, targetType, targetId, targetLabel) {
    if (!feature || !action || !this.user) return;
    var key = feature + ':' + action + ':' + (targetId || '');
    var now = Date.now();
    if (action === 'open' && now - Number(this._telemetryRecent[key] || 0) < 60000) return;
    this._telemetryRecent[key] = now;
    this.api('/api/activity', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        feature: feature,
        action: action,
        target_type: targetType || '',
        target_id: targetId || '',
        target_label: targetLabel || ''
      })
    }).catch(function() {});
  },

  _getCookie: function(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  },

  /* ═══ Theme ═══ */
  toggleTheme: function() {
    var c = document.documentElement.className.replace('wide','').trim();
    var n = (c === 'dark') ? 'light' : 'dark';
    document.documentElement.className = n + (document.documentElement.classList.contains('wide') ? ' wide' : '');
    localStorage.setItem('cl-theme', n);
    // 主题切换后重绘当前 K 线（换配色表）
    if (this._mode === 'market' && this._currentTicker) {
      this._renderKline(this._currentTicker, this._currentPeriod || '1d');
    }
    if (this._mode === 'industry' && this._refreshIndustryTheme) {
      this._refreshIndustryTheme();
    }
  },
  toggleWidth: function() {
    document.documentElement.classList.toggle('wide');
    localStorage.setItem('cl-wide', document.documentElement.classList.contains('wide'));
  },

  _isCompactViewport: function() {
    return !!(window.matchMedia && window.matchMedia('(max-width: 900px)').matches);
  },

  toggleSidebar: function(force) {
    var shell = document.getElementById('appShell');
    if (!shell) return;
    if (this._isCompactViewport()) {
      var open = force;
      if (open === undefined) open = !shell.classList.contains('mobile-sidebar-open');
      shell.classList.toggle('mobile-sidebar-open', !!open);
    } else {
      shell.classList.remove('mobile-sidebar-open');
      shell.classList.toggle('collapsed');
    }
  },

  _closeMobileSidebar: function() {
    var shell = document.getElementById('appShell');
    if (shell) shell.classList.remove('mobile-sidebar-open');
  },

  /* ═══ Welcome ═══ */
  loadConvs: async function() {
    var r = await this.api('/api/convs'); if (!r.ok) return;
    var convs = await r.json(), el = document.getElementById('convList');
    var badge = document.getElementById('convBadge');
    if (!convs.length) {
      el.innerHTML = '<div class="conv-empty">暂无分析</div>'; badge.textContent = '';
      this._bulkChecked = {};
      this._renderBulkBar();
    } else {
      var self = this;
      el.innerHTML = convs.map(function(c){
        var checked = self._bulkChecked[c.id] ? ' checked' : '';
        return '<div class="conv-item'+(c.id===self.convId?' active':'')+'" data-id="'+escAttr(c.id)+'" onclick="App.openConv(\''+escAttr(c.id)+'\')" title="'+escAttr(c.title)+'">' +
          '<input type="checkbox" class="conv-cb'+(self._bulkMode?' visible':'')+'" ' + checked +
          ' onclick="event.stopPropagation();App._toggleCheck(\''+c.id+'\',event)" title="选择">' +
          esc(c.title) +
          '<span class="del" onclick="event.stopPropagation();App.delConv(\''+escAttr(c.id)+'\')">×</span></div>';
      }).join('');
      badge.textContent = convs.length;
    }
    this._updateTrashLink();
    this._renderBulkBar();
  },

  _toggleCheck: function(id, ev) {
    if (ev.target.checked) this._bulkChecked[id] = true;
    else delete this._bulkChecked[id];
    this._renderBulkBar();
  },

  _selectAll: function() {
    var self = this;
    var cbs = document.querySelectorAll('#convList .conv-cb');
    var allChecked = cbs.length > 0 && Object.keys(this._bulkChecked).length >= cbs.length;
    cbs.forEach(function(cb) {
      cb.checked = !allChecked;
      var id = cb.parentElement.getAttribute('data-id');
      if (!allChecked) self._bulkChecked[id] = true; else delete self._bulkChecked[id];
    });
    this._renderBulkBar();
  },

  _renderBulkBar: function() {
    var bar = document.getElementById('bulkBar');
    if (!bar) return;
    var count = Object.keys(this._bulkChecked).length;
    if (this._bulkMode) {
      bar.className = 'bulk-bar show';
      bar.innerHTML = '<span class="bulk-bar-count">' + count + ' 项</span>' +
        '<div class="bulk-bar-actions">' +
        '<button class="bulk-act" onclick="App._selectAll()">全选</button>' +
        '<button class="bulk-act danger" onclick="App._bulkDelete()">删除</button>' +
        '<button class="bulk-act" onclick="App._toggleBulkMode()">完成</button>' +
        '</div>';
    } else {
      bar.className = 'bulk-bar show';
      bar.innerHTML = '<button class="bulk-act" onclick="App._toggleBulkMode()">选择</button>';
    }
  },

  _toggleBulkMode: function() {
    this._bulkMode = !this._bulkMode;
    this._bulkChecked = {};
    this.loadConvs();
  },

  _bulkDelete: async function() {
    var ids = Object.keys(this._bulkChecked);
    if (!ids.length) return;
    if (!confirm('将 ' + ids.length + ' 个分析移到回收站？')) return;
    var self = this;
    var r = await this.api('/api/convs/batch-delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ids: ids})
    });
    if (r.ok) {
      this._bulkChecked = {};
      // If current conversation was deleted, reset
      if (ids.indexOf(this.convId) >= 0) { this.convId = ''; this.msgs = []; this.showWelcome(); }
      this.loadConvs();
    }
  },

  _updateTrashLink: async function() {
    var r = await this.api('/api/trash'); if (!r.ok) return;
    var trash = await r.json();
    var link = document.getElementById('trashLink');
    if (trash.length) {
      link.classList.remove('hidden');
      link.style.display = '';
      link.setAttribute('data-count', String(trash.length));
      link.setAttribute('title', '回收站 ' + trash.length);
    } else {
      link.classList.add('hidden');
      link.removeAttribute('data-count');
      link.setAttribute('title', '回收站');
    }
    // Show/hide analysis graph link based on whether a conv is active
    var gl = document.getElementById('graphLink');
    if (gl) gl.style.display = this.convId ? '' : 'none';
  },

  newConv: async function(skipWelcome) {
    var requestId = this._convRequestId = (this._convRequestId || 0) + 1;
    var skillForNew = ['prism-research','bottleneck-hunter','serenity'].indexOf(this.skill) >= 0 ? this.skill : 'prism-research';
    var r = await this.api('/api/convs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill: skillForNew})});
    if (!r.ok) return false; var c = await r.json();
    if (requestId !== this._convRequestId) return false;
    this.convId = c.id; this.msgs = [];
    if (this._syncChatStreamControls) this._syncChatStreamControls();
    if (this.switchMode) this.switchMode('chat');
    if (!skipWelcome) this.showWelcome();
    await this.loadConvs();
    this._refreshReportsButton();
    this._closeMobileSidebar();
    return requestId === this._convRequestId;
  },

  openConv: async function(id) {
    var requestId = this._convRequestId = (this._convRequestId || 0) + 1;
    var prevId = this.convId, prevMsgs = this.msgs, prevSkill = this.skill;
    this.convId = id;
    if (this._syncChatStreamControls) this._syncChatStreamControls();
    var s = Streams[id];
    if (s && s.active) {
      this.msgs = s.msgs; this.skill = s.skill || 'prism-research';
      this.renderAll(); this.loadConvs();
      this._reattachStream();
      this._refreshReportsButton();
      this._closeMobileSidebar();
      if (this._loadConversationCalls) this._loadConversationCalls(id);
      return;
    }
    // Try IndexedDB cache first for instant render (P5)
    var cached = _idb ? await _idb.getConv(id) : null;
    if (requestId !== this._convRequestId || this.convId !== id) return;
    if (cached) {
      this.msgs = cached.messages || [];
      this.skill = cached.skill || 'prism-research';
      this.renderAll();
    }
    var r;
    try { r = await this.api('/api/convs/'+encodeURIComponent(id)); }
    catch (error) {
      if (requestId !== this._convRequestId || this.convId !== id) return;
      this.convId = prevId; this.msgs = prevMsgs; this.skill = prevSkill;
      if (this._syncChatStreamControls) this._syncChatStreamControls();
      this.renderAll();
      if (this._toast) this._toast('会话加载失败，请检查连接后重试');
      return;
    }
    if (requestId !== this._convRequestId || this.convId !== id) return;
    if (!r.ok) {
      this.convId = prevId; this.msgs = prevMsgs; this.skill = prevSkill;
      if (this._syncChatStreamControls) this._syncChatStreamControls();
      this.renderAll();
      if (this._toast) this._toast('该会话不可用，已返回上一会话');
      console.error('openConv failed: ' + r.status + ' for ' + id);
      return;
    }
    var c = await r.json();
    if (requestId !== this._convRequestId || this.convId !== id) return;
    this.msgs = c.messages; this.skill = c.skill || 'prism-research';
    // Cache in IndexedDB for next time
    if (_idb) _idb.putConv(id, c).catch(function(){});
    if (cached) this.renderAll(); // re-render with fresh data
    else this.renderAll();
    this.loadConvs();
    this._refreshReportsButton();
    this._closeMobileSidebar();
    if (this._loadConversationCalls) this._loadConversationCalls(id);
  },

  /* Show/hide the reports button in the input zone based on what reports
     are available for the current conversation. */
  delConv: async function(id) {
    if (!confirm('将对话移到回收站？')) return;
    try {
      var r = await this.api('/api/convs/'+id, {method:'DELETE'});
      if (!r.ok) {
        var err = '删除失败 (' + r.status + ')';
        try { var b = await r.json(); err = b.detail || b.error || err; } catch(e) {}
        alert(err); return;
      }
      if (this.convId===id) { this.convId=''; this.msgs=[]; this.showWelcome(); }
    } catch(e) {
      console.error('delConv error:', e);
      alert('删除失败：网络异常，请检查网络后重试');
    } finally {
      this.loadConvs();
    }
  },

  /* ═══ Trash ═══ */
  openTrash: async function() {
    var r = await this.api('/api/trash'); if (!r.ok) return;
    var trash = await r.json();
    var body = document.getElementById('trashList');
    if (!trash.length) { body.innerHTML = '<div class="trash-empty">回收站为空</div>'; }
    else {
      var self = this;
      body.innerHTML = trash.map(function(t) {
        var d = new Date(t.deleted_at * 1000);
        var ds = d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
        return '<div class="trash-item"><span class="trash-title">'+esc(t.title)+'</span><span class="trash-date">'+ds+'</span><div class="trash-acts"><button onclick="App.restoreConv(\''+t.id+'\')">恢复</button><button class="danger" onclick="App.permDeleteConv(\''+t.id+'\')">永久删除</button></div></div>';
      }).join('');
    }
    document.getElementById('trashModal').style.display = 'flex';
  },
  closeTrash: function() { document.getElementById('trashModal').style.display = 'none'; },

  /* ═══ Market search modal (v0.6) ═══ */
  restoreConv: async function(id) {
    await this.api('/api/convs/'+id+'/restore', {method:'POST'});
    this.openTrash(); this.loadConvs();
  },
  permDeleteConv: async function(id) {
    if (!confirm('永久删除后无法恢复，确定？')) return;
    await this.api('/api/convs/'+id+'/permanent', {method:'DELETE'});
    this.openTrash(); this.loadConvs();
  },

  /* ═══ Skill ═══ */
  setSkill: function(s) {
    this.skill = ['prism-research','bottleneck-hunter','serenity'].indexOf(s) >= 0 ? s : 'prism-research';
    this._activeSkills = new Set([this.skill]);
  },

  /* ═══ Smart Scroll ═══ */
  logout: async function() {
    try {
      var response = await this.api('/api/logout', {method:'POST'});
      if (!response.ok) throw new Error('Logout failed');
      this._convRequestId = (this._convRequestId || 0) + 1;
      if (_idb && _idb.clear) { try { await _idb.clear(); } catch(e) {} }
      this.msgs = []; this.convId = ''; this.user = '';
      this._watchlist = []; this._watchlistOwner = '';
      try { sessionStorage.removeItem('prism_analysis'); } catch(e) {}
      window.location.href = '/login';
    } catch (error) {
      if (this._toast) this._toast('退出未完成，请检查网络后重试');
    }
  },

  /* ═══ Preferences (used by the guided tour) ═══ */
  _prefs: null,
  _loadPrefs: function() { if (!this._prefs) { try { this._prefs = JSON.parse(localStorage.getItem("prism_prefs") || "{}"); } catch(e) { this._prefs = {}; } } return this._prefs; },
  _savePrefs: function() { try { localStorage.setItem("prism_prefs", JSON.stringify(this._prefs)); } catch(e) {} },

  /* ═══ Scroll TOC ═══ */
};

/* ═══ Event Bindings ═══ */
document.addEventListener('DOMContentLoaded', function() { App.init(); });
document.getElementById('msgList').addEventListener('scroll', function() { App._updateTOC(); });
window.addEventListener('resize', function() { App._buildTOC(); });
document.addEventListener('click', function(e) {
  if (!e.target.closest('#acDropdown')&&!e.target.closest('#input')) App.hideAC();
  // 技能抽屉：点击外部（非抽屉、非 🛠 按钮）关闭
  var sd = document.getElementById('skillDrawer');
  if (sd && sd.style.display==='block' && !e.target.closest('#skillDrawer') && !e.target.closest('#cmpTools')) sd.style.display='none';
});
document.getElementById('input').addEventListener('keydown', function(e) {
  var key = e.key||e.code||'';
  if (key==='Enter'&&!e.shiftKey) { e.preventDefault(); if (App.selectAC()) return; App.send(); return; }
  if (key==='Tab'||e.keyCode===9) { e.preventDefault(); var dd=document.getElementById('acDropdown'); if (dd.style.display!=='none') { if (App.acIdx<0) { App.moveAC(1); } else { App.selectAC(); } return; } App.showAC(); return; }
  if (key==='ArrowDown'||e.keyCode===40) { e.preventDefault(); App.moveAC(1); return; }
  if (key==='ArrowUp'||e.keyCode===38) { e.preventDefault(); App.moveAC(-1); return; }
  if (key==='Escape'||e.keyCode===27) { App.hideAC(); return; }
});
document.getElementById('input').addEventListener('input', function() { App.showAC(); this.style.height='auto'; this.style.height=Math.min(140,Math.max(24,this.scrollHeight))+'px'; });
