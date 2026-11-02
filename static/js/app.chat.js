/* ═══════════════════════════════════════
   Prism — Chat and Report Reader (v0.19.7)
   ═══════════════════════════════════════ */

Object.assign(App, {
  loadSkills: async function() {
    try {
      var r = await this.api('/api/agent/workflows');
      if (!r.ok) throw new Error('workflows unavailable');
      var data = await r.json();
      this.skills = (Array.isArray(data) ? data : []).filter(function(item) {
        return ['prism-research', 'bottleneck-hunter', 'serenity', 'futures-research'].indexOf(item.id) >= 0;
      });
      this._workflowsUnavailable = !this.skills.length;
      this.setSkill(this.skill);
    } catch(error) {
      this.skills = []; this._workflowsUnavailable = true;
      if (this._toast) this._toast('研究方式暂时无法读取，请刷新后重试');
    }
    this.renderSkillsBar();
  },

  /* ═══ Skills (composer chips + drawer) ═══ */
  renderSkillsBar: function() {
    var box = document.getElementById('composerChips');
    if (!box) return;
    if (!this.skills.length) { box.innerHTML = ''; return; }
    var self = this;
    var selected = ['prism-research','bottleneck-hunter','serenity','futures-research'].indexOf(self.skill) >= 0 ? self.skill : 'prism-research';
    box.innerHTML = this.skills.filter(function(s){ return s.id === selected; })
      .map(function(s){ return '<button type="button" class="cmp-chip" onclick="App._toggleSkillDrawer()" aria-label="切换研究方式">'+esc(s.name)+'</button>'; }).join('');
  },
  _toggleSkillDrawer: function() {
    var d = document.getElementById('skillDrawer');
    if (!d) {
      d = document.createElement('div');
      d.id = 'skillDrawer';
      d.className = 'skill-drawer';
      // 锚定到 composer（.composer{position:relative}），抽屉就近贴在 🛠 按钮上方
      var host = document.getElementById('composer') || document.body;
      host.appendChild(d);
    }
    if (d.style.display === 'block') { d.style.display = 'none'; return; }
    this._renderSkillDrawerItems(d);
    d.style.display = 'block';
  },
  _renderSkillDrawerItems: function(d) {
    var self = this;
    var selected = ['prism-research','bottleneck-hunter','serenity','futures-research'].indexOf(self.skill) >= 0 ? self.skill : 'prism-research';
    d.innerHTML = '<div class="skill-drawer-hd">研究方式 · 单选</div>' +
      this.skills.map(function(s){
        var on = selected === s.id;
        return '<button type="button" class="skill-drawer-item' + (on ? ' on' : '') + '" aria-pressed="'+on+'" onclick="App._toggleSkill(\'' + escAttr(s.id) + '\')">' + esc(s.name) + '</button>';
      }).join('');
  },
  _toggleSkill: function(id) {
    if (!this.skills.some(function(item) { return item.id === id; })) return;
    this.setSkill(id);
    this.renderSkillsBar();
    var d = document.getElementById('skillDrawer');
    if (d) d.style.display = 'none';
  },

  selectSkillChip: function(skillId) {
    this._toggleSkill(skillId);
  },

  clearSkill: function() {
    this.setSkill('prism-research');
    this.renderSkillsBar();
  },

  updateSkillTagInInput: function() {
    // No longer used — skills shown in bar only
  },

  showWelcome: function() {
    document.getElementById('msgList').innerHTML = '<div class="welcome"><h2>Prism</h2><p>聚焦 AI 数据中心 · 半导体 · 新能源 · 机器人</p><div class="qa-row"><span class="qa-chip" onclick="App.quickAsk(\'AI数据中心液冷供应链上游瓶颈\')">液冷供应链</span><span class="qa-chip" onclick="App.quickAsk(\'光模块有哪些被低估的上游标的？\')">光模块上游</span><span class="qa-chip" onclick="App.quickAsk(\'HBM4钼替代钨，上游有什么被低估的标的？\')">HBM4·钼替钨</span><span class="qa-chip" onclick="App.quickAsk(\'800VDC车载电源器件卡脖子环节\')">800VDC电源</span><span class="qa-chip" onclick="App.quickAsk(\'人形机器人减速器投资机会\')">机器人减速器</span><span class="qa-chip" onclick="App.quickAsk(\'铜连接产业链上游瓶颈\')">铜连接</span></div></div>';
  },

  /* ═══ Models ═══ */
  loadModels: async function() {
    var s = document.getElementById('modelSelect');
    try {
      var r = await this.api('/api/models'); if (!r.ok) throw new Error('unavailable');
      var data = await r.json(), models = Array.isArray(data) ? data : data.models || [];
      this._models = models; this._modelsLoaded = true;
      var available = models.filter(function(m){return m.configured === true;});
      var preferred = available.find(function(m){return m.default === true || m.id === data.default;}) || available[0];
      var saved = ''; try { saved = localStorage.getItem('prism-model:'+this.user) || ''; } catch(error) {}
      var selected = available.find(function(m){return m.id === saved;}) || preferred;
      this.model = selected ? selected.id : '';
      s.innerHTML = models.length ? models.map(function(m){return '<option value="'+escAttr(m.id)+'"'+(m.id===App.model?' selected':'')+(m.configured===true?'':' disabled')+'>'+esc(m.name || m.id)+(m.configured===true?'':' · 未配置')+'</option>';}).join('') : '<option value="">尚未配置模型</option>';
      s.value = this.model; s.disabled = !selected;
      s.title = selected ? '配置已提供；真实调用是否可用以执行结果为准' : '没有已配置的研究模型，请由管理员完成配置';
      var notice = document.getElementById('modelConfigurationNotice');
      if (notice) { notice.hidden = !!selected; notice.textContent = selected ? '' : '尚未配置研究模型。可以继续查看历史、行情和报告；新研究暂不可用。'; }
    } catch(error) {
      s.innerHTML = '<option value="">模型配置读取失败</option>'; s.disabled = true;
      this.model = ''; this._modelsLoaded = true;
      var notice = document.getElementById('modelConfigurationNotice');
      if (notice) { notice.hidden = false; notice.textContent = '模型配置暂时无法读取，请刷新后重试。'; }
    }
  },
  setModel: function(m) {
    if (!(this._models || []).some(function(item){return item.id === m && item.configured === true;})) return;
    this.model = m; try { localStorage.setItem('prism-model:'+this.user,m); } catch(error) {}
  },

  /* ═══ Conversations ═══ */
  _refreshReportsButton: async function() {
    var btn = document.getElementById('reportsBtn');
    if (!btn) return;
    if (!this.convId) { btn.style.display = 'none'; btn.classList.remove('has-reports'); return; }
    var r = await this.api('/api/convs/' + this.convId + '/reports');
    if (!r.ok) { btn.style.display = 'none'; return; }
    var data = await r.json();
    var n = (data.reports || []).length;
    if (n > 0) {
      btn.style.display = 'flex';
      btn.classList.add('has-reports');
      btn.title = '本对话有 ' + n + ' 份报告，点击查看';
    } else {
      btn.style.display = 'none';
      btn.classList.remove('has-reports');
    }
  },

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
      var answerOption = e.target.closest('.ask-opt[data-ask-answer]');
      if (answerOption && !answerOption.disabled) {
        e.preventDefault();
        self.answerAsk(answerOption.dataset.askAnswer);
        return;
      }
      var termEl = e.target.closest('.gloss-term');
      if (termEl) {
        e.preventDefault();
        var term = termEl.getAttribute('data-term');
        self._handleGlossClick(term, termEl);
      } else if (!e.target.closest('#glossPanel')) {
        document.getElementById('glossPanel').style.display = 'none';
      }
      // Report links: open in inline viewer by default, new tab on shift/cmd/ctrl.
      var reportEl = e.target.closest('.report-link');
      if (reportEl) {
        e.preventDefault();
        var fname = reportEl.getAttribute('data-report') || '';
        var newTab = e.shiftKey || e.metaKey || e.ctrlKey;
        self.openReport(fname, { newTab: newTab });
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
      // Only advance the typewriter for the currently-active conversation.
      // Background streams should keep their _displayLen untouched so that
      // when the user switches back, the typewriter picks up where it left off.
      var sid = App.convId;
      var s = sid ? Streams[sid] : null;
      if (s && s.active) {
        anyActive = true;
        var m = s.msgs[s.msgIdx];
        if (m && m.role === 'assistant' && !s.restored) {
          if (m._displayLen === undefined) m._displayLen = 0;
          // Calculate total text length from chunks
          var total = 0;
          (m._chunks || []).forEach(function(c) { if (c.t === 'text') total += c.c.length; });
          if (m._displayLen < total) {
            // Reveal 2-5 chars per frame for natural reading pace
            m._displayLen = Math.min(total, m._displayLen + 2 + Math.floor(Math.random() * 4));
            var curMsgIdx = App.msgs.length - 1;
            if (curMsgIdx >= 0 && App.msgs[curMsgIdx].role === 'assistant') App.renderOne(curMsgIdx);
          }
        }
      }
      // Also keep the timer alive if any stream is still active elsewhere,
      // so the loop can pick up when the user switches back.
      for (var otherSid in Streams) {
        if (Streams[otherSid].active && otherSid !== App.convId) { anyActive = true; break; }
      }
      if (anyActive) {
        self._twTimer = requestAnimationFrame(tick);
      } else {
        self._twTimer = null;
      }
    }
    this._twTimer = requestAnimationFrame(tick);
  },

  _stopTypewriter: function(sid) {
    if (sid && sid !== this.convId && Streams[this.convId] && Streams[this.convId].active) return;
    if (this._twTimer) {
      cancelAnimationFrame(this._twTimer);
      this._twTimer = null;
    }
    // Clear _displayLen only on the named stream (or the current conv if no sid).
    var targetSid = sid || this.convId;
    if (targetSid && Streams[targetSid]) {
      var m = Streams[targetSid].msgs[Streams[targetSid].msgIdx];
      if (m) m._displayLen = undefined;
    }
  },

  /* Re-render any in-flight stream when the user switches to its conv.
     The stream keeps its _displayLen so the typewriter effect resumes
     from where it was left off. */
  _reattachStream: function() {
    var s = Streams[this.convId];
    this._syncChatStreamControls();
    if (s && s.active) {
      var m = s.msgs[s.msgIdx];
      if (m && m.role === 'assistant' && m._chunks) {
        this.renderOne(this.msgs.length - 1);
      }
      if (s.offline && !s.following && s.runId) this._followNativeStream(s);
    }
    if (!this._twTimer && !(s && s.restored)) this._startTypewriter();
  },

  _syncChatStreamControls: function() {
    var stream = Streams[this.convId], running = !!(stream && stream.active);
    var button = document.getElementById('sendBtn');
    if (button) {
      button.disabled = !!(stream && stream.stopping); button.classList.toggle('stop-mode', running);
      button.onclick = running ? function() { App.stopStream(); } : function() { App.send(); };
      button.title = running ? (stream.stopping ? '正在确认停止…' : '停止生成') : '发送 (Enter)';
    }
    var bar = document.getElementById('loadingBar'); if (bar) bar.classList.toggle('active', running);
    this._activeReader = running ? stream.reader || null : null;
  },

  /* ═══ Quote tags ([[quote:TICKER]]) ═══
     v0.6 — the chat pipeline may emit [[quote:600519]] tokens inline.
     We rewrite them into a placeholder chip and then async-fill each
     chip with a live price fetched from /api/quote/{ticker}. */
  _QUOTE_TAG_RE: /\[\[quote:([A-Za-z0-9.\-]{1,16})\]\]/g,
  _fillQuoteChipsSeq: 0, // monotonic seq so only the latest fill wins per chip

  renderQuoteTags: function(html) {
    if (!html) return html;
    var self = this;
    return html.replace(this._QUOTE_TAG_RE, function(_, ticker) {
      // Defensive esc — ticker is already constrained by the regex.
      return '<span class="quote-chip" data-ticker="'+escAttr(ticker)+'">'+
             '<span class="quote-chip__code">'+esc(ticker)+'</span>'+
             '<span class="quote-chip__price">…</span>'+
             '<span class="quote-chip__pct"></span>'+
             '</span>';
    });
  },

  fillQuoteChips: function(root) {
    if (!root) return;
    var chips = root.querySelectorAll ? root.querySelectorAll('.quote-chip:not([data-filled])') : [];
    if (!chips.length) return;
    var self = this;
    var mySeq = ++this._fillQuoteChipsSeq;
    // Collect unique tickers and batch-fetch in one request
    var tickers = [];
    var chipMap = {}; // ticker → [chips]
    for (var i = 0; i < chips.length; i++) {
      var t = chips[i].dataset.ticker;
      chips[i].dataset.filled = '1';
      if (!chipMap[t]) { chipMap[t] = []; tickers.push(t); }
      chipMap[t].push(chips[i]);
    }
    self.api('/api/quotes?tickers=' + encodeURIComponent(tickers.join(','))).then(function(r) {
      if (mySeq !== self._fillQuoteChipsSeq) return;
      if (!r.ok) return;
      return r.json().then(function(data) {
        if (mySeq !== self._fillQuoteChipsSeq) return;
        var quotes = data.quotes || {};
        for (var t in quotes) {
          var q = quotes[t];
          if (q.error) { _markChipsErr(chipMap[t]); continue; }
          var price = (q.price == null) ? null : Number(q.price);
          var pct = (q.change_pct == null) ? null : Number(q.change_pct);
          var chipList = chipMap[t] || [];
          for (var ci = 0; ci < chipList.length; ci++) {
            var chip = chipList[ci];
            var priceEl = chip.querySelector('.quote-chip__price');
            var pctEl = chip.querySelector('.quote-chip__pct');
            if (price != null && !isNaN(price)) {
              if (priceEl) priceEl.textContent = price.toFixed(price >= 100 ? 2 : 3);
            } else if (priceEl) {
              priceEl.textContent = '—';
            }
            if (pct != null && !isNaN(pct)) {
              if (pctEl) pctEl.textContent = (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
              chip.classList.add(pct >= 0 ? 'quote-chip--up' : 'quote-chip--down');
            }
            chip.classList.add(q.stale ? 'quote-chip--stale' : 'quote-chip--live');
          }
        }
      });
    }).catch(function() {});
    function _markChipsErr(list) { if (list) for (var ei = 0; ei < list.length; ei++) { list[ei].classList.add('quote-chip--err'); } }
  },

  /* ═══ Rendering ═══ */
  _renderMsg: function(chunks, fullText, streaming, msgObj) {
    chunks = Array.isArray(chunks) ? chunks : [];
    var refs = ChatRender.collectRefs((msgObj && msgObj._refs) || [], chunks);
    var research = msgObj && (msgObj.research || msgObj._research);
    var calls = {}, callOrder = [];
    chunks.forEach(function(chunk) {
      if (!chunk) return;
      if (chunk.t === 'research' && chunk.research) research = chunk.research;
      if (chunk.t === 'platform_call' && chunk.call && chunk.call.call_id) {
        if (!calls[chunk.call.call_id]) callOrder.push(chunk.call.call_id);
        calls[chunk.call.call_id] = chunk.call;
      }
    });
    var reportFiles = streaming ? [] : this._reportFilesForMessage(msgObj, chunks, fullText);
    var html = ChatRender.renderResearch(research), pending = [];
    try {
      if (!chunks.some(function(c) { return c && (c.t === 'text' || !c.t); })) html += ChatRender.renderContent(reportFiles.length ? this._compactDeliveredReportText(fullText) : fullText);
      chunks.forEach(function(c, index) {
        if (!c || c.t === 'ref' || c.t === 'research' || c.t === 'platform_call') return;
        if (c.t === 'tool') { pending.push(c); return; }
        html += ChatRender.renderSteps(pending); pending = [];
        if (c.t === 'think' || c.t === 'ask') html += ChatRender.renderChunk(c, {open:streaming});
        else if (c.t === 'status') html += '<div class="research-status-notice" role="status">'+esc(ChatRender.stringify(c.c))+'</div>';
        else if (c.t === 'text' || !c.t) {
          var text = reportFiles.length ? this._compactDeliveredReportText(ChatRender.stringify(c.c)) : c.c;
          html += ChatRender.renderContent(text) + (streaming && index === chunks.length - 1 ? '<span class="stream-cursor"></span>' : '');
        }
      }, this);
      html += ChatRender.renderSteps(pending, streaming);
      if (streaming && !html) html = '<div class="shimmer-bar" style="width:60%"></div>';
      if (reportFiles.length) html += this._inlineReportCard(reportFiles);
      if (callOrder.length && this._renderPlatformCallHTML) {
        html += '<section class="platform-call-section"><h4>平台能力与记录</h4>' + callOrder.map(function(id) { return this._renderPlatformCallHTML(calls[id]); }, this).join('') + '</section>';
      }
      if (refs.length && !streaming) {
        html += '<section class="ref-section"><div class="ref-title">采集来源 ('+refs.length+')</div><p class="ref-boundary">工具采集成功不等于事实已核验；平台数据、历史研究与外部网页保留各自来源。</p>';
        refs.forEach(function(ref) { html += ChatRender.renderChunk(ref); });
        html += '</section>';
      }
      return html;
    } catch(e) { console.error('_renderMsg:',e); return ChatRender.renderContent(fullText); }
  },

  _sanitizeRenderedArtifacts: function(container) {
    if (!container || !container.querySelectorAll) return;
    var root = container;
    try {
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
      var nodes = [], node;
      while ((node = walker.nextNode())) {
        if (node.nodeValue && node.nodeValue.indexOf('[object Object]') >= 0) nodes.push(node);
      }
      nodes.forEach(function(n) {
        n.nodeValue = ChatRender.clean ? ChatRender.clean(n.nodeValue) : String(n.nodeValue).replace(/\[object Object\]/g, '');
      });
    } catch(e) {}
    root.querySelectorAll('.think-box').forEach(function(box) {
      var body = box.querySelector('.think-content');
      if (body && !body.textContent.trim()) box.remove();
    });
  },

  renderAll: function() {
    var el = document.getElementById('msgList');
    if (!this.msgs.length) { this.showWelcome(); return; }
    var self = this;
    // Snapshot which message is currently being streamed on this conv.
    // During streaming, m.content is plain text (not JSON) and m._chunks holds
    // the live structure. We must render from _chunks to preserve tool steps,
    // think blocks, and references.
    var stream = Streams[this.convId];
    var streamingLastIdx = (stream && stream.active) ? stream.msgIdx : -1;
    el.innerHTML = this.msgs.map(function(m, idx) {
      if (m.role === 'user') return '<div class="msg user"><div class="msg-inner"><div class="bubble">'+esc(m.content)+'</div></div></div>';
      try {
        var text = m.content || '', chunks = null;
        // Prefer the live _chunks array for the currently-streaming message;
        // for any other message fall back to parsing the stored JSON content.
        if (idx === streamingLastIdx && m._chunks && m._chunks.length) {
          chunks = m._chunks;
          text = m.content || '';
        } else if (typeof text === 'string' && text.startsWith('{"text":')) {
          try { var p = JSON.parse(text); text = p.text || ''; chunks = p.chunks || []; m._chunks = chunks; m._reportFiles = p.report_files || m._reportFiles || []; m._reportStatus = p.report_status || m._reportStatus || ''; m.research = p.research || m.research; } catch(e) { chunks = []; }
        } else {
          chunks = [];
        }
        // Pass refs through for final render
        if (!m._refs && chunks) {
          m._refs = [];
          for (var ci = 0; ci < chunks.length; ci++) {
            if (chunks[ci].t === 'ref') m._refs.push(chunks[ci]);
          }
        }
        // Attach refs to the plain msg object so _renderMsg can see them
        var bubble = self._renderMsg(chunks, text, idx === streamingLastIdx, m);
        bubble = self.renderQuoteTags(bubble);
        bubble = self._glossifyTerms(self._linkifyReports(bubble));
        var htmlOut = '<div class="msg assistant"><div class="msg-inner"><div class="msg-card">' +
          '<div class="msg-card__hd"><span class="msg-card__who">PRISM</span>' +
          '<span class="msg-card__acts"><span onclick="App._copyMsg(this)">复制</span><span onclick="App._regenMsg(this)">重生成</span><span onclick="App._exportMsg(this)">导出报告</span></span>' +
          '</div><div class="bubble">'+bubble+'</div></div></div></div>';
        // Fill quote chips async after attaching to DOM — schedule for after this innerHTML assignment.
        return htmlOut;
      } catch(e) {
        return '<div class="msg assistant"><div class="msg-inner"><div class="msg-card"><div class="msg-card__hd"><span class="msg-card__who">PRISM</span><span class="msg-card__acts"></span></div><div class="bubble">'+ChatRender.renderContent(m.content)+'</div></div></div></div>';
      }
    }).join('');
    this._sanitizeRenderedArtifacts(el);
    this._addCopyButtons(el);
    el.scrollTop = el.scrollHeight;
    this._buildTOC();
    // v0.6 — fill any [[quote:TICKER]] chips with live prices
    this.fillQuoteChips(el);
  },

  renderOne: function(idx) {
    var el = document.getElementById('msgList'), bubbles = el.querySelectorAll('.bubble');
    if (!bubbles[idx]) return;
    var m = this.msgs[idx];
    if (!m || m.role !== 'assistant') return;
    var chunks = m._chunks || [];
    var streaming = !!(Streams[this.convId] && Streams[this.convId].active);

    // Ensure _refs is populated from _chunks when missing — renderOne can be
    // called on completion before renderAll has had a chance to build _refs.
    if (!m._refs && chunks.length) {
      m._refs = [];
      for (var rci = 0; rci < chunks.length; rci++) {
        if (chunks[rci].t === 'ref') m._refs.push(chunks[rci]);
      }
    }

    if (chunks.length === 0 && !m.content) {
      bubbles[idx].innerHTML = '<div class="typing"><span></span><span></span><span></span></div>';
      this._scrollToBottom(false); return;
    }
    if (chunks.length === 0 && m.content) {
      bubbles[idx].innerHTML = this.renderQuoteTags(this._glossifyTerms(this._linkifyReports(this._renderMsg([], m.content, false, m))));
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
          var textValue = ChatRender.stringify(c.c);
          var portion = remaining > 0 ? textValue.substring(0, remaining) : '';
          // Keep a text placeholder so metadata-only rendering cannot expose
          // the full response before the typewriter reaches it.
          displayChunks.push({t: 'text', c: portion});
          displayContent += portion;
          charCount += textValue.length;
        } else {
          // Capability/status events must remain visible while prose streams.
          displayChunks.push(c);
        }
      }
    }

    // O(1) chunk signature — avoid expensive JSON.stringify on large arrays.
    // Captures: chunk count, last chunk type, and last text-chunk length.
    var chunkSig = chunks.length;
    if (chunks.length > 0) {
      var lc = chunks[chunks.length - 1];
      chunkSig += ':' + lc.t + ':' + (lc.c ? lc.c.length : 0);
    }
    if (!streaming || displayLen === undefined) {
      if (m._lastSig === chunkSig) return;
    }
    m._lastSig = chunkSig;

    // Save open state of fold elements to prevent flicker during re-render
    var openFolds = [];
    if (bubbles[idx]) {
      bubbles[idx].querySelectorAll('.step-fold[open]').forEach(function(f, i) { openFolds.push(i); });
    }
    bubbles[idx].innerHTML = this.renderQuoteTags(this._glossifyTerms(this._linkifyReports(this._renderMsg(displayChunks, displayContent || m.content, streaming, m))));
    this._sanitizeRenderedArtifacts(bubbles[idx]);
    // Restore open state
    if (openFolds.length) {
      var newFolds = bubbles[idx].querySelectorAll('.step-fold');
      openFolds.forEach(function(i) { if (newFolds[i]) newFolds[i].setAttribute('open', ''); });
    }
    this._addCopyButtons(bubbles[idx]);
    // v0.6 — fill any [[quote:TICKER]] chips with live prices
    this.fillQuoteChips(bubbles[idx]);
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

  // ── Message card actions (v0.8) ──
  _copyMsg: function(el) {
    var card = el.closest('.msg-card');
    var bubble = card && card.querySelector('.bubble');
    if (!bubble) return;
    var txt = bubble.innerText;
    navigator.clipboard.writeText(txt).then(function(){
      var o = el.textContent; el.textContent = '已复制'; setTimeout(function(){ el.textContent = o; }, 1200);
    }).catch(function(){});
  },
  _regenMsg: function(el) {
    var msg = el.closest('.msg');
    var prev = msg && msg.previousElementSibling;
    var ub = prev && prev.querySelector('.bubble');
    if (ub) { document.getElementById('input').value = ub.innerText; this.send(); }
  },
  _exportMsg: function() {
    if (typeof this.openReports === 'function') this.openReports();
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
    // Step 1 (==term==) is now handled by _preGlossify() in render(),
    // BEFORE marked.parse — so it runs on raw text and doesn't corrupt
    // already-rendered HTML tags like <strong>.
    //
    // Step 2: scan for glossary terms only in text between > and < bounds
    // (visible text, not inside HTML attributes).  The regex intentionally
    // skips <script>, <style>, and other raw-text containers — marked.parse
    // never emits those in normal prose output.
    html = html.replace(/>([^<]{1,2000})</g, function(match, text) {
      // Guard: skip text that looks like partial HTML to avoid corrupting
      // attribute boundaries or nested tag structures.
      if (/<[a-z/]|^\s*(?:href|src|class|id|style|alt|title|data-)\s*=/.test(text)) return match;
      var terms = Object.keys(self._glossary).sort(function(a,b){return b.length-a.length;});
      for (var ti = 0; ti < terms.length; ti++) {
        var t = terms[ti];
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
    // Final safety pass: remove any orphaned attribute-fragment patterns
    // that might have leaked through (e.g. stray `">` from truncated markup).
    return sanitizeHtml(html);
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
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRF-Token': self._getCookie('csrf_token'),
        },
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
    var self = this;
    return html.replace(/(?<!["'=>])(reports\/[^\s<>"')]+?\.(?:html|htm|pdf|csv|md))/gi, function(match) {
      var fname = match.replace(/^reports\//, '');
      var url = '/api/reports/' + App.convId + '/' + encodeURIComponent(fname);
      return '<a href="' + escAttr(url) + '" class="report-link" title="点击内嵌查看报告 (Shift+点击 在新标签页打开)" data-report="' + escAttr(fname) + '">' + match + '</a>';
    });
  },

  _reportFilesForMessage: function(msgObj, chunks, fullText) {
    var files = [], seen = {};
    var add = function(value) {
      var name = value && typeof value === 'object' ? value.filename : value;
      name = String(name || '').replace(/^reports\//i, '');
      try { name = decodeURIComponent(name); } catch(e) {}
      if (!name || name.indexOf('..') >= 0 || /[\\/]/.test(name) || !/\.(?:html?|pdf|md)$/i.test(name) || seen[name]) return;
      seen[name] = true; files.push(name);
    };
    ((msgObj && msgObj._reportFiles) || []).forEach(add);
    var sources = [String(fullText || '')];
    (chunks || []).forEach(function(chunk) {
      if (chunk && chunk.c) sources.push(ChatRender.stringify(chunk.c));
      if (chunk && chunk.d) sources.push(ChatRender.stringify(chunk.d));
    });
    var re = /reports\/([^\s<>"')]+?\.(?:html?|pdf|md))/gi;
    sources.forEach(function(source) {
      var match;
      while ((match = re.exec(source))) add(match[1]);
      re.lastIndex = 0;
    });
    var rank = function(name) { return /\.html?$/i.test(name) ? 0 : (/\.pdf$/i.test(name) ? 1 : 2); };
    return files.sort(function(a, b) { return rank(a) - rank(b); });
  },

  _compactDeliveredReportText: function(text) {
    text = ChatRender.clean(text || '');
    var marker = /^#{1,4}\s*报告已交付(?:\s|$).*$/im.exec(text);
    if (!marker) return text;
    var delivered = text.slice(marker.index).trim();
    var headings = [], headingRe = /^#{1,4}\s+.+$/gm, match;
    while ((match = headingRe.exec(delivered))) headings.push({index:match.index, text:match[0]});
    var cutoff = headings.length >= 3 ? headings[2].index : -1;
    if (cutoff < 0 && delivered.length > 1800) {
      cutoff = delivered.lastIndexOf('\n\n', 1800);
      if (cutoff < 400) cutoff = 1800;
    }
    if (cutoff > 0) delivered = delivered.slice(0, cutoff).trim() + '\n\n> 完整内容已在下方报告中展开。';
    return delivered;
  },

  _inlineReportCard: function(files) {
    if (!this.convId || !files || !files.length) return '';
    var fname = files[0];
    var url = '/api/reports/' + this.convId + '/' + encodeURIComponent(fname);
    var title = fname.replace(/\.(?:html?|pdf|md)$/i, '').replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
    var kind = /\.pdf$/i.test(fname) ? 'PDF 报告' : (/\.md$/i.test(fname) ? 'Markdown 报告' : '完整报告');
    return '<section class="chat-inline-report" data-report-file="' + escAttr(fname) + '">' +
      '<header><div class="chat-inline-report-copy"><span>' + kind + '</span><b title="' + escAttr(fname) + '">' + esc(title || '研究报告') + '</b></div>' +
      '<div class="chat-inline-report-actions"><button type="button" class="chat-inline-report-toggle" aria-expanded="true" onclick="App._toggleInlineReport(this)">收起</button>' +
      '<button type="button" onclick="App.openReport(\'' + escAttr(fname) + '\',{newTab:true})">独立阅读</button></div></header>' +
      '<div class="chat-inline-report-body"><iframe src="' + escAttr(url) + '" loading="lazy" sandbox="allow-scripts" referrerpolicy="no-referrer" title="' + escAttr(title || '研究报告') + '"></iframe></div>' +
    '</section>';
  },

  _toggleInlineReport: function(button) {
    var card = button && button.closest('.chat-inline-report');
    if (!card) return;
    var collapsed = card.classList.toggle('collapsed');
    button.textContent = collapsed ? '展开' : '收起';
    button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  },

  /* ═══ Inline report viewer ═══ */
  _currentReportUrl: '',

  openReport: function(fname, opts) {
    if (!this.convId) return;
    opts = opts || {};
    var url = '/api/reports/' + this.convId + '/' + encodeURIComponent(fname);
    if (opts.newTab) { window.open(url, '_blank'); return; }
    var frame = document.getElementById('reportFrame');
    var title = document.getElementById('reportViewerTitle');
    this._currentReportUrl = url;
    frame.src = url;
    title.textContent = fname;
    document.getElementById('reportViewer').style.display = 'flex';
  },

  openReportInNewTab: function() {
    if (this._currentReportUrl) window.open(this._currentReportUrl, '_blank');
  },

  closeReportViewer: function() {
    var viewer = document.getElementById('reportViewer');
    viewer.style.display = 'none';
    // Clear the iframe so a stale document doesn't keep running.
    document.getElementById('reportFrame').src = 'about:blank';
    this._currentReportUrl = '';
  },

  /* List all reports for the current conversation. */
  openReports: async function() {
    if (!this.convId) return;
    var r = await this.api('/api/convs/' + this.convId + '/reports');
    if (!r.ok) return;
    var data = await r.json();
    var body = document.getElementById('reportsList');
    if (!data.reports || !data.reports.length) {
      body.innerHTML = '<div class="reports-empty">该对话还没有生成报告</div>';
    } else {
      body.innerHTML = '<div class="reports-list">' + data.reports.map(function(rep) {
        var sz = rep.kind === 'saved' ? 'saved'
                : rep.size < 1024 ? (rep.size + 'B')
                : rep.size < 1024 * 1024 ? (Math.round(rep.size/1024) + 'KB')
                : (Math.round(rep.size/1024/1024 * 10) / 10) + 'MB';
        var d = new Date(rep.mtime * 1000);
        var ds = d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+
                 String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
        var title = rep.title || rep.filename || 'Saved report';
        var onclick = rep.report_id
          ? "App.openSavedReportFromChat('" + escAttr(rep.report_id) + "')"
          : "App.openReport('" + escAttr(rep.filename) + "')";
        return '<div class="reports-item" onclick="' + onclick + '">' +
          '<div class="reports-kind">' + esc(rep.kind.toUpperCase().slice(0, 4)) + '</div>' +
          '<div class="reports-meta">' +
            '<div class="reports-name">' + esc(title) + '</div>' +
            '<div class="reports-sub">' + ds + ' · ' + sz + '</div>' +
          '</div>' +
          '<span class="reports-open">查看</span>' +
        '</div>';
      }).join('') + '</div>';
    }
    document.getElementById('reportsModal').style.display = 'flex';
  },

  openSavedReportFromChat: function(id) {
    this.closeReports();
    this.switchMode('reports');
    this._activeSavedReportId = id;
    if (this._openSavedReport) this._openSavedReport(id);
  },

  closeReports: function() {
    document.getElementById('reportsModal').style.display = 'none';
  },

  /* ═══ Send & Stream ═══ */
  quickAsk: function(q) { document.getElementById('input').value = q; this.send(); },
  answerAsk: async function(answer) {
    // Native research continues as an explicit new user message.
    var cards = document.querySelectorAll('.ask-card');
    for (var ci = 0; ci < cards.length; ci++) {
      cards[ci].classList.add('ask-answered');
      var btns = cards[ci].querySelectorAll('.ask-opt');
      for (var bi = 0; bi < btns.length; bi++) btns[bi].disabled = true;
    }
    var input = document.getElementById('input');
    input.value = answer; input.focus();
    if (this._toast) this._toast('补充内容已填入输入框，请发送以继续研究');
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
      return s.id.toLowerCase().indexOf(query)>=0 || s.name.indexOf(query)>=0 || String(s.shortcut || '').indexOf(query)>=0;
    });
    if (!items.length) { dd.style.display = 'none'; return; }
    this.acIdx = -1; this.acItems = items;
    this._renderAC(dd, inp);
  },
  _renderAC: function(dd, inp) {
    var self = this;
    dd.innerHTML = '<div class="ac-header"><span>研究方式</span><span class="ac-header-hint">⇥ select · ↓↑ nav</span></div>' +
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
  selectSkill: function(id) { var inp = document.getElementById('input'), val = inp.value, pos = inp.selectionStart, before = val.substring(0,pos), after = val.substring(pos); before = before.replace(/\/\S*$/, ''); inp.value = before.trim()?before.trim()+' '+after:after; this.setSkill(id); this.renderSkillsBar(); this.hideAC(); inp.focus(); },

  _restoreNativeStream: function(sid) {
    if (this.convId !== sid || (Streams[sid] && Streams[sid].active)) return false;
    var messages = this.msgs, index = -1, payload, native;
    for (var i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role !== 'assistant') continue;
      try { payload = JSON.parse(messages[i].content); } catch(error) { continue; }
      native = payload && payload.native_agent;
      if (native && native.engine === 'prism-native' && ['queued','running','awaiting_user'].indexOf(native.status) >= 0 && typeof native.run_id === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(native.run_id)) {
        index = i; break;
      }
    }
    if (index < 0) return false;
    // A history snapshot has no event cursor. Rebuild this one message from
    // sequence zero, rather than appending a replay onto its existing text/cards.
    var message = messages[index];
    var snapshot = {text:payload.text || '',chunks:payload.chunks || [],refs:payload.refs || []};
    message._reportStatus = '';
    message.content = ''; message._chunks = []; message._refs = [];
    message._displayLen = undefined; message._lastSig = null;
    message.research = payload.research; message._reportFiles = [];
    var stream = {msgs:messages,msgIdx:index,convId:sid,runId:native.run_id,lastSeq:0,
      full:'',chunks:[],skill:this.skill,active:true,following:true,restored:true,snapshot:snapshot};
    Streams[sid] = stream;
    this._syncChatStreamControls();
    this._followNativeStream(stream, snapshot);
    return true;
  },

  _followNativeStream: function(stream, snapshot) {
    if (stream.promise && stream.following) return stream.promise;
    var self = this, sid = stream.convId, messages = stream.msgs, index = stream.msgIdx;
    var message = messages[index];
    snapshot = snapshot || stream.snapshot;
    stream.following = true; stream.offline = false;
    stream.promise = (async function() {
      try {
        stream.transportController = new AbortController();
        var response = await fetch('/api/agent/runs/'+encodeURIComponent(stream.runId)+'/stream?after_seq='+(stream.lastSeq || 0), {
          method:'GET',credentials:'same-origin',headers:{'Accept':'text/event-stream','X-Requested-With':'XMLHttpRequest'},
          signal:stream.transportController.signal
        });
        await self._readNativeStream(stream, response, function(frame) { self._applyNativeFrame(stream, frame); });
      } catch(error) {
        if (!stream.cancelled && !stream.terminal) {
          stream.offline = true;
          var displayChunks = stream.chunks.slice();
          if (snapshot && snapshot.text.length > stream.full.length && snapshot.text.indexOf(stream.full) === 0) {
            // The snapshot can be ahead of the last replayed event. Keep it as
            // a display fallback without advancing the durable replay buffer.
            displayChunks = snapshot.chunks.slice();
            message.content = snapshot.text; message._refs = snapshot.refs.slice();
          }
          displayChunks.push({t:'status',c:'连接暂时中断，已保留研究记录。仍可请求停止；本次不会重复提交研究。'});
          message._chunks = displayChunks;
        }
      } finally {
        stream.following = false;
        message._displayLen = undefined; message._lastSig = null;
        if ((stream.terminal || stream.doneReceived || stream.cancelled) && Streams[sid] === stream) {
          delete Streams[sid]; self._streamDone(sid, document.getElementById('sendBtn'));
        } else if (self.convId === sid) self._syncChatStreamControls();
        if (self.convId === sid && self.msgs === messages) self.renderOne(index);
        self._refreshReportsButton();
      }
    })();
    return stream.promise;
  },

  _applyNativeFrame: function(stream, p) {
    var self = this, msgIdx = stream.msgIdx, sid = stream.convId;
    var full = stream.full || '', chunks = stream.chunks || [];
            if (p.error) {
              var streamError = '⚠ ' + ChatRender.clean(p.error);
              full += (full ? '\n\n' : '') + streamError;
              chunks.push({t:'text', c:(full && chunks.length ? '\n\n' : '') + streamError});
              stream.error = streamError;
            }
            if (p.pulse) { /* loading pulse */ }
            if (p.status) chunks.push({t:'status', c:self._nativeStatusLabel(p.status)});
            else if (p.agent && stream.agentStatusChanged) chunks.push({t:'status', c:self._nativeStatusLabel(p.agent.status)});
            if (p.tool) {
              var toolName = ChatRender.clean(p.tool || '');
              if (toolName==='AskUserQuestion'&&p.ask) chunks.push({t:'ask',c:toolName,q:p.ask,opts:p.opts||[]});
              else { var td = ChatRender.clean(p.d || ''); chunks.push({t:'tool',c:toolName,n:ChatRender.stringify(p.n||''),d:td}); }
            }
            if (p.think) { var tc = ChatRender.clean(p.think || ''); if (tc) chunks.push({t:'think',c:tc}); }
            if (p.text) {
              var pt = fixPunct(ChatRender.stringify(p.text || ''));
              if (pt) {
                full += pt;
                var last = chunks[chunks.length-1];
                if (last&&last.t==='text') last.c+=pt; else chunks.push({t:'text',c:pt});
              }
            }
            if (p.ref) {
              // Reference citation from WebSearch/WebFetch tool results
              chunks.push(Object.assign({}, p.ref, {t:'ref'}));
              stream.msgs[msgIdx]._refs = ChatRender.collectRefs(stream.msgs[msgIdx]._refs || [], [{t:'ref', ...p.ref}]);
            }
            if (p.research && typeof p.research === 'object') {
              stream.msgs[msgIdx].research = p.research;
              chunks.push({t:'research', research:p.research});
            }
            if (p.platform_call && p.platform_call.call_id) {
              chunks.push({t:'platform_call', call:p.platform_call});
              stream.msgs[msgIdx]._lastSig = null;
              if (self._receivePlatformCall) self._receivePlatformCall(p.platform_call);
            }
            if (p.report_status) {
              stream.reportStatus = p.report_status;
              stream.reportFiles = p.report_files || [];
              stream.msgs[msgIdx]._reportStatus = p.report_status;
              stream.msgs[msgIdx]._reportFiles = p.report_files || [];
            }
        stream.msgs[msgIdx].content = String(full||''); stream.msgs[msgIdx]._chunks = chunks;
        stream.full = full; stream.chunks = chunks;
        if (self.convId === sid && self.msgs === stream.msgs) self.renderOne(msgIdx);
  },

  _newAgentRequestId: function() {
    if (globalThis.crypto && globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
    if (globalThis.crypto && globalThis.crypto.getRandomValues) {
      var bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
      return Array.from(bytes, function(value) { return value.toString(16).padStart(2, '0'); }).join('');
    }
    throw new Error('当前浏览器无法建立研究请求标识，请更新浏览器后重试');
  },

  _nativeStatusLabel: function(status) {
    return {queued:'Prism 正在准备研究',running:'Prism 正在研究',awaiting_user:'等待补充研究信息',completed:'研究已保存',done:'研究已保存',partial:'已保存已有结果，研究尚未完整完成',failed:'研究未完成，已有记录已保留',cancelled:'研究已停止，已有记录已保留'}[status] || ChatRender.clean(status);
  },

  _acceptNativeFrame: function(stream, frame) {
    var agent = frame.agent;
    if (!agent) {
      if (frame.pulse) return true;
      throw new Error('研究事件缺少运行记录，正在尝试恢复已保存进度');
    }
    if (typeof agent.run_id !== 'string' || !agent.run_id || agent.run_id.length > 128 || agent.engine !== 'prism-native' || !Number.isSafeInteger(agent.seq) || agent.seq < 1) {
      throw new Error('研究事件格式无效，正在尝试恢复已保存进度');
    }
    if (stream.runId && stream.runId !== agent.run_id) {
      var mismatch = new Error('研究记录不匹配，请重新打开原会话核对');
      mismatch.stopReplay = true; throw mismatch;
    }
    stream.runId = agent.run_id;
    var last = stream.lastSeq || 0;
    if (agent.seq <= last) return false;
    if (agent.seq !== last + 1) throw new Error('研究事件暂不连续，正在恢复已保存进度');
    stream.lastSeq = agent.seq;
    stream.agentStatusChanged = stream.agentStatus !== agent.status;
    stream.agentStatus = agent.status;
    stream.terminal = ['completed','partial','failed','cancelled','done'].indexOf(agent.status) >= 0;
    var message = stream.msgs && stream.msgs[stream.msgIdx];
    if (message) message.agent = Object.assign({}, agent);
    return true;
  },

  _readNativeStream: async function(stream, initialResponse, receive) {
    var self = this, response = initialResponse, failure = null, reconnects = 0;
    var interrupted = function() { return new Error('连接中断，已保留收到的内容。请重新打开原会话查看已保存进度；不会重复提交研究。'); };
    while (!stream.cancelled) {
      var reader = null, guard = null;
      try {
        if (!response) throw failure || interrupted();
        if (!response.ok || !response.body) {
          var unavailable = new Error('无法读取原研究记录（'+response.status+'），请重新打开原会话核对');
          unavailable.stopReplay = [401,403,404,409].indexOf(response.status) >= 0;
          if (response.status === 401) window.location.href = '/login';
          throw unavailable;
        }
        reader = response.body.getReader(); stream.reader = reader;
        if (App.convId === stream.convId) self._activeReader = reader;
        failure = null;
        var decoder = new TextDecoder(), buffer = '', lastStreamEventAt = Date.now(), streamTimedOut = false;
        var line = function(value) {
          value = value.replace(/\r$/, '');
          if (!value.startsWith('data:')) return;
          var data = value.slice(5).trim();
          if (data === '[DONE]') { stream.doneReceived = true; return; }
          if (!data) return;
          var frame;
          try { frame = JSON.parse(data); }
          catch(error) { throw new Error('研究记录读取中断，请重新打开原会话核对已保存进度'); }
          if (self._acceptNativeFrame(stream, frame)) receive(frame);
        };
        guard = setInterval(function() {
          if (Date.now() - lastStreamEventAt > 7 * 60 * 1000) {
            streamTimedOut = true;
            try { reader.cancel(); } catch(error) { /* The replay path handles EOF. */ }
          }
        }, 15000);
        while (!stream.cancelled && !stream.doneReceived && !stream.terminal) {
          var chunk = await reader.read();
          if (stream.cancelled) break;
          if (chunk.done) {
            buffer += decoder.decode();
            if (buffer.trim()) line(buffer);
            break;
          }
          lastStreamEventAt = Date.now();
          buffer += decoder.decode(chunk.value, {stream:true});
          var lines = buffer.split('\n'); buffer = lines.pop() || '';
          for (var i = 0; i < lines.length && !stream.doneReceived && !stream.terminal; i++) line(lines[i]);
        }
        if (streamTimedOut) failure = new Error('超过 7 分钟没有收到新内容，已尝试恢复原研究记录；不会重复提交研究。');
      } catch(error) { failure = error; }
      finally {
        if (guard) clearInterval(guard);
        if (reader) {
          try { await reader.cancel(); } catch(error) { /* Already closed. */ }
          if (reader.releaseLock) { try { reader.releaseLock(); } catch(error) {} }
          if (stream.reader === reader) stream.reader = null;
          if (self._activeReader === reader) self._activeReader = null;
        }
      }
      if (stream.cancelled || stream.terminal || stream.doneReceived) return;
      if (!stream.runId || reconnects >= 3 || (failure && failure.stopReplay)) throw failure || interrupted();
      reconnects += 1; stream.reconnects = reconnects;
      // Read only the original durable run. A transport failure never repeats POST.
      stream.transportController = new AbortController();
      try {
        response = await fetch('/api/agent/runs/'+encodeURIComponent(stream.runId)+'/stream?after_seq='+(stream.lastSeq || 0), {
          method:'GET', credentials:'same-origin',
          headers:{'Accept':'text/event-stream','X-Requested-With':'XMLHttpRequest'},
          signal:stream.transportController.signal
        });
      } catch(error) { response = null; failure = error; }
    }
  },

  send: async function() {
    if (this._sendStarting || (Streams[this.convId] && Streams[this.convId].active)) {
      if (this._toast) this._toast('当前会话仍在生成，请等待完成或先停止');
      return;
    }
    if (this._modelsLoaded && !this.model) { this._toast('尚无可用模型配置，请刷新配置或联系管理员；不会创建空的研究任务'); return; }
    if (this.selectAC()) return;
    var inp = document.getElementById('input'), q = inp.value.trim(); if (!q) return;
    var m = q.match(/^\/([a-zA-Z一-龥-]+)\s*(.*)/);
    if (m) {
      var cmd = m[1].toLowerCase();
      var found = this.skills.filter(function(s) { return s.shortcut === '/' + cmd || s.id === cmd || s.name === cmd; })[0];
      if (found) {
        this.setSkill(found.id); this.renderSkillsBar();
        q = m[2] || ''; inp.value = q; if (!q.trim()) return;
      }
    }
    if (this._workflowsUnavailable) { this._toast('研究方式暂不可用，请刷新后重试'); return; }
    var self = this;
    var skillForReq = ['prism-research','bottleneck-hunter','serenity','futures-research'].indexOf(this.skill) >= 0 ? this.skill : 'prism-research';
    var requestId;
    try { requestId = this._newAgentRequestId(); }
    catch(error) { this._toast(error.message); return; }
    var wasNew = !this.convId;
    if (!this.convId) {
      this._sendStarting = true;
      try { if (!(await this.newConv(true))) return; }
      finally { this._sendStarting = false; }
    }
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
    btn.disabled = false; btn.classList.add('stop-mode'); btn.onclick = function() { App.stopStream(); }; btn.title = '停止生成';
    // Activate loading bar
    var loadingBar = document.getElementById('loadingBar');
    if (loadingBar) loadingBar.classList.add('active');
    var msgIdx = this.msgs.length-1, full = '', chunks = [];
    var sid = this.convId;
    var requestMessages = this.msgs;
    var requestModel = this.model;
    var requestStream;

    // Keep ownership attached to the original message array across navigation.
    var _runStream = async function(_full, _chunks) {
      full = _full; chunks = _chunks;
      var stream = {msgs: requestMessages, msgIdx: msgIdx, chunks: chunks, full: full, skill: skillForReq, convId: sid, requestId: requestId, lastSeq: 0, following: true, active: true};
      requestStream = stream;
      Streams[sid] = stream;
      stream.msgs[msgIdx]._displayLen = 0;
      if (!_full) self._startTypewriter();

      try {
      stream.transportController = new AbortController();
      var resp = await fetch('/api/agent/chat/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest',
          'X-CSRF-Token': self._getCookie('csrf_token'),
        },
        body: JSON.stringify({question: q, skill: skillForReq, conv_id: sid, model: requestModel, request_id: requestId}),
        signal: stream.transportController.signal
      });
      if (resp.status===401){window.location.href='/login'; delete Streams[sid]; self._streamDone(sid, btn); return;}
      if (!resp.ok){
        var errText = '服务异常 (' + resp.status + ')';
        try { var eb = await resp.json(); errText = eb.error || errText; } catch(e) {}
        stream.msgs[msgIdx].content = errText; if (App.convId === sid) self.renderAll(); delete Streams[sid]; self._streamDone(sid, btn); return;
      }
      await self._readNativeStream(stream, resp, function(p) {
        self._applyNativeFrame(stream, p); full = stream.full; chunks = stream.chunks;
      });
    } catch(e) {
      var errorMsg = '连接中断，请重新打开原会话核对已保存进度；本次不会重复提交研究。';
      if (stream.cancelled) return;
      if (e.name === 'AbortError') {
        errorMsg = '连接已中断，请重新打开原会话核对研究状态';
      } else if (e.name === 'TypeError') {
        // A POST may already be running on the server. Never resubmit a paid
        // research job automatically after an ambiguous transport failure.
        errorMsg = '网络连接失败或中断。请重新打开本会话核对已保存内容；本次不会自动重复提交研究。';
      } else if (e.message) {
        errorMsg = ChatRender.clean(e.message);
      }
      stream.msgs[msgIdx].content = full + (full ? '\n\n' : '') + errorMsg;
      chunks.push({t:'text', c:(full ? '\n\n' : '') + errorMsg});
      stream.msgs[msgIdx]._chunks = chunks;
      if (App.convId === sid) App.renderAll();
    }
    stream.following = false;
  }; // end _runStream

  await _runStream('', []);

  // Clear typewriter state before deleting the stream so the final
  // renderOne (called from _streamDone) shows full content, not a
  // truncated typewriter view. Also reset _lastSig so the dedup check
  // in renderOne doesn't block the final render.
  var s = Streams[sid];
  if (s && s.msgIdx != null && s.msgs && s.msgs[s.msgIdx]) {
    s.msgs[s.msgIdx]._displayLen = undefined;
    s.msgs[s.msgIdx]._lastSig = null;
  }
  if (Streams[sid] === requestStream) {
    if (requestStream.terminal || requestStream.doneReceived || requestStream.cancelled) {
      delete Streams[sid];
      self._streamDone(sid, btn);
    } else {
      // A lost transport does not prove the durable background run stopped.
      // Keep its stop control and block another paid turn until it is checked.
      requestStream.offline = true;
      self._stopTypewriter(sid);
      if (self.convId === sid) self._syncChatStreamControls();
    }
  }
    if (App.convId === sid) {
      App._buildTOC();
      App.loadConvs();
      // Reports may have been generated during this stream — refresh the button.
      App._refreshReportsButton();
    } else {
      // Even if the user is on a different conv, refresh anyway so when they
      // switch back the button state is current.
      self._refreshReportsButton();
    }
  },

  _streamDone: function(sid, btn) {
    if (App.convId === sid) {
      btn.disabled = false; btn.classList.remove('stop-mode'); btn.onclick = function() { App.send(); }; btn.title = '发送 (Enter)';
      var loadingBar = document.getElementById('loadingBar');
      if (loadingBar) loadingBar.classList.remove('active');
      this._activeReader = null;
    }
    this._stopTypewriter(sid);
    // Final full render for the completed message.
    // Access msg via App.msgs (Streams[sid] may have been deleted already).
    if (App.convId === sid) {
      var curMsgIdx = App.msgs.length - 1;
      if (curMsgIdx >= 0 && App.msgs[curMsgIdx].role === 'assistant') {
        App.msgs[curMsgIdx]._displayLen = undefined;
        App.msgs[curMsgIdx]._lastSig = null;
        App.renderOne(curMsgIdx);
      }
    }
  },

  /* ═══ Stop Generation ═══ */
  stopStream: async function() {
    var sid = this.convId, activeStream = Streams[sid];
    if (!sid || (activeStream && activeStream.stopping)) return;
    if (activeStream) activeStream.stopping = true;
    this._syncChatStreamControls();
    var controller = new AbortController();
    var timer = setTimeout(function() { controller.abort(); }, 5000);
    var confirmed = false;
    try {
      var response = await this.api('/api/agent/chat/cancel', {
        method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,
        body:JSON.stringify({conv_id:sid,run_id:activeStream && activeStream.runId || undefined})
      });
      if (!response.ok) throw new Error('Cancellation failed');
      var result = await response.json();
      if (result.ok !== true) throw new Error('Cancellation failed');
      var runId = activeStream && activeStream.runId || result.run_id;
      if (activeStream && activeStream.runId && result.run_id && result.run_id !== activeStream.runId) throw new Error('Cancellation run mismatch');
      if (activeStream && !activeStream.runId && typeof runId === 'string') activeStream.runId = runId;
      var terminal = ['completed','partial','failed','cancelled','done'];
      confirmed = terminal.indexOf(result.status) >= 0 || !!(activeStream && activeStream.terminal);
      var end = Date.now() + 4000;
      // An accepted cancellation is intent. Keep the reader and ownership until
      // the actual persisted terminal status confirms transport/tool cleanup.
      while (!confirmed && runId && Date.now() < end) {
        var state = await this.api('/api/agent/runs/'+encodeURIComponent(runId), {signal:controller.signal});
        if (!state.ok) throw new Error('Cancellation state unavailable');
        var current = (await state.json()).run;
        if (!current || current.run_id !== runId) throw new Error('Cancellation state mismatch');
        confirmed = terminal.indexOf(current.status) >= 0 || !!(activeStream && activeStream.terminal);
        if (!confirmed) await new Promise(function(resolve) { setTimeout(resolve, 200); });
      }
      if (!confirmed) throw new Error('Cancellation terminal not confirmed');
      if (activeStream) {
        activeStream.terminal = true; activeStream.cancelled = true;
        if (activeStream.transportController) activeStream.transportController.abort();
        if (activeStream.reader) { try { await activeStream.reader.cancel(); } catch(error) {} }
      }
      if (Streams[sid] === activeStream) delete Streams[sid];
      if (!Streams[sid]) this._stopTypewriter(sid);
      if (this.convId === sid && !Streams[sid]) {
        this._syncChatStreamControls();
        // Read the final owned message/report, rather than declaring the live
        // partial buffer a completed cancellation or creating a second turn.
        await this.openConv(sid);
      }
    } catch(error) {
      if (this._toast) this._toast('停止已请求，但未确认后台取消成功。已有进度保留，请稍后再核对或重新请求停止。');
    } finally {
      clearTimeout(timer);
      if (activeStream) activeStream.stopping = false;
      if (this.convId === sid) this._syncChatStreamControls();
    }
  },

  /* ═══ Logout ═══ */
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
  },

  /* ═══ Analysis Graph ═══ */
  openGraph: async function() {
    if (!this.convId) return;
    var modal = document.getElementById('graphModal');
    if (!modal) return;
    modal.style.display = 'flex';
    document.getElementById('graphModalTitle').textContent = '分析关系图';
    var container = document.getElementById('graphChart');
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:500px;color:var(--text3)">加载中…</div>';
    try {
      var r = await this.api('/api/convs/' + this.convId + '/graph');
      if (!r.ok) { container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:500px;color:var(--text3)">暂无关系图数据</div>'; return; }
      var data = await r.json();
      if (!data.nodes || !data.nodes.length) { container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:500px;color:var(--text3)">暂无关系图数据</div>'; return; }
      this._renderGraph(container, data);
    } catch(e) {
      container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:500px;color:var(--text3)">加载失败</div>';
    }
  },
  closeGraph: function() {
    var modal = document.getElementById('graphModal');
    if (modal) modal.style.display = 'none';
  },
  _renderGraph: function(container, data) {
    if (typeof echarts === 'undefined') { container.innerHTML = '图表库未加载'; return; }
    container.innerHTML = '';
    var chart = echarts.init(container);
    var nodes = data.nodes.map(function(n) {
      var colors = {question:'#6b95c8',tool_call:'#d97706',think:'#706c64',analysis:'#3b5d8a',reference:'#7c5fd0'};
      var c = colors[n.node_type] || '#706c64';
      return {id:n.id, name:n.label, category:n.node_type, symbolSize:14,
        itemStyle:{color:c,borderColor:'rgba(255,255,255,.15)',borderWidth:1}};
    });
    var edges = data.edges.map(function(e) {
      return {source:e.source_node_id, target:e.target_node_id,
        lineStyle:{color:'rgba(255,255,255,.12)',width:1,curveness:0.2}};
    });
    chart.setOption({
      animation:true, backgroundColor:'transparent', tooltip:{},
      series:[{type:'graph', layout:'force', roam:true, draggable:true,
        force:{repulsion:200, edgeLength:[80,200]},
        data:nodes, links:edges,
        lineStyle:{color:'rgba(255,255,255,.12)',curveness:0.2},
        label:{show:true, position:'right', fontSize:10, color:'#b8b2a6',
          formatter:function(p){return p.name.length>30?p.name.substring(0,28)+'…':p.name;}},
      }],
    });
    var self = this;
    window.addEventListener('resize', function(){try{chart.resize();}catch(e){}});
  },

});
