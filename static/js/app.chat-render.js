/* Prism — Chat Content Renderer (v0.14) — strips CLI [object Object] artifacts */
var ChatRender = (function() {

  function _s(v) {
    if (v === null || v === undefined) return '';
    if (typeof v === 'string') return v;
    if (typeof v === 'number' || typeof v === 'boolean') return String(v);
    if (Array.isArray(v)) return v.map(_s).filter(Boolean).join('\n');
    if (typeof v === 'object') {
      if (v.text && typeof v.text === 'string') return v.text;
      if (v.content && typeof v.content === 'string') return v.content;
      try { return JSON.stringify(v, null, 2); } catch(e) { return ''; }
    }
    return String(v);
  }

  function _clean(t) {
    // Claude CLI (Node.js) sometimes stringifies internal objects into
    // the thinking stream — strip these artifacts. The real content is
    // human-readable text, never the literal string "[object Object]".
    return t.replace(/\[object Object\]/g, '').replace(/\n{3,}/g, '\n\n');
  }

  function _md(t) {
    t = _s(t); if (!t) return '';
    try { return render(t); } catch(e) { return esc(t); }
  }

  return {
    stringify: _s,

    renderContent: function(input) {
      input = _s(input); if (!input) return '';
      return _md(_clean(input));
    },

    renderChunk: function(c) {
      if (!c) return '';
      var t = c.t || 'text';
      var v = _clean(_s(c.c));
      if (t === 'text')  return _md(v);
      if (t === 'think') return '<details class="think-box" open><summary>Thinking</summary><div class="think-content">'+_md(v)+'</div></details>';
      if (t === 'tool')  return '<div class="cr-tool"><details class="cr-tool-det"><summary><b>'+esc(v)+'</b></summary><pre class="cr-tool-body">'+esc(_clean(_s(c.d)))+'</pre></details></div>';
      if (t === 'ask')   return '<div class="ask-card"><div class="ask-q">'+esc(_s(c.q))+'</div><div class="ask-opts">'+(c.opts||[]).map(function(o){return '<button class="ask-opt" onclick="App.answerAsk(\''+escAttr(_s(o))+'\')">'+esc(_s(o))+'</button>';}).join('')+'</div></div>';
      if (t === 'ref')   return '<div class="ref-item"><span class="ref-idx"></span><div><a class="ref-link" href="'+escAttr(safeUrl(c.url)||'#')+'" target="_blank" rel="noopener">'+esc(_s(c.title||c.url))+'</a>'+(c.snippet?'<div class="ref-snippet">'+esc(_s(c.snippet))+'</div>':'')+'</div></div>';
      return _md(v);
    },

    renderSteps: function(steps, streaming) {
      if (!steps || !steps.length) return '';
      var n = steps.length, fold = n >= 3;
      var h = '<div class="step-bar'+(streaming?'':' stable')+'">';
      if (fold && !streaming) h += '<details class="step-fold"><summary>已完成 '+n+' 步</summary>';
      var end = streaming ? n-1 : n;
      for (var i=0;i<end;i++) {
        var s = steps[i];
        h += '<span class="step-tag"><b>'+esc(_clean(_s(s.c||s.name||s)))+'</b>'+(s.d?' <i title="'+escAttr(_s(s.d))+'">'+esc(_clean(_s(s.d)))+'</i>':'')+'</span>';
      }
      if (streaming && n>0) {
        var last = steps[n-1];
        h += '<span class="step-tag now step-now"><b>'+esc(_clean(_s(last.c||last.name)))+'</b>'+(last.d?' <i title="'+escAttr(_s(last.d))+'">'+esc(_clean(_s(last.d)))+'</i>':'')+'</span>';
      }
      if (fold && !streaming) h += '</details>';
      h += '</div>';
      return h;
    }
  };
})();
