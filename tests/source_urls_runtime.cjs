/* Shipped JS and real Marked; synthetic URLs only, no browser/network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const ctx={URL,URLSearchParams,Set,Intl,console,marked:require('../static/lib/marked.min.js').marked};
vm.createContext(ctx);
for(const name of ['app.util.js','app.chat-render.js']) vm.runInContext(fs.readFileSync('static/js/'+name,'utf8'),ctx,{filename:name});
for(const url of input.bad) {
  assert.equal(ctx.publicSourceUrl(url),'');
  assert.equal(ctx.safeUrl(url),'');
  const detail=ctx.ChatRender.renderChunk({t:'tool',c:'WebFetch',d:JSON.stringify({url})});
  const fallback=ctx.render('['+url+']('+url+')');
  const plain=ctx.render(url);
  const native=ctx.sanitizeHtml('<a href="'+url+'">'+url+'</a><img src="'+url+'">');
  const ref=ctx.ChatRender.renderChunk({t:'ref',url,title:url,snippet:url});
  for(const html of [detail,fallback,plain,native,ref]) {
    assert(!html.includes('SYNTHETIC_OTHER_KEY'));
    assert(!html.includes(url));
    assert(html.includes('[链接已隐藏]'));
  }
}
for(const url of input.good) {
  assert.equal(ctx.publicSourceUrl(url),url);
  assert.equal(ctx.safeUrl(url),url);
  assert(ctx.render('[source]('+url+')').includes('href="'));
}
assert.equal(ctx.safeUrl('/api/reports/owned/document.html'),'/api/reports/owned/document.html');
for(const path of ['document.html','./document.html','../document.html','#section','?page=2']) assert.equal(ctx.safeUrl(path),path);
for(const url of ['//127.0.0.1/a','https&#58;//127.0.0.1/a','https&colon;&sol;&sol;127.0.0.1/a']) {
  const html=ctx.sanitizeHtml('<a href="'+url+'">source</a>');
  assert(!html.includes('href="'+url+'"'));
  assert(!/href="(?:https?:)?\/\//i.test(html));
}
assert(ctx.sanitizeHtml('<a href="/api/reports/owned/document.html">report</a>').includes('href="/api/reports/owned/document.html"'));
if(input.legacy) assert(!ctx.ChatRender.renderChunk({t:'tool',c:'WebFetch',d:"{'url': '"+input.legacy+"'}"}).includes('SYNTHETIC_OTHER_KEY'));
for(const event of input.events||[]) {
  const html=event.tool?ctx.ChatRender.renderChunk({t:'tool',c:event.tool,d:event.d}):event.text?ctx.render(event.text):'';
  assert(!html.includes('SYNTHETIC_OTHER_KEY'));
}
for(const message of (input.history||{}).messages||[]) {
  const content=message.role==='assistant'?JSON.parse(message.content):{text:message.content};
  assert(!ctx.render(content.text||'').includes('SYNTHETIC_OTHER_KEY'));
  for(const chunk of content.chunks||[]) assert(!ctx.ChatRender.renderChunk(chunk).includes('SYNTHETIC_OTHER_KEY'));
}
console.log('source URL runtime ok');
