/* ═══════════════════════════════════════
   Prism — Application Logic (v0.8 split)
   ═══════════════════════════════════════ */

/* ═══ Utilities ═══ */
function escHtml(s) { return redactSourceText(String(s||'')).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function esc(s) { return redactSourceText(String(s||'')).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return redactSourceText(String(s||'')).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }

// Display/registration boundary only: this cannot establish DNS destinations or
// restrict a CLI's network. Keep in step with server/source_urls.py fixtures.
function _sourceUrlDecoded(s, keepPercent) {
  for (var i=0;i<3;i++) {
    var entities={amp:'&',colon:':',sol:'/',bsol:'\\',commat:'@',percnt:'%',quest:'?',num:'#',equals:'=',period:'.',lowbar:'_',underbar:'_',tab:'\t',newline:'\n'};
    var next=s.replace(/&#(?:x([a-f0-9]+)|([0-9]+));?/gi,function(raw,hex,decimal) {
      var code=parseInt(hex||decimal,hex?16:10);
      return code>0 && code<=0x10ffff ? String.fromCodePoint(code) : '\u0000';
    }).replace(/&([a-z][a-z0-9]+);/gi,function(raw,name) {
      return Object.prototype.hasOwnProperty.call(entities,name.toLowerCase())?entities[name.toLowerCase()]:'_';
    });
    if (!keepPercent) { try { next=decodeURIComponent(next); } catch(e) {} }
    if (next===s) break; s=next;
  }
  return s;
}
function _publicSourceHost(host) {
  host=host.toLowerCase().replace(/^\[|\]$/g,'').replace(/\.$/,'');
  if (!host || host.indexOf('%')>=0) return false;
  if (host.indexOf(':')>=0) {
    var parts=host.split(':'), first=parseInt(parts[0],16), second=parseInt(parts[1]||'0',16);
    return first>=0x2000 && first<=0x3fff && first!==0x2002 && first!==0x3fff &&
      !(first===0x2001 && (second<=0x1ff || second===0xdb8));
  }
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
    var p=host.split('.').map(Number), a=p[0], b=p[1], c=p[2];
    return p.every(function(n){return n>=0 && n<=255;}) && a!==0 && a!==10 && a!==127 && a<224 &&
      !(a===100 && b>=64 && b<=127) && !(a===169 && b===254) && !(a===172 && b>=16 && b<=31) &&
      !(a===192 && (b===168 || (b===0 && (c===0 || c===2)) || (b===88 && c===99))) &&
      !(a===198 && (b===18 || b===19 || (b===51 && c===100))) && !(a===203 && b===0 && c===113);
  }
  var labels=host.split('.');
  return host.length<=253 && labels.length>=2 && !/^\d+$/.test(labels[labels.length-1]) &&
    !/\.(?:localhost|local|localdomain|internal|lan|home|arpa|onion)$/.test(host) &&
    labels.every(function(label){return /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label);});
}
function publicSourceUrl(value) {
  if (typeof value!=='string' || !value || value.length>4000 || /[\u0000-\u0020\u007f\\]/.test(value)) return '';
  var names=new Set(['key','apikey','xapikey','subscriptionkey','ocpapimsubscriptionkey','token','accesstoken','refreshtoken','idtoken','authtoken','auth','authorization','authentication','password','passwd','secret','clientsecret','privatekey','accountkey','credential','credentials','cookie','sessionid','signature','sig','sharedaccesssignature','awsaccesskeyid','xamzsignature','xamzcredential','xamzsecuritytoken','xgoogsignature','xgoogcredential']);
  try {
    for (var candidate of [value,_sourceUrlDecoded(value)]) {
      if (/[\u0000-\u001f\u007f\\]/.test(candidate) || !/^https?:\/\//i.test(candidate)) return '';
      var parsed=new URL(candidate);
      if (parsed.username || parsed.password || !_publicSourceHost(parsed.hostname)) return '';
      var authority=candidate.match(/^https?:\/\/([^/?#]+)/i)[1], rawHost=authority.replace(/:\d+$/,'').toLowerCase().replace(/\.$/,'');
      if (authority.indexOf('@')>=0) return '';
      if (rawHost.indexOf('%')>=0 || (/^\d+\.\d+\.\d+\.\d+$/.test(parsed.hostname) && rawHost!==parsed.hostname)) return '';
      var fragment=parsed.hash.slice(1);
      for (var params of [parsed.search.slice(1),fragment,fragment.indexOf('?')>=0?fragment.slice(fragment.indexOf('?')+1):'']) {
        for (var pair of new URL('https://prism-display.example/?'+params).searchParams) {
          var name=_sourceUrlDecoded(pair[0]).replace(/&[a-z][a-z0-9]+;/gi,'');
          if (names.has(name.toLowerCase().replace(/[^a-z0-9]/g,''))) return '';
        }
      }
    }
    return value;
  } catch(e) { return ''; }
}
function redactSourceText(value) {
  return String(value===null || value===undefined?'':value).replace(/(?:https?(?::|%3a|&(?:colon;|#(?:x0*3a|0*58);))|[a-z][a-z0-9+.-]*:(?:\\?\/){2}|(?<![\w:/\\])(?:\\?\/){2})[^\s<>"']+/gi,function(raw) {
    var url=raw.replace(/[)\]}.,;，。；]+$/,''), tail=raw.slice(url.length);
    var decoded=_sourceUrlDecoded(url.replace(/\\\//g,'/'),true);
    if (decoded.indexOf('//')===0) decoded='https:'+decoded;
    return (publicSourceUrl(decoded)?url:'[链接已隐藏]')+tail;
  });
}
function safeUrl(u) {
  if (!u) return '';
  var s = String(u).trim();
  // Browsers remove tabs/newlines before resolving a URL's scheme. Reject
  // those bytes before parsing so an obfuscated script URL cannot become a link.
  if (!s || /[\u0000-\u001f\u007f]/.test(s)) return '';
  try {
    var parsed = new URL(s, 'https://prism.invalid/');
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return '';
    if (parsed.username || parsed.password) return '';
    if (/^https?:\/\//i.test(s)) return publicSourceUrl(s);
    if (s.indexOf('//')===0) return publicSourceUrl('https:'+s)?s:'';
    // Keep same-site relative paths while rejecting redaction placeholders.
    return s.indexOf('[链接已隐藏]')<0 && !/^[a-z][a-z0-9+.-]*:/i.test(s) &&
      publicSourceUrl(new URL(s,'https://prism-display.example/').href) ? s : '';
  } catch(e) { return ''; }
}

/* ── Marked 安全配置 + HTML 净化 ──
   防御 AI 输出 / 第三方 skill 报告里的 XSS 攻击。
   marked 本身不做 HTML 净化，必须我们自己处理。 */
(function configureMarked() {
  if (typeof marked === 'undefined') return;
  // Use the same parsed protocol allowlist for Markdown links and images.
  const renderer = new marked.Renderer();
  const origLink = renderer.link.bind(renderer);
  const origImage = renderer.image.bind(renderer);
  renderer.link = function(token) {
    token = Object.assign({}, token || {});
    token.href = safeUrl(token.href) || '#';
    return origLink(token);
  };
  renderer.image = function(token) {
    token = Object.assign({}, token || {});
    token.href = safeUrl(token.href);
    return origImage(token);
  };
  // Blockquotes are never intentional in this domain — render > as literal text,
  // preserving the original character so nothing is lost. This is the root fix
  // for the recurring ">供应链" / "">瓶颈" rendering bugs (marked was turning
  // those stray >'s into <blockquote> and the escaping regex was a band-aid).
  renderer.blockquote = function(token) {
    var html = this.parser.parse((token && token.tokens) || []);
    return '<div class="md-literal-quote"><span class="md-gt">&gt;</span><div>' + html + '</div></div>';
  };
  renderer.paragraph = function(token) {
    return '<p>' + this.parser.parseInline((token && token.tokens) || []) + '</p>\n';
  };
  marked.setOptions({ renderer, gfm: true, breaks: true, headerIds: false, mangle: false });
})();

/* 净化 marked 输出：剥离危险标签、on* 事件、危险 URL scheme。 */
function sanitizeHtml(html) {
  if (!html) return '';
  var s = redactSourceText(typeof html === 'string' ? html : String(html));
  // Pre-decode entity-encoded quotes so the regexes below catch
  // obfuscated on* handlers and javascript: URLs (e.g. &#34; → ").
  s = s.replace(/&quot;/gi, '"').replace(/&#0*34;/gi, '"');
  s = s.replace(/&#0*39;/gi, "'").replace(/&apos;/gi, "'");
  s = s.replace(/&#x0*27;/gi, "'");
  // Remove dangerous container tags (with content)
  s = s.replace(/<script\b[\s\S]*?<\/script>/gi, '');
  s = s.replace(/<style\b[\s\S]*?<\/style>/gi, '');
  s = s.replace(/<iframe\b[\s\S]*?<\/iframe>/gi, '');
  s = s.replace(/<object\b[\s\S]*?<\/object>/gi, '');
  s = s.replace(/<form\b[\s\S]*?<\/form>/gi, '');
  // 自闭合危险标签
  s = s.replace(/<embed\b[^>]*\/?>/gi, '');
  s = s.replace(/<link\b[^>]*\/?>/gi, '');
  s = s.replace(/<meta\b[^>]*\/?>/gi, '');
  s = s.replace(/<base\b[^>]*\/?>/gi, '');
  // on* 事件属性（引号 / 单引号 / 无引号）
  s = s.replace(/\s+on[a-z]+\s*=\s*"[^"]*"/gi, '');
  s = s.replace(/\s+on[a-z]+\s*=\s*'[^']*'/gi, '');
  s = s.replace(/\s+on[a-z]+\s*=\s*[^\s>]+/gi, '');
  // 危险 URL scheme（仅在属性值内）
  s = s.replace(/(href|src|action|formaction|srcset|xlink:href)\s*=\s*"\s*(javascript|vbscript)\s*:[^"]*"/gi, '$1="#"');
  s = s.replace(/(href|src|action|formaction|srcset|xlink:href)\s*=\s*'\s*(javascript|vbscript)\s*:[^']*'/gi, "$1='#'");
  s = s.replace(/(href|src|action|formaction|srcset|xlink:href)\s*=\s*(javascript|vbscript)\s*:[^\s>]*/gi, '$1="#"');
  // Raw HTML bypasses Marked's link renderer. Validate decoded URL attributes
  // too, including protocol-relative links and numeric HTML entities.
  s = s.replace(/\b(href|src|action|formaction|xlink:href)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi,function(raw,name,double,single,bare) {
    var url=safeUrl(_sourceUrlDecoded(double!==undefined?double:single!==undefined?single:bare));
    return name+'="'+escAttr(url)+'"';
  });
  s = s.replace(/\bsrcset\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi,'');
  return s;
}

/* Escape every > character so marked.js NEVER interprets any text as a
   blockquote.  > renders as a literal > in financial analysis text, and
   unintentional > at line-start (e.g. ">供应链" / "">瓶颈") has been the
   #1 recurring rendering bug.  &gt; displays identically to > in the
   browser — zero visual difference — but cannot trigger a <blockquote>. */
function escapeAllGt(text) {
  text = String(text||'');
  return text.replace(/>/g, '&gt;');
}

/* 在 markdown 解析之前处理 ==术语== 显式标记。
   必须在 marked.parse 之前执行，否则 markdown 已渲染的 <strong> 等标签
   会被 _glossifyTerms 的后续正则错误地重新转义。 */
function _preGlossify(text) {
  text = String(text||'');
  if (!text) return '';
  return text.replace(/==([^=]+?)==/g, function(m, term) {
    var t = term.trim();
    // 跳过含 markdown 语法的匹配（*bold*、_italic_、`code` 等），
    // 避免破坏已渲染的 HTML 标签。
    if (/[*_~`\\]/.test(t)) return m;
    return '<span class="gloss-term gloss-explicit" data-term="' + escAttr(t) + '" title="点击查看AI解释">' + esc(t) + '</span>';
  });
}

/* During streaming, the text is often half-rendered markdown — an unclosed
   ``` fence swallows the rest into a code block, an open ** bolds everything
   after. Close any unbalanced fences / emphasis so partial input still renders
   readably. Idempotent for already-complete markdown (even counts → no-op). */
function _balanceMarkdown(text) {
  text = String(text||'');
  if (!text) return '';
  // Unclosed ``` code fence → close it.
  if ((text.match(/```/g) || []).length % 2 === 1) text += "\n```";
  // Count emphasis markers OUTSIDE code spans/fences; close unbalanced pairs.
  var noCode = text.replace(/```[\s\S]*?```/g, "").replace(/`[^`\n]*`/g, "");
  ["~~", "**", "__"].forEach(function(sym) {
    var i = 0, n = 0;
    while ((i = noCode.indexOf(sym, i)) !== -1) { n++; i += sym.length; }
    if (n % 2 === 1) text += sym;
  });
  return text;
}

function render(t) {
  t = redactSourceText(t);
  try {
    var html = marked.parse(escapeAllGt(_preGlossify(_balanceMarkdown(t))));
    if (typeof html !== 'string') return escHtml(t);
    return sanitizeHtml(html).replace(/\[object Object\]/g, '');
  }
  catch(e) { return escHtml(t); }
}

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

