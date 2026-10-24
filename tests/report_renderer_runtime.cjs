// Synthetic saved documents only: no browser, account, network, or model calls.
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const markedModule = require('../static/lib/marked.min.js');
const panel = { innerHTML: '' };
const context = {
  App: {}, marked: markedModule.marked, console, URL,
  document: { getElementById: id => id === 'reportsPanelInner' ? panel : null },
};
context.window = context;
vm.createContext(context);
for (const file of ['app.util.js', 'app.chat-render.js', 'app.reports.js']) {
  vm.runInContext(fs.readFileSync('static/js/' + file, 'utf8'), context);
}
const app = context.App;
const longUrl = 'https://example.com/research/' + 'a'.repeat(300);
const markdown = [
  '## 报告已交付', '',
  '> 历史来源观点，尚待核验。',
  '>',
  '> - 第一项引用', '',
  '2026年收入增幅 > 10%，历史值为3.5。', '',
  '| 环节 | 来源 |', '| --- | --- |',
  '| 合成行业 | [' + longUrl + '](' + longUrl + ') |', '',
  '```text', 'x > y', '```', '',
  '`' + 'unbroken_'.repeat(40) + '`',
].join('\n');
const html = app._safeReportMarkdown(markdown);
assert(html.includes('<h2>报告已交付</h2>'), html);
assert(html.includes('<blockquote>'), html);
assert(html.includes('<li>第一项引用</li>'), html);
assert(!html.includes('md-literal-quote'), html);
assert(html.includes('2026年收入增幅 &gt; 10%'), html);
assert(html.includes('<code class="language-text">x &gt; y'), html);
assert(html.includes('class="reports-lib-table-scroll" role="region"'), html);
assert(html.includes('tabindex="0"><table>'), html);
assert(html.includes('href="' + longUrl + '"'), html);
// A report render must not change the separate streaming/chat quote policy.
assert(!context.render('> 聊天文本').includes('<blockquote>'));

const hostile = app._safeReportMarkdown([
  '[不安全链接](javascript:alert%281%29)', '',
  '<script>window.reportExecuted=true</script>', '',
  '<img src="https://example.com/image.png" onerror="alert(1)">',
  '<iframe src="https://example.com/"></iframe>',
].join('\n'));
assert(!/<(?:script|img|iframe)\b/i.test(hostile), hostile);
assert(!/href="javascript:/i.test(hostile), hostile);
assert(hostile.includes('&lt;script&gt;'), hostile);

assert.strictEqual(
  app._reportSummaryText('> ## 历史观点\n\n2026年收入增长30%。\n3.5倍是历史估值。\n1. 只作合成示例。'),
  '历史观点 2026年收入增长30%。 3.5倍是历史估值。 只作合成示例。',
);
app._savedReports = [{ id: 'synthetic', title: '合成历史报告', summary: '> **2026年**收入增长30%。' }];
app._renderReportLibraryHome();
assert(panel.innerHTML.includes('<p>2026年收入增长30%。</p>'), panel.innerHTML);

const saved = {
  id: 'synthetic', source: 'chat', title: '合成历史报告', created_at: 1704067200,
  report: {
    status: 'complete', skill: 'synthetic-old-skill', final_decision: markdown,
    references: [{
      source_kind: 'external_web', evidence_status: 'retrieved',
      title: '合成来源', url: 'https://example.com/research',
      published_at: null, retrieved_at: null,
    }],
  },
};
const original = JSON.stringify(saved);
app._renderSavedReportDetail(saved);
assert.strictEqual(JSON.stringify(saved), original, 'Rendering must not rewrite the saved report.');
assert(panel.innerHTML.includes('<h4>研究观点</h4>'), panel.innerHTML);
assert(panel.innerHTML.includes('<blockquote>'), panel.innerHTML);
assert(panel.innerHTML.includes('保存于 ' + context.ChatRender.formatTime(saved.created_at)), panel.innerHTML);
assert(panel.innerHTML.includes('历史报告未记录可靠的研究时点；数据截至未知。'), panel.innerHTML);
assert(panel.innerHTML.includes('已采集 · 未核验'), panel.innerHTML);
assert(panel.innerHTML.includes('发布：未提供'), panel.innerHTML);
assert(!panel.innerHTML.includes('本轮研究截至'), panel.innerHTML);

app._renderSavedReportDetail({
  id: 'unfinished', source: 'chat', created_at: saved.created_at,
  report: { status: 'incomplete', skill: 'synthetic-old-skill', final_decision: '> 未完成的研究过程' },
});
assert(panel.innerHTML.includes('研究未完成'), panel.innerHTML);
assert(panel.innerHTML.includes('<details class="reports-lib-section reports-lib-process">'), panel.innerHTML);
assert(!panel.innerHTML.includes('<h4>研究观点</h4>'), panel.innerHTML);

// Missing parser or sanitizer degrades to escaped text, never raw report HTML.
const parser = context.marked;
context.marked = undefined;
assert.strictEqual(app._safeReportMarkdown('<img src=x>'), '<p>&lt;img src=x&gt;</p>');
context.marked = parser;
context.sanitizeHtml = undefined;
assert.strictEqual(app._safeReportMarkdown('<img src=x>'), '<p>&lt;img src=x&gt;</p>');
console.log('report renderer runtime ok');
