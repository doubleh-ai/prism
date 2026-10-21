/* Prism - Chat Content Renderer (v0.19.7) - strips CLI object artifacts */
var ChatRender = (function() {

  function _stripObjectArtifacts(t, trim) {
    t = String(t || '')
      .replace(/\[object Object\]/g, '')
      .replace(/([^\n])\s*["']>\s*(?=[\u3400-\u9fffA-Za-z0-9])/g, '$1 ')
      .replace(/\n{3,}/g, '\n\n');
    return trim ? t.trim() : t;
  }

  function _s(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return _stripObjectArtifacts(v, false);
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    if (Array.isArray(v)) return v.map(_s).filter(Boolean).join('\n');
    if (typeof v === 'object') {
      if (v.type === 'text' && typeof v.text === 'string') return _s(v.text);
      if (v.type === 'thinking' && typeof v.thinking === 'string') return _s(v.thinking);
      if (v.type === 'tool_result') return _s(v.content);
      if (v.type === 'tool_use') {
        var tool = v.name || 'tool';
        var detail = _s(v.input || '');
        return detail ? tool + ': ' + detail : String(tool);
      }
      if (typeof v.text === 'string') return _s(v.text);
      if (typeof v.thinking === 'string') return _s(v.thinking);
      if (v.content !== undefined) return _s(v.content);
      if (v.message && v.message.content !== undefined) return _s(v.message.content);
      try { return _stripObjectArtifacts(JSON.stringify(v, null, 2), false); } catch(e) { return ''; }
    }
    return _stripObjectArtifacts(String(v), false);
  }

  function _clean(t) {
    // Claude CLI (Node.js) sometimes stringifies internal objects into
    // the thinking stream - strip these artifacts. The real content is
    // human-readable text, never the literal string "[object Object]".
    return _stripObjectArtifacts(_s(t), true);
  }

  function _md(t) {
    t = _s(t); if (!t) return '';
    try { return render(t); } catch(e) { return esc(t); }
  }

  return {
    stringify: _s,
    clean: _clean,

    formatTime: function(value) {
      if (value === null || value === undefined || value === '') return '未提供';
      if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return String(value);
      var numeric = typeof value === 'number' || /^-?\d+(?:\.\d+)?$/.test(String(value));
      var date;
      if (numeric) {
        var timestamp = Number(value);
        if (!Number.isFinite(timestamp)) return '未提供';
        date = new Date(Math.abs(timestamp) < 1e11 ? timestamp * 1000 : timestamp);
      }
      // Do not silently interpret an unzoned historical timestamp as local time.
      else if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(String(value))) return _clean(value) + '（时区未标注）';
      else date = new Date(value);
      if (!Number.isFinite(date.getTime())) return '未提供';
      return new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date) + ' 北京时间';
    },

    collectRefs: function(refs, chunks) {
      var result = [], positions = Object.create(null);
      (refs || []).concat((chunks || []).filter(function(c) { return c && c.t === 'ref'; })).forEach(function(ref) {
        if (!ref || typeof ref !== 'object') return;
        var key = [ref.run_id || '', ref.source_kind || '', ref.url || ref.title || ''].join('|');
        if (!ref.url && !ref.title) return;
        if (positions[key] !== undefined) {
          var existing = result[positions[key]];
          Object.keys(ref).forEach(function(k) { if (ref[k] !== null && ref[k] !== undefined && ref[k] !== '') existing[k] = ref[k]; });
        } else { positions[key] = result.length; result.push(Object.assign({}, ref, {t:'ref'})); }
      });
      return result;
    },

    renderResearch: function(context) {
      if (!context || typeof context !== 'object') return '';
      return '<div class="research-context"><span>本轮研究截至 <strong>' + esc(this.formatTime(context.as_of)) + '</strong></span>' +
        '<span>来源数据截至 <strong>以各来源标注为准；未提供则未知</strong></span></div>';
    },

    renderContent: function(input) {
      input = _s(input); if (!input) return '';
      return _md(_clean(input));
    },

    renderChunk: function(c, options) {
      if (!c) return '';
      options = options || {};
      var t = c.t || 'text';
      var v = _clean(_s(c.c));
      if (t === 'text')  return _md(v);
      if (t === 'think') return '<details class="think-box"' + (options.open ? ' open' : '') + '><summary>分析过程</summary><div class="think-content">'+_md(v)+'</div></details>';
      if (t === 'tool')  return '<div class="cr-tool"><details class="cr-tool-det"><summary><b>'+esc(v)+'</b></summary><pre class="cr-tool-body">'+esc(_clean(_s(c.d)))+'</pre></details></div>';
      if (t === 'ask') {
        var opts = Array.isArray(c.opts) ? c.opts : _s(c.opts).split('\n').filter(Boolean);
        return '<div class="ask-card"><div class="ask-q">'+esc(_s(c.q))+'</div><div class="ask-opts">'+opts.map(function(o){var value=esc(_s(o)).replace(/"/g,'&quot;').replace(/'/g,'&#39;');return '<button class="ask-opt" data-ask-answer="'+value+'">'+esc(_s(o))+'</button>';}).join('')+'</div></div>';
      }
      if (t === 'research') return '';
      if (t === 'ref') {
        var kind = c.source_kind === 'external_web' ? '外部网页' : c.source_kind === 'historical_research' ? '历史研究' : (c.source_kind && c.source_kind.indexOf('platform') === 0) ? '平台数据' : '来源归属未标注';
        var status = c.evidence_status === 'retrieved' ? '已采集 · 未核验' : '待核验';
        var url = safeUrl(_s(c.url));
        var title = esc(_s(c.title || c.url || '未命名来源'));
        return '<div class="ref-item"><span class="ref-idx"></span><div class="ref-body"><div class="ref-meta"><span>'+esc(kind)+'</span><span>'+status+'</span></div>' +
          (url ? '<a class="ref-link" href="'+escAttr(url)+'" target="_blank" rel="noopener noreferrer">'+title+' ↗</a>' : '<span class="ref-link">'+title+'</span>') +
          (c.snippet?'<div class="ref-snippet">'+esc(_s(c.snippet))+'</div>':'') +
          '<div class="ref-time">发布：'+esc(this.formatTime(c.published_at))+'<br>采集：'+esc(this.formatTime(c.retrieved_at))+'</div></div></div>';
      }
      return _md(v);
    },

    renderSteps: function(steps, streaming) {
      if (!steps || !steps.length) return '';
      var n = steps.length;
      var h = '<div class="step-bar'+(streaming?'':' stable')+'">';

      function stepTag(s, extraClass) {
        var name = _clean(_s(s.c||s.name||s));
        var detail = s.d ? _clean(_s(s.d)) : '';
        return '<span class="step-tag' + (extraClass || '') + '"><b>' + esc(name) + '</b>' +
          (detail ? ' <i title="' + escAttr(_s(s.d)) + '">' + esc(detail) + '</i>' : '') +
          '</span>';
      }

      var completed = streaming ? steps.slice(0, Math.max(0, n - 1)) : steps;
      if (completed.length) {
        h += '<details class="step-fold"><summary>已完成 '+completed.length+' 步</summary>';
      }
      for (var i=0;i<completed.length;i++) {
        var s = steps[i];
        h += stepTag(s, '');
      }
      if (completed.length) h += '</details>';
      if (streaming && n>0) {
        var last = steps[n-1];
        h += stepTag(last, ' now step-now');
      }
      h += '</div>';
      return h;
    }
  };
})();
