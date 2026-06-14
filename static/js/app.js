/* ═══════════════════════════════════════
   Prism — Application Logic
   ═══════════════════════════════════════ */

var Streams = {}; // Active SSE streams keyed by convId

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
  },

  loadSkills: async function() {
    var r = await fetch('/api/skills');
    if (r.ok) { this.skills = await r.json(); this.renderSkillsBar(); }
  },

  /* ═══ Skills bar ═══ */
  renderSkillsBar: function() {
    var bar = document.getElementById('skillsBar');
    if (!bar || !this.skills.length) return;
    var self = this;
    bar.innerHTML = '<span class="skills-bar-label">工具</span>' +
      this.skills.map(function(s) {
        var active = self._activeSkills.has(s.id);
        var locked = s.id === 'cn'; // force-chinese always on
        return '<span class="skill-chip' + (active ? ' active' : '') + (locked ? ' locked' : '') + '" data-skill="' + s.id + '" onclick="App.selectSkillChip(\'' + s.id + '\')">' + esc(s.name) + (locked ? ' ·' : '') + '<span class="chip-kbd">' + esc(s.shortcut) + '</span></span>';
      }).join('');
  },

  selectSkillChip: function(skillId) {
    if (skillId === 'cn') return; // force-chinese cannot be toggled off
    if (this._activeSkills.has(skillId)) {
      this._activeSkills.delete(skillId);
    } else {
      this._activeSkills.add(skillId);
    }
    this.renderSkillsBar();
  },

  clearSkill: function() {
    this._activeSkills = new Set(['cn']); // Keep cn, clear others
    this.renderSkillsBar();
  },

  updateSkillTagInInput: function() {
    // No longer used — skills shown in bar only
  },

  api: async function(url, opts) {
    opts = opts || {}; var r = await fetch(url, opts);
    if (r.status === 401) window.location.href = '/login'; return r;
  },

  /* ═══ Theme ═══ */
  toggleTheme: function() {
    var c = document.documentElement.className.replace('wide','').trim();
    var n = (c === 'dark') ? 'light' : 'dark';
    document.documentElement.className = n + (document.documentElement.classList.contains('wide') ? ' wide' : '');
    localStorage.setItem('cl-theme', n);
  },
  toggleWidth: function() {
    document.documentElement.classList.toggle('wide');
    localStorage.setItem('cl-wide', document.documentElement.classList.contains('wide'));
  },

  /* ═══ Welcome ═══ */
  showWelcome: function() {
    document.getElementById('msgList').innerHTML = '<div class="welcome"><h2>Prism</h2><p>聚焦 AI 数据中心 · 半导体 · 新能源 · 机器人</p><div class="qa-row"><span class="qa-chip" onclick="App.quickAsk(\'AI数据中心液冷供应链上游瓶颈\')">液冷供应链</span><span class="qa-chip" onclick="App.quickAsk(\'光模块有哪些被低估的上游标的？\')">光模块上游</span><span class="qa-chip" onclick="App.quickAsk(\'HBM4钼替代钨，上游有什么被低估的标的？\')">HBM4·钼替钨</span><span class="qa-chip" onclick="App.quickAsk(\'800VDC车载电源器件卡脖子环节\')">800VDC电源</span><span class="qa-chip" onclick="App.quickAsk(\'人形机器人减速器投资机会\')">机器人减速器</span><span class="qa-chip" onclick="App.quickAsk(\'铜连接产业链上游瓶颈\')">铜连接</span></div></div>';
  },

  /* ═══ Models ═══ */
  loadModels: async function() {
    var r = await fetch('/api/models'); if (!r.ok) return;
    var s = document.getElementById('modelSelect');
    s.innerHTML = (await r.json()).map(function(m){return '<option value="'+m.id+'"'+(m.id===App.model?' selected':'')+'>'+m.name+'</option>'}).join('');
  },
  setModel: function(m) { this.model = m; },

  /* ═══ Conversations ═══ */
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
        return '<div class="conv-item'+(c.id===self.convId?' active':'')+'" data-id="'+escAttr(c.id)+'" onclick="App.openConv(\''+c.id+'\')" title="'+escAttr(c.title)+'">' +
          '<input type="checkbox" class="conv-cb'+(self._bulkMode?' visible':'')+'" ' + checked +
          ' onclick="event.stopPropagation();App._toggleCheck(\''+c.id+'\',event)" title="选择">' +
          esc(c.title) +
          '<span class="del" onclick="event.stopPropagation();App.delConv(\''+c.id+'\')">×</span></div>';
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
  },

  newConv: async function(skipWelcome) {
    var skillForNew = this.skill || 'bottleneck-hunter';
    var r = await this.api('/api/convs', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill: skillForNew})});
    if (!r.ok) return; var c = await r.json();
    this.convId = c.id; this.msgs = [];
    if (!skipWelcome) this.showWelcome();
    await this.loadConvs();
  },

  openConv: async function(id) {
    var prevId = this.convId, prevMsgs = this.msgs, prevSkill = this.skill;
    this.convId = id;
    var s = Streams[id];
    if (s && s.active) {
      this.msgs = s.msgs; this.skill = s.skill || 'bottleneck-hunter';
      this.renderAll(); this.loadConvs(); return;
    }
    var r = await this.api('/api/convs/'+id);
    if (!r.ok) {
      // Restore previous state on failure
      this.convId = prevId; this.msgs = prevMsgs; this.skill = prevSkill;
      console.error('openConv failed: ' + r.status + ' for ' + id);
      return;
    }
    var c = await r.json(); this.msgs = c.messages; this.skill = c.skill || 'bottleneck-hunter';
    this.renderAll(); this.loadConvs();
  },

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
  _bindScroll: function() {
    var self = this;
    var el = document.getElementById('msgList');
    el.addEventListener('scroll', function() {
      var dist = el.scrollHeight - el.scrollTop - el.clientHeight;
      self._userScrolledUp = dist > 80;
    });
  },

  _bindGlossClicks: function() {
    var self = this;
    document.addEventListener('click', function(e) {
      var termEl = e.target.closest('.gloss-term');
      if (termEl) {
        e.preventDefault();
        var term = termEl.getAttribute('data-term');
        self._handleGlossClick(term, termEl);
      } else if (!e.target.closest('#glossPanel')) {
        document.getElementById('glossPanel').style.display = 'none';
      }
    });
  },

  _scrollToBottom: function(force) {
    if (force || !this._userScrolledUp) {
      var el = document.getElementById('msgList');
      el.scrollTo({ top: el.scrollHeight, behavior: force ? 'instant' : 'smooth' });
    }
  },

  /* ═══ Typewriter effect — character-by-character reveal ═══ */
  _twTimer: null,

  _startTypewriter: function() {
    if (this._twTimer) return;
    var self = this;
    function tick() {
      var anyActive = false;
      for (var sid in Streams) {
        if (!Streams[sid].active) continue;
        anyActive = true;
        var s = Streams[sid];
        var m = s.msgs[s.msgIdx];
        if (!m || m.role !== 'assistant') continue;
        if (m._displayLen === undefined) m._displayLen = 0;
        // Calculate total text length from chunks
        var total = 0;
        (m._chunks || []).forEach(function(c) { if (c.t === 'text') total += c.c.length; });
        if (m._displayLen < total) {
          // Reveal 2-5 chars per frame for natural reading pace
          m._displayLen = Math.min(total, m._displayLen + 2 + Math.floor(Math.random() * 4));
          if (App.convId === sid) {
            var curMsgIdx = App.msgs.length - 1;
            if (curMsgIdx >= 0 && App.msgs[curMsgIdx].role === 'assistant') App.renderOne(curMsgIdx);
          }
        }
      }
      if (anyActive) {
        self._twTimer = requestAnimationFrame(tick);
      } else {
        self._twTimer = null;
      }
    }
    this._twTimer = requestAnimationFrame(tick);
  },

  _stopTypewriter: function() {
    if (this._twTimer) {
      cancelAnimationFrame(this._twTimer);
      this._twTimer = null;
    }
    // Clear displayLens so next renderAll shows full content
    for (var sid in Streams) {
      var m = Streams[sid].msgs[Streams[sid].msgIdx];
      if (m) m._displayLen = undefined;
    }
  },

  /* ═══ Rendering ═══ */
  _renderMsg: function(chunks, fullText, streaming, msgObj) {
    if (!chunks || !chunks.length) return render(fullText || '');
    try {
      var html = '', pendingTools = [];

      function cleanDetail(d) {
        if (!d) return '';
        return d.replace(/^\['/, '').replace(/'\]$/, '').replace(/^\["/, '').replace(/"\]$/, '');
      }

      function flushTools(lastIsNow) {
        if (!pendingTools.length) return;
        var n = pendingTools.length;
        if (lastIsNow && streaming) {
          // Streaming: show fold + current step prominently
          html += '<div class="step-bar"><details class="step-fold"><summary>已完成 '+n+' 步</summary>';
          for (var i = 0; i < n; i++) {
            var td = pendingTools[i];
            html += '<span class="step-tag"><b>'+esc(td.name)+'</b>'+(td.detail?' <i title="'+escAttr(td.detail)+'">'+esc(td.detail)+'</i>':'')+'</span>';
          }
          html += '</details>';
          var last = pendingTools[n-1];
          html += '<span class="step-tag now step-now"><b>'+esc(last.name)+'</b>'+(last.detail?' <i title="'+escAttr(last.detail)+'">'+esc(last.detail)+'</i>':'')+'</span></div>';
        } else {
          // Non-streaming or completed: fold all steps
          html += '<div class="step-bar stable"><details class="step-fold"><summary>已完成 '+n+' 步</summary>';
          for (var i = 0; i < n; i++) {
            var td = pendingTools[i];
            html += '<span class="step-tag"><b>'+esc(td.name)+'</b>'+(td.detail?' <i title="'+escAttr(td.detail)+'">'+esc(td.detail)+'</i>':'')+'</span>';
          }
          html += '</details></div>';
        }
        pendingTools = [];
      }

      // Build references from tool results collected during streaming
      var refs = (msgObj && msgObj._refs) || [];

      for (var ci = 0; ci < chunks.length; ci++) {
        var c = chunks[ci];
        if (c.t === 'tool') {
          pendingTools.push({name: c.c, detail: cleanDetail(c.d || '')});
        } else if (c.t === 'think') {
          // Render immediately — interleave with tools, don't defer
          flushTools(false);
          html += '<details class="think-box" open><summary>Thinking</summary><div class="think-content">'+render(c.c)+'</div></details>';
        } else if (c.t === 'ask') {
          flushTools(false);
          html += '<div class="ask-card"><div class="ask-q">'+esc(c.q)+'</div><div class="ask-opts">';
          for (var oi = 0; oi < (c.opts || []).length; oi++) {
            html += '<button class="ask-opt" onclick="App.answerAsk(\''+escAttr(c.opts[oi])+'\')">'+esc(c.opts[oi])+'</button>';
          }
          html += '</div></div>';
        } else if (c.t === 'ref') {
          refs.push({url: c.url, title: c.title || c.url, snippet: c.snippet || ''});
        } else {
          var isLastText = streaming && ci === chunks.length - 1;
          flushTools(false);
          html += render(c.c) + (isLastText ? '<span class="stream-cursor"></span>' : '');
        }
      }

      if (streaming) {
        flushTools(true);
        if (html === '') html = '<div class="shimmer-bar" style="width:60%"></div>';
      } else {
        flushTools(false);
      }

      // Append reference section if we have refs and this is the final render
      if (refs.length && !streaming) {
        html += '<div class="ref-section"><div class="ref-title">参考来源 ('+refs.length+')</div><div class="ref-list">';
        for (var ri = 0; ri < refs.length; ri++) {
          var ref = refs[ri];
          html += '<div class="ref-item"><span class="ref-idx">'+(ri+1)+'</span><div><a class="ref-link" href="'+escAttr(ref.url)+'" target="_blank" rel="noopener">'+esc(ref.title)+'</a>';
          if (ref.snippet) html += '<div class="ref-snippet">'+esc(ref.snippet)+'</div>';
          html += '</div></div>';
        }
        html += '</div></div>';
      }

      return html;
    } catch(e) {
      console.error('_renderMsg error:', e);
      return render(fullText || '');
    }
  },

  renderAll: function() {
    var el = document.getElementById('msgList');
    if (!this.msgs.length) { this.showWelcome(); return; }
    var self = this;
    el.innerHTML = this.msgs.map(function(m) {
      if (m.role === 'user') return '<div class="msg user"><div class="msg-inner"><div class="msg-byline">You</div><div class="bubble">'+esc(m.content)+'</div></div></div>';
      try {
        var text = m.content || '', chunks = [];
        if (typeof text === 'string' && text.startsWith('{"text":')) {
          try { var p = JSON.parse(text); text = p.text || ''; chunks = p.chunks || []; } catch(e) {}
        }
        // Pass refs through for final render
        if (!m._refs && m._chunks) {
          m._refs = [];
          for (var ci = 0; ci < m._chunks.length; ci++) {
            if (m._chunks[ci].t === 'ref') m._refs.push(m._chunks[ci]);
          }
        }
        // Attach refs to the plain msg object so _renderMsg can see them
        var bubble = self._renderMsg(chunks, text, false, m);
        bubble = self._glossifyTerms(self._linkifyReports(bubble));
        return '<div class="msg assistant"><div class="msg-inner"><div class="msg-byline">Prism</div><div class="bubble">'+bubble+'</div></div></div>';
      } catch(e) {
        return '<div class="msg assistant"><div class="msg-inner"><div class="msg-byline">Prism</div><div class="bubble">'+render(m.content||'')+'</div></div></div>';
      }
    }).join('');
    this._addCopyButtons(el);
    el.scrollTop = el.scrollHeight;
    this._buildTOC();
  },

  renderOne: function(idx) {
    var el = document.getElementById('msgList'), bubbles = el.querySelectorAll('.bubble');
    if (!bubbles[idx]) return;
    var m = this.msgs[idx];
    if (!m || m.role !== 'assistant') return;
    var chunks = m._chunks || [];
    var streaming = !!(Streams[this.convId] && Streams[this.convId].active);

    if (chunks.length === 0 && !m.content) {
      bubbles[idx].innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
      this._scrollToBottom(false); return;
    }
    if (chunks.length === 0 && m.content) {
      bubbles[idx].innerHTML = this._glossifyTerms(this._linkifyReports(render(m.content)));
      this._scrollToBottom(false); return;
    }

    // ── Typewriter: truncate text chunks to display length ──
    var displayLen = m._displayLen;
    var displayChunks = chunks;
    var displayContent = m.content || '';
    if (streaming && displayLen !== undefined) {
      displayChunks = [];
      displayContent = '';
      var charCount = 0;
      for (var ci = 0; ci < chunks.length; ci++) {
        var c = chunks[ci];
        if (c.t === 'text') {
          var remaining = displayLen - charCount;
          if (remaining <= 0) break;
          var portion = c.c.substring(0, remaining);
          displayChunks.push({t: 'text', c: portion});
          displayContent += portion;
          charCount += portion.length;
          if (charCount >= displayLen) break;
        } else {
          displayChunks.push(c);
        }
      }
    }

    var chunkSig = JSON.stringify(chunks);
    if (!streaming || displayLen === undefined) {
      if (m._lastSig === chunkSig) return;
    }
    m._lastSig = chunkSig;

    // Save open state of fold elements to prevent flicker during re-render
    var openFolds = [];
    if (bubbles[idx]) {
      bubbles[idx].querySelectorAll('.step-fold[open]').forEach(function(f, i) { openFolds.push(i); });
    }
    bubbles[idx].innerHTML = this._glossifyTerms(this._linkifyReports(this._renderMsg(displayChunks, displayContent || m.content, streaming, m)));
    // Restore open state
    if (openFolds.length) {
      var newFolds = bubbles[idx].querySelectorAll('.step-fold');
      openFolds.forEach(function(i) { if (newFolds[i]) newFolds[i].setAttribute('open', ''); });
    }
    this._addCopyButtons(bubbles[idx]);
    this._scrollToBottom(false);
    if (this.msgs.filter(function(m){return m.role==='user'}).length > 0) this._buildTOC();
  },

  /* ═══ Copy buttons on code blocks ═══ */
  _addCopyButtons: function(container) {
    container.querySelectorAll('pre').forEach(function(pre) {
      if (pre.querySelector('.copy-btn')) return;
      pre.style.position = 'relative';
      var btn = document.createElement('button');
      btn.className = 'copy-btn';
      btn.textContent = '复制';
      btn.onclick = function() {
        var code = pre.querySelector('code');
        var text = code ? code.textContent : pre.textContent;
        navigator.clipboard.writeText(text).then(function() {
          btn.textContent = '✓ 已复制';
          setTimeout(function() { btn.textContent = '复制'; }, 2000);
        }).catch(function() {
          btn.textContent = '失败';
          setTimeout(function() { btn.textContent = '复制'; }, 2000);
        });
      };
      pre.appendChild(btn);
    });
  },

  // ── Financial Glossary (built-in definitions for instant hover) ──
  _glossary: {
    "市盈率": "股票价格除以每股收益的比率，衡量估值高低。高PE可能意味着高增长预期或泡沫。",
    "PE": "Price-to-Earnings ratio，即市盈率。股价÷每股盈利，是最常用的估值指标之一。",
    "市净率": "股票价格除以每股净资产的比率，常用于银行、保险等重资产行业的估值。",
    "PB": "Price-to-Book ratio，即市净率。股价÷每股净资产，低于1可能意味着低估。",
    "ROE": "净资产收益率，衡量公司利用股东资金赚钱的效率。ROE=净利润÷净资产。",
    "ROA": "总资产收益率，衡量公司利用全部资产赚钱的效率。ROA=净利润÷总资产。",
    "毛利率": "（营业收入-营业成本）÷营业收入。反映产品本身的盈利空间，越高越好。",
    "净利率": "净利润÷营业收入。反映公司最终赚钱能力，扣除了所有费用和税。",
    "EPS": "每股收益，净利润÷总股本。是计算PE的基础，增长趋势比绝对值更重要。",
    "EBITDA": "息税折旧摊销前利润。剔除非现金支出和资本结构影响，常用于跨公司比较。",
    "自由现金流": "经营现金流减去资本支出。反映企业真正可以自由支配的现金。",
    "FCF": "Free Cash Flow，自由现金流。FCF>0的公司有自我造血能力。",
    "护城河": "企业持续的竞争优势，能阻止竞争对手侵蚀其市场份额和利润。",
    "市占率": "企业销售额占整个市场总销售额的比例，反映竞争地位。",
    "龙头": "行业中规模最大、竞争力最强的公司。通常有定价权和品牌溢价。",
    "TAM": "Total Addressable Market，总可寻址市场规模。指产品或服务的最大潜在市场。",
    "CAGR": "复合年增长率。衡量一段时间内投资的年均增长速度，比简单平均更准确。",
    "YoY": "Year-over-Year，同比增长率。与去年同期相比的增长幅度。",
    "QoQ": "Quarter-over-Quarter，环比增长率。与上一季度相比的增长幅度。",
    "供应链": "从原材料到最终产品的所有环节。供应链瓶颈会影响产能和成本。",
    "上游": "产业链中靠近原材料的一端。上游企业提供基础材料和零部件。",
    "下游": "产业链中靠近最终消费者的一端。下游企业做组装、品牌、销售。",
    "产能": "企业或行业在一定时期内的最大生产能力。产能利用率高说明需求旺盛。",
    "良率": "半导体/制造业中合格产品占总产出的比例。良率直接影响成本和利润。",
    "国产替代": "用国产产品替代进口产品，通常由政策推动或供应链安全驱动。",
    "景气度": "行业的繁荣程度。高景气=需求旺盛+利润好；低景气=产能过剩+价格战。",
    "周期股": "业绩随经济周期大幅波动的股票。在行业低谷买入、高峰卖出是常见策略。",
    "成长股": "收入和利润增长速度快于市场平均的公司股票。通常PE较高。",
    "价值股": "股价低于内在价值的公司股票。通常PE较低、分红稳定。",
    "分红率": "每股分红÷每股收益，反映公司将多少利润分给股东。",
    "股息率": "每股分红÷股价，衡量投资该股票能获得的现金回报率。",
    "商誉": "收购价格超出被收购公司净资产的部分。商誉减值会直接冲击利润。",
    "负债率": "总负债÷总资产。衡量公司杠杆水平，过高意味着财务风险大。",
    "流动性": "资产快速变现而不损失价值的能力。也指市场交易活跃程度。",
    "估值": "对公司或资产价值的评估。常用方法有PE法、PB法、DCF法等。",
    "DCF": "折现现金流模型。将未来现金流折现到现在来估算企业内在价值。",
    "风险溢价": "投资者承担额外风险所要求的额外回报。风险越高，溢价越大。",
    "贝塔": "衡量股票相对市场波动的指标。β>1比市场波动大，β<1比市场稳定。",
    "ETF": "交易所交易基金。像股票一样买卖的一篮子证券组合。",
    "做多": "买入并持有，预期价格上涨。",
    "做空": "借入证券卖出，预期价格下跌后买回还券赚差价。",
    "多头": "看好后市、买入持有的投资者。",
    "空头": "看空后市、卖出或做空的投资者。",
    "换手率": "一定时间内股票转手买卖的频率。高换手率说明交易活跃。",
    "流动性溢价": "流动性差的资产需要提供更高回报来补偿投资者。",
    "范式转移": "行业或市场的基本运行方式发生根本性变化。如AI对芯片行业的改变。",
    "资本开支": "企业用于购买或升级固定资产的支出。高资本开支行业需要大量投资。",
    "CAPEX": "Capital Expenditure，资本开支。重资产行业（如芯片制造）CAPEX巨大。",
    "摩尔定律": "集成电路上可容纳的晶体管数约每两年翻一倍。正在接近物理极限。",
    "先进封装": "将多个芯片集成在一个封装内的技术，是突破摩尔定律瓶颈的关键。",
    "AI芯片": "专门为人工智能计算优化的处理器，包括GPU、NPU、ASIC等。",
    "GPU": "图形处理器。NVIDIA的GPU是当前AI训练和推理的主流芯片。",
    "HBM": "高带宽存储器。将DRAM芯片堆叠在一起，是AI芯片的关键配套组件。",
    "液冷": "使用液体冷却服务器的方法。AI数据中心功耗大，液冷正在取代传统风冷。",
    "光模块": "光电信号转换器件。数据中心高速互联的核心组件，速率不断提升。",
    "铜缆互联": "用铜线连接服务器。在短距离场景比光纤更便宜，但带宽有限。",
    "氟化液": "用于浸没式液冷的绝缘液体。3M退出后供应缺口大，是液冷关键卡点。",
    "导热界面材料": "填充芯片和散热器之间空隙的材料。虽不起眼但对散热效果至关重要。",
    "IDC": "Internet Data Center，互联网数据中心。存服务器的地方，AI时代需求暴增。",
    "SaaS": "Software as a Service，软件即服务。按订阅收费的云软件模式。",
    "PaaS": "Platform as a Service，平台即服务。提供云上开发和部署环境。",
    "IaaS": "Infrastructure as a Service，基础设施即服务。提供虚拟化的计算资源。",
    "边缘计算": "在靠近数据产生的地方进行计算，而非全部传到云端。适合实时场景。",
    "私有化": "上市公司被收购并从交易所退市。通常收购方支付溢价。",
    "回购": "公司用自己的现金买回自己的股票。减少流通股数量，推高EPS。",
    "定增": "定向增发，向特定投资者发行新股融资。会稀释现有股东权益。",
    "解禁": "限售股解除锁定可以自由交易。大量解禁可能带来抛压。",
    "AH股溢价": "同一公司A股（大陆）和H股（香港）的价格差异。反映两地市场偏好。",
    "北向资金": "通过沪/深港通从香港流入A股的外资。被视为“聪明钱”的风向标。",
    "南向资金": "通过沪/深港通从内地流入港股的资金。",
    "量化宽松": "央行购买金融资产向市场注入流动性。通常利好股市。",
    "加息": "央行提高基准利率。通常利空股市（资金成本上升、估值压缩）。",
    "降息": "央行降低基准利率。通常利好股市（资金成本降低、估值扩张）。",
    "通胀": "物价持续上涨。适度通胀利好经济，恶性通胀破坏经济。",
    "通缩": "物价持续下跌。比通胀更可怕，会导致消费和生产萎缩。",
    "PMI": "采购经理人指数。>50表示制造业扩张，<50表示收缩。是经济先行指标。",
    "CPI": "居民消费价格指数。衡量通胀水平的核心指标。",
    "PPI": "工业生产者出厂价格指数。衡量工业品价格变化，是CPI的先行指标。",
    "GDP": "国内生产总值。衡量一国经济规模的核心指标。",
    "M2": "广义货币供应量。M2增速高于GDP增速通常意味着流动性充裕。",
    "LPR": "贷款市场报价利率。中国最重要的基准贷款利率，影响房贷和企业贷款成本。",
    "MLF": "中期借贷便利。央行向银行提供中期资金的工具，利率影响LPR。",
    "社融": "社会融资规模。衡量实体经济从金融体系获得的资金总量。"
  },

  _glossifyTerms: function(html) {
    if (!html) return html;
    var self = this;
    // 1. Process ==term== explicit markup from AI (most reliable, preferred)
    html = html.replace(/==([^=]+?)==/g, function(m, term) {
      var def = self._glossary[term.trim()] || '';
      return '<span class="gloss-term gloss-explicit" data-term="'+escAttr(term.trim())+'" title="'+escAttr(def || '点击查看AI解释')+'">'+esc(term.trim())+'</span>';
    });
    // 2. Scan for glossary terms only in visible text segments (between > and <)
    html = html.replace(/>([^<]+)</g, function(match, text) {
      var terms = Object.keys(self._glossary).sort(function(a,b){return b.length-a.length;});
      for (var ti = 0; ti < terms.length; ti++) {
        var t = terms[ti];
        // Only match whole words for short English acronyms (<5 chars), any occurrence for Chinese
        var re;
        if (/^[A-Za-z]+$/.test(t) && t.length <= 4) {
          re = new RegExp('\\b(' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')\\b', 'gi');
        } else {
          re = new RegExp('(' + t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
        }
        text = text.replace(re, function(m, captured) {
          return '<span class="gloss-term" data-term="'+escAttr(t)+'" title="'+escAttr(self._glossary[t])+'">'+captured+'</span>';
        });
      }
      return '>' + text + '<';
    });
    return html;
  },

  _handleGlossClick: function(term, el) {
    var panel = document.getElementById('glossPanel');
    var def = this._glossary[term];
    if (def) {
      // Show built-in definition immediately
      panel.innerHTML = '<div class="gloss-panel-hd"><b>'+esc(term)+'</b><button class="gloss-panel-close" onclick="document.getElementById(\'glossPanel\').style.display=\'none\'">×</button></div><div class="gloss-panel-body">'+esc(def)+'<div class="gloss-ai-cta" onclick="App._aiExplain(\''+escAttr(term)+'\')"><span class="ai-cta-icon"></span>AI 深入解释</div></div>';
    } else {
      panel.innerHTML = '<div class="gloss-panel-hd"><b>'+esc(term)+'</b><button class="gloss-panel-close" onclick="document.getElementById(\'glossPanel\').style.display=\'none\'">×</button></div><div class="gloss-panel-body"><div class="gloss-ai-cta" onclick="App._aiExplain(\''+escAttr(term)+'\')"><span class="ai-cta-icon"></span>AI 解释此术语</div></div>';
    }
    // Position near the clicked element
    var rect = el.getBoundingClientRect();
    panel.style.top = Math.min(rect.bottom + 6, window.innerHeight - 160) + 'px';
    panel.style.left = Math.max(8, rect.left) + 'px';
    panel.style.display = 'block';
  },

  _aiExplain: async function(term) {
    var panel = document.getElementById('glossPanel');
    var body = panel.querySelector('.gloss-panel-body');
    if (body) body.innerHTML = '<div class="shimmer-bar" style="width:40%"></div><div style="font-size:11px;color:var(--text3);margin-top:4px">正在生成解释…</div>';
    try {
      var r = await fetch('/api/explain-term', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({term: term, context: ''})
      });
      if (r.ok) {
        var data = await r.json();
        var cachedTag = data.cached ? ' <span class="gloss-cached-tag">(缓存)</span>' : '';
        panel.innerHTML = '<div class="gloss-panel-hd"><b>'+esc(term)+'</b>'+cachedTag+'<button class="gloss-panel-close" onclick="document.getElementById(\'glossPanel\').style.display=\'none\'">×</button></div><div class="gloss-panel-body">'+esc(data.explanation)+'</div>';
      } else {
        body.innerHTML = '<span style="color:var(--text3)">AI解释暂不可用，请稍后重试</span>';
      }
    } catch(e) {
      if (body) body.innerHTML = '<span style="color:var(--text3)">网络异常，请检查连接</span>';
    }
  },

  _linkifyReports: function(html) {
    // Auto-link report file paths like reports/xxx.html
    if (!this.convId || !html) return html;
    return html.replace(/(?<!["'=>])(reports\/[^\s<>"')]+?\.(?:html|htm|pdf|csv|md))/gi, function(match) {
      var fname = match.replace(/^reports\//, '');
      return '<a href="/api/reports/' + App.convId + '/' + encodeURIComponent(fname) + '" target="_blank" class="report-link" title="在新标签页打开报告">' + match + '</a>';
    });
  },

  /* ═══ Send & Stream ═══ */
  quickAsk: function(q) { document.getElementById('input').value = q; this.send(); },
  answerAsk: function(answer) {
    var inp = document.getElementById('input'); inp.value = answer; inp.focus();
  },

  showAC: function() {
    var inp = document.getElementById('input'), val = inp.value, pos = inp.selectionStart,
        before = val.substring(0, pos), m = before.match(/\/(\S*)$/),
        dd = document.getElementById('acDropdown');
    if (!m) { dd.style.display = 'none'; return; }
    var query = m[1].toLowerCase();
    if (!query && this.skills.length) {
      this.acIdx = -1; this.acItems = this.skills;
      this._renderAC(dd, inp); return;
    }
    var items = this.skills.filter(function(s) {
      return s.id.toLowerCase().indexOf(query)>=0 || s.name.indexOf(query)>=0 || s.shortcut.indexOf(query)>=0;
    });
    if (!items.length) { dd.style.display = 'none'; return; }
    this.acIdx = -1; this.acItems = items;
    this._renderAC(dd, inp);
  },
  _renderAC: function(dd, inp) {
    var self = this;
    dd.innerHTML = '<div class="ac-header"><span>Function</span><span class="ac-header-hint">⇥ select · ↓↑ nav</span></div>' +
      this.acItems.map(function(s, i) {
        return '<div class="ac-item' + (i === self.acIdx ? ' sel' : '') + '" onclick="App.selectSkill(\'' + s.id + '\')">' +
          '<span class="ac-name">' + esc(s.name) + '</span>' +
          '<span class="ac-desc">' + esc(s.shortcut) + '</span>' +
        '</div>';
      }).join('');
    var rect = inp.getBoundingClientRect();
    dd.style.left = rect.left + 'px';
    dd.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
    dd.style.display = 'block';
  },
  hideAC: function() { document.getElementById('acDropdown').style.display = 'none'; this.acIdx = -1; },
  moveAC: function(dir) { if (document.getElementById('acDropdown').style.display === 'none') return; this.acIdx = (this.acIdx+dir+this.acItems.length)%this.acItems.length; document.getElementById('acDropdown').querySelectorAll('.ac-item').forEach(function(item,i){item.className='ac-item'+(i===App.acIdx?' sel':'')}); },
  selectAC: function() { if (document.getElementById('acDropdown').style.display!=='none'&&this.acIdx>=0&&this.acItems[this.acIdx]){this.selectSkill(this.acItems[this.acIdx].id);return true}return false; },
  selectSkill: function(id) { var inp = document.getElementById('input'), val = inp.value, pos = inp.selectionStart, before = val.substring(0,pos), after = val.substring(pos); before = before.replace(/\/\S*$/, ''); inp.value = before.trim()?before.trim()+' '+after:after; this.setSkill(id); if (id !== 'cn') this._activeSkills.add(id); this.renderSkillsBar(); this.hideAC(); inp.focus(); },

  send: async function() {
    if (this.selectAC()) return;
    var inp = document.getElementById('input'), q = inp.value.trim(); if (!q) return;
    var m = q.match(/^\/([a-zA-Z一-龥-]+)\s*(.*)/);
    if (m) {
      var cmd = m[1].toLowerCase();
      var found = this.skills.filter(function(s) { return s.shortcut === '/' + cmd || s.id === cmd || s.name === cmd; })[0];
      if (found) {
        this._activeSkills.add(found.id); this.renderSkillsBar();
        this.setSkill(found.id);
        if (this.convId) await this.api('/api/convs/'+this.convId+'/skill',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({skill:found.id})});
      }
      q = m[2] || ''; inp.value = q; if (!q.trim()) return;
    }
    // Determine primary skill: first non-cn active skill, or default
    var primary = null;
    var extra = [];
    var self = this;
    this._activeSkills.forEach(function(sid) {
      if (sid !== 'cn' && !primary) primary = sid;
      else if (sid !== primary) extra.push(sid);
    });
    var skillForReq = primary || this.skill || 'bottleneck-hunter';
    var extraSkills = extra.length ? extra : null;
    var wasNew = !this.convId;
    if (!this.convId) await this.newConv(true);
    this.msgs.push({role:'user',content:q}); this.msgs.push({role:'assistant',content:'',_chunks:[],_refs:[]});
    inp.value = ''; inp.style.height = 'auto'; this.renderAll();
    // Reset smart scroll for new message
    this._userScrolledUp = false;
    this._scrollToBottom(true);

    if (wasNew) {
      var items = document.querySelectorAll('#convList .conv-item');
      if (items.length) {
        var first = items[0];
        // Find the text node (skip checkbox/span elements)
        for (var ci = 0; ci < first.childNodes.length; ci++) {
          if (first.childNodes[ci].nodeType === 3) { first.childNodes[ci].textContent = q.substring(0, 30); break; }
        }
      }
    }

    var btn = document.getElementById('sendBtn');
    btn.disabled = false; btn.classList.add('stop-mode'); btn.onclick = App.stopStream; btn.title = '停止生成';
    // Activate loading bar
    var loadingBar = document.getElementById('loadingBar');
    if (loadingBar) loadingBar.classList.add('active');
    var msgIdx = this.msgs.length-1, full = '', chunks = [], self = this;
    var sid = this.convId;

    var stream = {msgs: this.msgs, msgIdx: msgIdx, chunks: chunks, full: '', skill: skillForReq, active: true};
    Streams[sid] = stream;
    // Initialize typewriter display length and start animation loop
    this.msgs[msgIdx]._displayLen = 0;
    this._startTypewriter();

    try {
      var resp = await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({question:q,skill:skillForReq,conv_id:sid,model:self.model,extra_skills:extraSkills})});
      if (resp.status===401){window.location.href='/login'; delete Streams[sid]; self._streamDone(sid, btn); return;}
      if (!resp.ok){
        var errText = '服务异常 (' + resp.status + ')';
        try { var eb = await resp.json(); errText = eb.error || errText; } catch(e) {}
        self.msgs[msgIdx].content = errText; self.renderAll(); delete Streams[sid]; self._streamDone(sid, btn); return;
      }
      var reader = resp.body.getReader(), dec = new TextDecoder(), buf = '';
      self._activeReader = reader;
      while (true) {
        var ck = await reader.read(); if (ck.done) break;
        buf += dec.decode(ck.value,{stream:true});
        var lines = buf.split('\n'); buf = lines.pop()||'';
        for (var i = 0; i < lines.length; i++) {
          var l = lines[i]; if (!l.startsWith('data: ')) continue;
          var d = l.slice(6); if (d==='[DONE]') continue;
          try { var p = JSON.parse(d);
            if (p.error) {
              self.msgs[msgIdx].content = '⚠ ' + p.error;
              self.renderAll(); delete Streams[sid]; self._streamDone(sid, btn); return;
            }
            if (p.pulse) { /* loading pulse */ }
            if (p.tool) {
              if (p.tool==='AskUserQuestion'&&p.ask) chunks.push({t:'ask',c:p.tool,q:p.ask,opts:p.opts||[]});
              else chunks.push({t:'tool',c:p.tool,n:p.n||'',d:p.d||''});
            }
            if (p.think) chunks.push({t:'think',c:p.think});
            if (p.text) {
              var pt = fixPunct(p.text);
              full += pt;
              var last = chunks[chunks.length-1];
              if (last&&last.t==='text') last.c+=pt; else chunks.push({t:'text',c:pt});
            }
            if (p.ref) {
              // Reference citation from WebSearch/WebFetch tool results
              chunks.push({t:'ref',url:p.ref.url,title:p.ref.title||p.ref.url,snippet:p.ref.snippet||''});
              if (!self.msgs[msgIdx]._refs) self.msgs[msgIdx]._refs = [];
              self.msgs[msgIdx]._refs.push(p.ref);
            }
          } catch(e) { /* skip malformed JSON */ }
        }
        stream.msgs[msgIdx].content = full; stream.msgs[msgIdx]._chunks = chunks;
        stream.full = full; stream.chunks = chunks;
        if (App.convId === sid) {
          var curMsgIdx = App.msgs.length - 1;
          if (curMsgIdx >= 0 && App.msgs[curMsgIdx].role === 'assistant') App.renderOne(curMsgIdx);
        }
      }
    } catch(e) {
      var errorMsg = '网络异常';
      if (e.name === 'AbortError') {
        errorMsg = '已停止生成';
      } else if (e.name === 'TypeError' && e.message.indexOf('fetch') >= 0) {
        errorMsg = '⚠ 网络连接失败，请检查网络后重试';
      } else if (e.message && e.message.indexOf('timeout') >= 0) {
        errorMsg = '⚠ 响应超时，请重试';
      }
      stream.msgs[msgIdx].content = errorMsg;
      if (App.convId === sid) App.renderAll();
    }
    delete Streams[sid];
    self._streamDone(sid, btn);
    if (App.convId === sid) {
      App._buildTOC();
      App.loadConvs();
    }
  },

  _streamDone: function(sid, btn) {
    btn.disabled = false; btn.classList.remove('stop-mode'); btn.onclick = App.send; btn.title = '发送 (Enter)';
    var loadingBar = document.getElementById('loadingBar');
    if (loadingBar) loadingBar.classList.remove('active');
    this._activeReader = null;
    this._stopTypewriter();
    // Final full render for the completed message
    if (App.convId === sid) {
      var curMsgIdx = App.msgs.length - 1;
      if (curMsgIdx >= 0 && App.msgs[curMsgIdx].role === 'assistant') App.renderOne(curMsgIdx);
    }
  },

  /* ═══ Stop Generation ═══ */
  stopStream: async function() {
    if (this._activeReader) {
      try { this._activeReader.cancel(); } catch(e) {}
      this._activeReader = null;
    }
    if (this.convId) {
      await this.api('/api/chat/cancel', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({conv_id:this.convId})});
    }
    delete Streams[this.convId];
    this._stopTypewriter();
    var btn = document.getElementById('sendBtn');
    btn.disabled = false; btn.classList.remove('stop-mode'); btn.onclick = App.send; btn.title = '发送 (Enter)';
    var loadingBar = document.getElementById('loadingBar');
    if (loadingBar) loadingBar.classList.remove('active');
    this.loadConvs();
  },

  /* ═══ Logout ═══ */
  logout: function() { document.cookie = 'token=;path=/;max-age=0'; window.location.href = '/login'; },

  /* ═══ Scroll TOC ═══ */
  _buildTOC: function() {
    var track = document.getElementById('scrollTrack');
    var list = document.getElementById('msgList');
    if (!track || !list) return;
    var turns = list.querySelectorAll('.msg.user');
    track.innerHTML = '';
    if (!turns.length) return;
    var toc = document.createElement('div');
    toc.className = 'scroll-toc';
    turns.forEach(function(turn, i) {
      var mark = document.createElement('div');
      mark.className = 'scroll-mark';
      mark.onclick = function() { turn.scrollIntoView({behavior:'smooth',block:'start'}); };
      track.appendChild(mark);
      var item = document.createElement('div');
      item.className = 'scroll-toc-item';
      item.style.transitionDelay = (i * .05) + 's';
      var num = String(i + 1).padStart(2, '0');
      item.innerHTML = '<span class="scroll-toc-idx">' + num + '</span><span class="scroll-toc-text">' + escHtml(turn.textContent.trim().substring(0, 32)) + '</span>';
      item.onclick = function() { turn.scrollIntoView({behavior:'smooth',block:'start'}); };
      item.setAttribute('data-idx', i);
      toc.appendChild(item);
    });
    track.appendChild(toc);
    this._updateTOC();
  },
  _updateTOC: function() {
    var track = document.getElementById('scrollTrack');
    var list = document.getElementById('msgList');
    if (!track || !list) return;
    var turns = list.querySelectorAll('.msg.user');
    if (!turns.length) return;
    var st = list.scrollTop, vh = list.clientHeight, mid = st + vh / 2;
    var active = -1, minD = Infinity;
    turns.forEach(function(t, i) {
      var d = Math.abs(t.offsetTop + t.offsetHeight / 2 - mid);
      if (d < minD) { minD = d; active = i; }
    });
    track.querySelectorAll('.scroll-mark').forEach(function(d, i) { d.classList.toggle('active', i === active); });
    track.querySelectorAll('.scroll-toc-item').forEach(function(d, i) { d.classList.toggle('active', i === active); });
  }
};

/* ═══ Utilities ═══ */
function escHtml(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }
function render(t) { try { return marked.parse(t); } catch(e) { return t; } }

/* Normalize English punctuation to Chinese in Chinese text context */
function fixPunct(t) {
  if (!t) return t;
  // English comma/period before or between Chinese characters → Chinese comma/period
  t = t.replace(/([一-鿿㐀-䶿]),(\s*)([一-鿿㐀-䶿])/g, '$1，$3');
  t = t.replace(/([一-鿿㐀-䶿])\.(\s*)([一-鿿㐀-䶿])/g, '$1。$3');
  // Semicolons and colons between Chinese
  t = t.replace(/([一-鿿㐀-䶿]);(\s*)([一-鿿㐀-䶿])/g, '$1；$3');
  t = t.replace(/([一-鿿㐀-䶿]):(\s*)([一-鿿㐀-䶿])/g, '$1：$3');
  // End of Chinese sentence: English period → Chinese period
  t = t.replace(/([一-鿿㐀-䶿])\.(\s|$)/g, '$1。$2');
  t = t.replace(/([一-鿿㐀-䶿]),(\s|$)/g, '$1，$2');
  return t;
}

/* ═══ Event Bindings ═══ */
document.addEventListener('DOMContentLoaded', function() { App.init(); });
document.getElementById('msgList').addEventListener('scroll', function() { App._updateTOC(); });
window.addEventListener('resize', function() { App._buildTOC(); });
document.addEventListener('click', function(e) { if (!e.target.closest('#acDropdown')&&!e.target.closest('#input')) App.hideAC(); });
document.getElementById('input').addEventListener('keydown', function(e) {
  var key = e.key||e.code||'';
  if (key==='Enter'&&!e.shiftKey) { e.preventDefault(); if (App.selectAC()) return; App.send(); return; }
  if (key==='Tab'||e.keyCode===9) { e.preventDefault(); var dd=document.getElementById('acDropdown'); if (dd.style.display!=='none') { if (App.acIdx<0) { App.moveAC(1); } else { App.selectAC(); } return; } App.showAC(); return; }
  if (key==='ArrowDown'||e.keyCode===40) { e.preventDefault(); App.moveAC(1); return; }
  if (key==='ArrowUp'||e.keyCode===38) { e.preventDefault(); App.moveAC(-1); return; }
  if (key==='Escape'||e.keyCode===27) { App.hideAC(); return; }
});
document.getElementById('input').addEventListener('input', function() { App.showAC(); this.style.height='auto'; this.style.height=Math.min(140,Math.max(24,this.scrollHeight))+'px'; });
