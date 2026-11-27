const assert = require('assert');
const fs = require('fs');
const vm = require('vm');

const markedModule = require('../static/lib/marked.min.js');
const context = { App: {}, marked: markedModule.marked, console, URL };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/js/app.util.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('static/js/app.chat-render.js', 'utf8'), context);
vm.runInContext(fs.readFileSync('static/js/app.reports.js', 'utf8'), context);
context.Streams = {};
vm.runInContext(fs.readFileSync('static/js/app.chat.js', 'utf8'), context);

const markdown = [
  '**最终结论**：保持观察。',
  '',
  '- 风险一',
  '- 风险二',
  '',
  '[来源](https://example.com)',
].join('\n');
const html = context.render(markdown);
assert(!html.includes('[object Object]'), html);
assert(html.includes('<strong>最终结论</strong>'), html);
assert(html.includes('<li>风险一</li>'), html);
assert(html.includes('href="https://example.com"'), html);
const reportHtml = context.App._safeReportMarkdown(markdown);
assert(!reportHtml.includes('[object Object]'), reportHtml);
assert(reportHtml.includes('<strong>最终结论</strong>'), reportHtml);
assert.strictEqual(
  context.App._reportDeliveredText('先检查数据\n\n## 报告已交付\n\n**核心结论**'),
  '## 报告已交付\n\n**核心结论**',
);
assert.strictEqual(
  context.App._reportIsIncompleteChat(
    { source: 'chat' },
    { skill: 'serenity-bottleneck-hunter' },
    '先检查数据，然后重做批量:',
    '',
  ),
  true,
);

const chatRenderer = context.ChatRender.renderContent;
context.ChatRender.renderContent = () => ({ html: '<p>聊天流内容</p>' });
assert.strictEqual(context.App._safeReportMarkdown('报告'), '<p>报告</p>');
assert.strictEqual(context.App._reportHtmlString({ html: '<p>真实报告</p>' }), '<p>真实报告</p>');
context.ChatRender.renderContent = chatRenderer;

const degradedSearch = '{"status":"ok","query":"HBM","results":[],"note":"No results found"}';
const connectionFailure = '{"ok":false,"error":"HTTPConnectionPool: Max retries exceeded"}';

context.App.convId = 'conv-1';
const deliveredText = [
  '## 报告已交付',
  '',
  '主题：AI 数据中心液冷产能和成本。 \">供应链上游瓶颈',
  '',
  '## 一句话结论',
  '',
  '优先关注具备真实订单验证的上游环节。',
  '',
  '## 5 层产业链',
  '',
  '这部分由内嵌完整报告承载。',
].join('\n');
const deliveredChunks = [
  {t:'think', c:'交付物：reports/AI数据中心液冷_v2_刷新分析报告.html'},
  {t:'text', c:deliveredText},
];
const inlineReportHtml = context.App._renderMsg(deliveredChunks, deliveredText, false, {});
assert(inlineReportHtml.includes('chat-inline-report'), inlineReportHtml);
assert(inlineReportHtml.includes('完整报告'), inlineReportHtml);
assert(inlineReportHtml.includes('sandbox="allow-scripts"'), inlineReportHtml);
assert(!inlineReportHtml.includes('allow-same-origin'), inlineReportHtml);
assert(!inlineReportHtml.includes('\">供应链'), inlineReportHtml);
assert(!inlineReportHtml.includes('这部分由内嵌完整报告承载'), inlineReportHtml);
assert(inlineReportHtml.includes('<details class="think-box"><summary>分析过程</summary>'), inlineReportHtml);
assert(context.ChatRender.renderChunk({t:'think', c:'运行中'}, {open:true}).includes(' open>'));

console.log('markdown runtime ok');
