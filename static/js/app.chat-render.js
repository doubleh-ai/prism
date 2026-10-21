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
        return '<div class="ask-card"><div class="ask-q">'+esc(_s(c.q))+'</div><div class="ask-opts">'+opts.map(function(o){return '<button class="ask-opt" onclick="App.answerAsk(\''+escAttr(_s(o))+'\')">'+esc(_s(o))+'</button>';}).join('')+'</div></div>';
      }
      if (t === 'ref')   return '<div class="ref-item"><span class="ref-idx"></span><div><a class="ref-link" href="'+escAttr(safeUrl(c.url)||'#')+'" target="_blank" rel="noopener">'+esc(_s(c.title||c.url))+'</a>'+(c.snippet?'<div class="ref-snippet">'+esc(_s(c.snippet))+'</div>':'')+'</div></div>';
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
