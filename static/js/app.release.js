/* Prism release notes and new-version guidance. */
Object.assign(App, {
  _releaseData: null,

  _initReleaseNotes: function() {
    var tag = document.getElementById('versionTag');
    if (tag) {
      tag.setAttribute('role', 'button');
      tag.setAttribute('tabindex', '0');
      tag.title = '更新日志';
      tag.onclick = function() { App.openReleaseNotes(); };
      tag.onkeydown = function(e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); App.openReleaseNotes(); }
      };
    }
    this._loadReleaseNotes(true);
  },

  _loadReleaseNotes: async function(showGuide) {
    var r = await this.api('/api/releases');
    if (!r.ok) return null;
    var data = await r.json();
    this._releaseData = data;
    this._renderReleaseVersion(data);
    if (showGuide) this._maybeShowReleaseGuide(data);
    return data;
  },

  _renderReleaseVersion: function(data) {
    var tag = document.getElementById('versionTag');
    if (!tag || !data) return;
    tag.textContent = data.version || (data.latest && data.latest.version) || 'v0';
  },

  _renderReleaseMarkdownInline: function(text) {
    var raw = String(text || '');
    if (!raw) return '';
    try {
      if (typeof marked !== 'undefined') {
        var input = raw;
        if (typeof _balanceMarkdown === 'function') input = _balanceMarkdown(input);
        if (typeof _preGlossify === 'function') input = _preGlossify(input);
        input = typeof escapeAllGt === 'function' ? escapeAllGt(input) : input.replace(/>/g, '&gt;');
        var html = typeof marked.parseInline === 'function'
          ? marked.parseInline(input)
          : marked.parse(input).replace(/^<p>/, '').replace(/<\/p>\s*$/, '');
        return typeof sanitizeHtml === 'function' ? sanitizeHtml(html) : html;
      }
    } catch(e) {}
    return esc(raw);
  },

  _releaseSummaryItems: function(release, limit) {
    var items = [];
    var sections = (release && release.sections) || [];
    for (var i = 0; i < sections.length; i++) {
      var rows = sections[i].items || [];
      for (var j = 0; j < rows.length; j++) {
        items.push(rows[j]);
        if (items.length >= limit) return items;
      }
    }
    return items;
  },

  _maybeShowReleaseGuide: function(data) {
    if (!data || !data.version) return;
    var key = 'prism_seen_release';
    try {
      if (localStorage.getItem(key) === data.version) return;
      localStorage.setItem(key, data.version);
    } catch(e) {}
    var latest = data.latest || {};
    var items = this._releaseSummaryItems(latest, 3);
    var box = document.createElement('div');
    box.className = 'guide-toast release-guide show';
    box.innerHTML = '<div class="guide-toast-hd"><span>新版本 ' + esc(data.version) + '</span>' +
      '<button class="guide-toast-dismiss" onclick="this.closest(\'.release-guide\').remove()">关闭</button></div>' +
      '<div class="guide-toast-body">' +
      (items.length ? '<ul>' + items.map(function(x) { return '<li>' + App._renderReleaseMarkdownInline(x) + '</li>'; }).join('') + '</ul>' : '已更新到最新版本。') +
      '<button class="release-guide-more" onclick="App.openReleaseNotes();this.closest(\'.release-guide\').remove()">查看更新</button>' +
      '</div>';
    document.body.appendChild(box);
    setTimeout(function() { if (box && box.parentNode) box.remove(); }, 12000);
  },

  openReleaseNotes: async function() {
    var data = this._releaseData || await this._loadReleaseNotes(false);
    var modal = document.getElementById('releaseModal');
    var body = document.getElementById('releaseList');
    if (!modal || !body) return;
    if (!data) {
      body.innerHTML = '<div class="admin-empty">暂时无法加载更新日志</div>';
    } else {
      body.innerHTML = this._renderReleaseList(data);
    }
    modal.style.display = 'flex';
  },

  closeReleaseNotes: function() {
    var modal = document.getElementById('releaseModal');
    if (modal) modal.style.display = 'none';
  },

  _renderReleaseList: function(data) {
    var releases = data.releases || [];
    var rev = data.deployed_revision ? '<span class="release-rev">部署 ' + esc(data.deployed_revision) + '</span>' : '';
    if (!releases.length) return '<div class="admin-empty">暂无更新日志</div>';
    return '<div class="release-headline"><span>当前版本 ' + esc(data.version || '') + '</span>' + rev + '</div>' +
      releases.map(function(rel) {
        var sections = (rel.sections || []).map(function(sec) {
          var items = (sec.items || []).map(function(item) { return '<li>' + App._renderReleaseMarkdownInline(item) + '</li>'; }).join('');
          return '<section class="release-section"><h4>' + esc(sec.title || 'Changed') + '</h4><ul>' + items + '</ul></section>';
        }).join('');
        return '<article class="release-entry">' +
          '<div class="release-entry-hd"><strong>' + esc(rel.version || '') + '</strong><span>' + esc(rel.date || '') + '</span></div>' +
          sections +
          '</article>';
      }).join('');
  }
});
