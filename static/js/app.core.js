/* ═══════════════════════════════════════
   Prism — Application Logic (v0.8 split)
   ═══════════════════════════════════════ */

var Streams = {}; // Active SSE streams keyed by convId

/* IndexedDB helper for conversation cache (P5).
   Stores {id, conv} keyed by conversation ID so switching convs
   renders instantly from cache while the network fetch completes. */
var _idb = (function() {
  if (!window.indexedDB) return null;
  var _db = null;
  function open() {
    return new Promise(function(resolve, reject) {
      if (_db) return resolve(_db);
      var req = indexedDB.open('prism_cache', 1);
      req.onupgradeneeded = function(e) {
        e.target.result.createObjectStore('convs', {keyPath: 'id'});
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
          var req = tx.objectStore('convs').get(id);
          req.onsuccess = function() { resolve(req.result ? req.result.data : null); };
          req.onerror = function() { resolve(null); };
        });
      }).catch(function() { return null; });
    },
    putConv: function(id, data) {
      return open().then(function(db) {
        var tx = db.transaction('convs', 'readwrite');
        tx.objectStore('convs').put({id: id, data: data, ts: Date.now()});
      }).catch(function(){});
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
  user: '', skill: '', convId: '', msgs: [], acIdx: -1, acItems: [], skills: [],
  model: 'minimax-m3',
  _userScrolledUp: false, // Smart scroll — true when user manually scrolls away from bottom
  _activeReader: null,    // AbortController for stop button
  _bulkMode: false,       // Bulk select mode — true when checkboxes are visible
  _bulkChecked: {},       // {convId: true} for checked items
  _activeSkills: new Set(['cn']), // Multi-select skills; 'cn' (force-chinese) always pre-selected

  /* ═══ Init ═══ */
  init: async function() {
    var r = await this.api('/api/me'); if (!r.ok) return;
    this.user = (await r.json()).user;
    document.getElementById('userBadge').textContent = this.user;
    // Theme & width
    var saved = localStorage.getItem('cl-theme');
    if (saved === 'dark') document.documentElement.className = 'dark';
    else if (saved === 'light') document.documentElement.className = 'light';
    else { var h = new Date().getHours(); document.documentElement.className = (h >= 7 && h < 19) ? 'light' : 'dark'; }
    if (localStorage.getItem('cl-wide') === 'true') document.documentElement.classList.add('wide');
    this.loadSkills(); this.loadModels(); this.loadConvs(); this.showWelcome();
    this.renderSkillsBar();
    this._bindScroll();
    this._bindGlossClicks();
    this._bindMarketModal();
    this._loadWatchlist();
    this._bindMarketPanel();
    this._bindAnalysisSearch();
    // Register Service Worker for offline cache (P5)
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/static/sw.js').catch(function(){});
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
    opts = opts || {};
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
    var r = await fetch(url, opts);
    if (r.status === 401) window.location.href = '/login'; return r;
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
  },
  toggleWidth: function() {
    document.documentElement.classList.toggle('wide');
    localStorage.setItem('cl-wide', document.documentElement.classList.contains('wide'));
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
    if (trash.length) { link.textContent = '回收站 ' + trash.length; link.classList.remove('hidden'); }
    else { link.classList.add('hidden'); }
    // Show/hide analysis graph link based on whether a conv is active
    var gl = document.getElementById('graphLink');
    if (gl) gl.style.display = this.convId ? '' : 'none';
  },

  newConv: async function(skipWelcome) {
    var skillForNew = this.skill || 'bottleneck-hunter';
    var r = await this.api('/api/convs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill: skillForNew})});
    if (!r.ok) return; var c = await r.json();
    this.convId = c.id; this.msgs = [];
    if (!skipWelcome) this.showWelcome();
    await this.loadConvs();
    this._refreshReportsButton();
  },

  openConv: async function(id) {
    var prevId = this.convId, prevMsgs = this.msgs, prevSkill = this.skill;
    this.convId = id;
    var s = Streams[id];
    if (s && s.active) {
      this.msgs = s.msgs; this.skill = s.skill || 'bottleneck-hunter';
      this.renderAll(); this.loadConvs();
      this._reattachStream();
      this._refreshReportsButton();
      return;
    }
    // Try IndexedDB cache first for instant render (P5)
    var cached = _idb ? await _idb.getConv(id) : null;
    if (cached) {
      this.msgs = cached.messages || [];
      this.skill = cached.skill || 'bottleneck-hunter';
      this.renderAll();
    }
    var r = await this.api('/api/convs/'+id);
    if (!r.ok) {
      if (!cached) {
        this.convId = prevId; this.msgs = prevMsgs; this.skill = prevSkill;
      }
      console.error('openConv failed: ' + r.status + ' for ' + id);
      return;
    }
    var c = await r.json(); this.msgs = c.messages; this.skill = c.skill || 'bottleneck-hunter';
    // Cache in IndexedDB for next time
    if (_idb) _idb.putConv(id, c).catch(function(){});
    if (cached) this.renderAll(); // re-render with fresh data
    else this.renderAll();
    this.loadConvs();
    this._refreshReportsButton();
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
  setSkill: function(s) { this.skill = s; },

  /* ═══ Smart Scroll ═══ */
  logout: function() { document.cookie = 'token=;path=/;max-age=0'; window.location.href = '/login'; },

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
