'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const markedModule = require('../static/lib/marked.min.js');
const context = { App: {}, marked: markedModule.marked, console, URL, document: { addEventListener() {} } };
context.window = context;
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/app.util.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/app.chat-render.js'), 'utf8'), context);
vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/js/app.research.js'), 'utf8'), context);
const app = context.App;

const narrativeText = '2025 年产能为 120 万吨✅，同比增长 12.5%◐。（来源：示例公告，2026-03-18）（二手来源）净利率约 9%≈，市占率为 【待核验：30.2%】。';
const evidence = {
  status: 'ok',
  total: 4,
  counts: { verified_source: 1, secondhand: 1, inferred: 1, unverified: 1 },
  coverage: { verified_claim_ratio: 0.25, threshold: 0.9, release_line: '≥90%', reached: false },
  missing: { source: 3, published_at: 3, scope: 0 },
  note: '证据清单只记录 claim 字段与支持资料，不构成独立事实认证。',
  rows: [
    {
      claim_id: 'c1',
      metric: '设计产能',
      value: '120',
      unit: '万吨',
      statement: '2025 年产能 120 万吨',
      source: '示例公告',
      source_url: 'https://example.test/report',
      published_at: '2026-03-18',
      scope: '2025 年合并报表',
      verification: 'verified_source',
    },
    {
      claim_id: 'c2',
      value: '12.5',
      unit: '%',
      statement: '同比增长 12.5%',
      source: null,
      source_url: null,
      published_at: null,
      scope: null,
      verification: 'secondhand',
    },
    {
      claim_id: 'c3',
      value: '9',
      unit: '%',
      statement: '净利率推算',
      source: null,
      source_url: null,
      published_at: '2026-03-01',
      scope: null,
      verification: 'inferred',
    },
    {
      claim_id: 'c4',
      value: '18',
      unit: '%',
      statement: '市占率待核验',
      source: null,
      source_url: null,
      published_at: null,
      scope: null,
      verification: 'unverified',
    },
  ],
};
const revision = {
  narrative: 'RAW-ONLY 原始报告文本',
  verification_render: { status: 'ok', narrative: { status: 'rendered', changed: true, text: narrativeText }, evidence },
};

const narrativeHtml = app._researchNarrativeHTML(revision);
assert.match(narrativeHtml, /research-report-narrative/);
assert.match(narrativeHtml, /<span class="research-level research-level-verified"[^>]*>✅<\/span>/);
assert.match(narrativeHtml, /<span class="research-level research-level-secondhand"[^>]*>◐<\/span>/);
assert.match(narrativeHtml, /<span class="research-level research-level-inferred"[^>]*>≈<\/span>/);
assert.match(narrativeHtml, /<span class="research-level research-level-unverified"[^>]*>【待核验：30\.2%】<\/span>/);
assert.doesNotMatch(narrativeHtml, /RAW-ONLY/);
assert.doesNotMatch(narrativeHtml, /\sstyle=/);

const evidenceHtml = app._researchEvidenceHTML(revision);
assert.match(evidenceHtml, /research-evidence-section/);
assert.match(evidenceHtml, /证据清单/);
assert.match(evidenceHtml, /共 4 条已登记 claim/);
assert.match(evidenceHtml, /核验覆盖率 25\.0%（发布线 ≥90%，未达到）/);
assert.match(evidenceHtml, /缺来源 3 条 · 缺发布日期 3 条 · 缺口径 0 条/);
assert.match(evidenceHtml, /href="https:\/\/example\.test\/report"/);
assert.match(evidenceHtml, /research-evidence-value/);
assert.match(evidenceHtml, /research-level-unverified/);
assert.ok((evidenceHtml.match(/未提供/g) || []).length >= 3, evidenceHtml);

const noView = app._researchNarrativeHTML({ narrative: '原始报告 120 万吨。' });
assert.match(noView, /本版本未保存核验标注/);
assert.match(noView, /原始报告 120 万吨/);
const degradedView = app._researchNarrativeHTML({
  narrative: '原始报告 120 万吨。',
  verification_render: { status: 'degraded', narrative: { status: 'original', text: '原始报告 120 万吨。' } },
});
assert.match(degradedView, /核验标注生成失败/);
assert.match(degradedView, /原始报告 120 万吨/);
assert.strictEqual(app._researchNarrativeHTML({}), '');
assert.match(
  app._researchEvidenceHTML({ verification_render: { status: 'degraded', evidence: { status: 'unavailable' } } }),
  /证据清单暂不可用/,
);
assert.match(app._researchEvidenceHTML({}), /证据清单暂不可用/);

const scriptRevision = {
  narrative: '',
  verification_render: {
    status: 'ok',
    narrative: { status: 'rendered', text: '报告里出现 <script>alert(1)</script> 和 <img src=x onerror=steal()> 字样。' },
  },
};
const scriptHtml = app._researchNarrativeHTML(scriptRevision);
assert.doesNotMatch(scriptHtml, /<script/);
assert.match(scriptHtml, /&lt;script/);
assert.doesNotMatch(scriptHtml, /<img/);
assert.match(scriptHtml, /&lt;img/);

const xssEvidence = {
  status: 'ok',
  total: 1,
  counts: { verified_source: 0, secondhand: 0, inferred: 0, unverified: 1 },
  coverage: { verified_claim_ratio: 0, threshold: 0.9, release_line: '≥90%', reached: false },
  missing: { source: 0, published_at: 0, scope: 0 },
  rows: [
    {
      value: '<script>steal()</script>',
      unit: null,
      statement: '<img src=x onerror=steal()>',
      source: '<b>来源</b>',
      source_url: 'javascript:alert(1)',
      published_at: 'x',
      scope: 'y',
      verification: 'unverified',
    },
  ],
};
const xssHtml = app._researchEvidenceHTML({ verification_render: { status: 'ok', evidence: xssEvidence } });
assert.doesNotMatch(xssHtml, /<script/);
assert.match(xssHtml, /&lt;script/);
assert.doesNotMatch(xssHtml, /<img/);
assert.match(xssHtml, /&lt;img/);
assert.doesNotMatch(xssHtml, /javascript:/);
assert.match(xssHtml, /&lt;b&gt;/);

app._researchBundle = {};
const claimHtml = app._researchClaimsHTML([
  { claim_id: 'a', kind: 'fact', statement: '合成数字', value: '120', unit: '万吨', verification: 'verified_source' },
]);
assert.match(claimHtml, /research-level-verified/);
const unknownLevel = app._researchClaimsHTML([
  { claim_id: 'b', kind: 'fact', statement: '未知等级', value: '1', unit: '%', verification: 'trusted' },
]);
assert.match(unknownLevel, /research-level-unverified/);
assert.match(app._researchLevelTag('verified_source'), /class="research-level research-level-verified"/);
assert.match(app._researchLevelTag('bogus'), /research-level-unverified/);
assert.doesNotMatch(app._researchLevelTag('secondhand'), /style=/);

process.stdout.write('research render runtime ok\n');
