/* ═══════════════════════════════════════
   Prism — Application Logic (v0.8 split)
   ═══════════════════════════════════════ */

/* ═══ Utilities ═══ */
function escHtml(s) { return String(s||'').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escAttr(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,"\\'").replace(/"/g,'&quot;'); }
function safeUrl(u) { if (!u) return ''; var s=String(u).trim(); return /^(javascript|vbscript|data):/i.test(s) ? '' : s; }

/* ── Marked 安全配置 + HTML 净化 ──
   防御 AI 输出 / 第三方 skill 报告里的 XSS 攻击。
   marked 本身不做 HTML 净化，必须我们自己处理。 */
(function configureMarked() {
  if (typeof marked === 'undefined') return;
  // 仅允许安全的 URL scheme。javascript:/vbscript:/data:text/html 一律放空。
  function safeUrl(url) {
    if (!url) return '';
    const s = String(url).trim();
    if (/^(javascript|vbscript|data):/i.test(s)) return '';
    return url;
  }
  const renderer = new marked.Renderer();
  const origLink = renderer.link.bind(renderer);
  const origImage = renderer.image.bind(renderer);
  renderer.link = function(href, title, text) {
    return origLink(safeUrl(href), title, text);
  };
  renderer.image = function(href, title, text) {
    return origImage(safeUrl(href), title, text);
  };
  // Blockquotes are never intentional in this domain — render > as literal text,
  // preserving the original character so nothing is lost. This is the root fix
  // for the recurring ">供应链" / "">瓶颈" rendering bugs (marked was turning
  // those stray >'s into <blockquote> and the escaping regex was a band-aid).
  renderer.blockquote = function(html) { return '<p><span class="md-gt">&gt;</span> ' + html + '</p>'; };
  renderer.paragraph = function(text) { return '<p>' + text + '</p>'; };
  marked.setOptions({ renderer, gfm: true, breaks: true, headerIds: false, mangle: false });
})();

/* 净化 marked 输出：剥离危险标签、on* 事件、危险 URL scheme。 */
function sanitizeHtml(html) {
  if (!html) return '';
  var s = html;
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
  try { return sanitizeHtml(marked.parse(escapeAllGt(_preGlossify(_balanceMarkdown(t))))); }
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


