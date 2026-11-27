/* Render an actual standalone card against finite local responses. No model or network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {randomUUID} = require('node:crypto');

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.textContent = ''; this.attributes = {};
    this.classList = {add() {}}; this.listeners = {};
  }
  append(...children) {
    for (const child of children) {
      if (child instanceof Element) {
        if (child.parent) child.parent.children = child.parent.children.filter(e => e !== child);
        child.parent = this;
      }
      this.children.push(child);
    }
  }
  prepend(child) {
    this.append(child); this.children.pop(); this.children.unshift(child);
  }
  after(child) {
    this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, child);
    child.parent = this.parent;
  }
  replaceChildren(...items) { this.children = []; this.append(...items); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  get text() { return this.textContent + this.children.map(c => typeof c === 'string' ? c : c.text).join(' '); }
  *walk() {
    yield this;
    for (const c of this.children) if (c instanceof Element) yield* c.walk();
  }
}

const root = new Element('div');
root.dataset = {account:'synthetic-owner'};
const ids = {'futures-content':root};
const requests = [];
const document = {
  createElement: tag => new Element(tag),
  getElementById: id => [...root.walk()].find(e => e.id === id) || (ids[id] ??= new Element('div')),
  documentElement: {dataset:{}},
};
const scope = vm.createContext({
  URL, URLSearchParams, Date, Intl, setTimeout, clearTimeout, document,
  localStorage: {getItem: () => null}, location: {search:'?symbol=M'},
  crypto: {randomUUID}, fetch: (url, options) => new Promise(resolve => requests.push({url,options,resolve})),
});
const asset = filename => vm.runInContext(fs.readFileSync('static/js/' + filename, 'utf8'), scope);
asset('futures-brief.js');
asset('futures.js');
const tick = () => new Promise(resolve => setImmediate(resolve));
const elements = () => [...root.walk()];
const named = (tag, name) => elements().find(e => e.tag === tag && e.textContent === name);
const source = {name:'本期周报',url:'https://example.org/weekly'};
const past = {name:'上期周报',url:'https://example.org/previous'};
const period = {label:'豆粕库存',scope:'样本油厂',unit:'万吨',current:10,previous:12,change:-2,
  data_as_of:'2026-11-23',previous_date:'2026-11-16',source,previous_source:past,stale:false};
const originReport = {institution:'样本机构',title:'周报原文',published_on:'2026-11-24',
  url:'https://example.org/report',stance:'neutral',summary:'等待下周库存数据',horizon:'一周',condition:'库存企稳'};
const brief = {fundamentals:{rows:[period]},technical:{label:'短中期分歧',reason:'当前价低于短均线。'},
  institutions:{status:'available',window_days:7,items:[originReport],missing_sides:['bullish','bearish']},
  evidence_id:'1'.repeat(64),interpret_available:true};
const card = {symbol:'M',name:'豆粕',exchange:'DCE',dimensions:{},
  research_brief:brief,technicals:{values:{ema20:3200,ema60:3100},data_as_of:'2026-11-24',source}};
for (const key of ['market','positioning','rankings','basis','inventory','spot_history','spread',
  'settlements','contract_dates','contract_parameters']) card[key] = {reason:'未取得',values:null};

(async () => {
  assert.deepEqual(requests.map(r => r.url), ['/api/futures/M/card']);
  requests[0].resolve({ok:true,status:200,json:async()=>card});
  await tick();
  assert.equal(requests.length,1);
  assert.match(root.text,/基本面变化.*较上期 -2 万吨.*2026-11-23.*2026-11-16/s);
  assert.match(root.text,/短中期分歧.*查看技术指标/s);
  assert.match(root.text,/近 7 日收录的公开研报.*样本机构.*未给出单边方向.*库存企稳.*2026-11-24/s);
  assert.match(root.text,/本期研报未明确给出偏多、偏空判断/);
  assert.equal(named('a','周报原文').href,originReport.url);
  assert.equal(named('a','上期来源').href,past.url);

  const button = named('button','解读当前资料');
  const first = button.listeners.click();
  await button.listeners.click();
  assert.equal(requests.length,2);
  assert.equal(requests[1].url,'/api/futures/M/interpret');
  assert.equal(requests[1].options.headers['X-Futures-Evidence'],brief.evidence_id);
  assert.equal(requests[1].options.headers['X-Requested-With'],'XMLHttpRequest');
  assert.equal(requests[1].options.headers['X-Prism-Account'],'synthetic-owner');
  assert.equal(requests[1].options.method,'POST');
  assert.match(requests[1].options.headers['X-Request-ID'],/^[a-f0-9-]{36}$/);
  requests[1].resolve({ok:false,json:async()=>({detail:'资料已更新，请刷新页面。'})});
  await first;
  assert.match(root.text,/资料已更新，请刷新页面/);
  assert.equal(button.disabled,false);
  const second = button.listeners.click();
  assert.notEqual(requests[2].options.headers['X-Request-ID'],requests[1].options.headers['X-Request-ID']);
  requests[2].resolve({ok:true,json:async()=>({summary:'库存下降 [F1]',as_of:'2026-11-25T01:00:00Z',
    references:[{id:'F1',label:'样本油厂'}]})});
  await second;
  assert.equal(named('a','[F1] 样本油厂').href,'#brief-F1');
  assert.match(root.text,/09:00 北京时间/);
  assert.equal(button.disabled,true);

  scope.renderCard({...card,research_brief:{...brief,interpret_available:false,fundamentals:{rows:[],reason:'无两期记录'},
    institutions:{status:'stale',latest_published_on:'2026-01-01',items:[]}}});
  assert.match(root.text,/上次收录的研报发表于 2026-01-01/);
  assert.match(root.text,/无两期记录/);
  assert.equal(named('button','解读当前资料'),undefined);
  assert.equal(named('a','周报原文'),undefined);
  assert.equal(requests.length,3);
  const excerpt = '库存下降，现货价格持平。';
  const item = {...originReport, stance:'unclassified', summary:excerpt, anchor:excerpt, locator:'第 2 页'};
  const renderReports = (items, unread=[]) => scope.renderCard({...card,research_brief:{
    ...brief,institutions:{status:'available',items,unclassified_items:unread,missing_sides:[]}
  }});
  renderReports([item]);
  assert.match(root.text,/原文方向未明/);
  assert.ok(root.text.includes('“' + excerpt + '”'));
  assert.equal(named('a','周报原文 · 第 2 页').href,originReport.url);
  assert.doesNotMatch(root.text,/未给出单边方向/);
  renderReports([{...item,summary:'库存回落'}]);
  assert.equal(named('q',excerpt).textContent,excerpt);
  renderReports([], [originReport]);
  assert.match(root.text,/原文待核对/);
  assert.equal(named('a','周报原文').href,originReport.url);
  assert.doesNotMatch(root.text,/未给出单边方向|暂无可核验的当期机构研报/);
  assert.equal(requests.length,3);

  const v2Card = {
    ...card,
    archetype_label: '农牧油脂与软商品',
    roll_guard: {roll_suspect: true, roll_notice: '最新日线跳空且持仓异动，留意主力换月影响', dominant_contract: 'M2701', dominant_oi_share_pct: 64.2},
    margins: [{id: 'soy_crush_margin', name: '大豆盘面压榨毛利', status: 'collected', formula: '0.785×M + 0.185×Y - B', unit: '元/吨', data_as_of: '2026-11-24', values: {current: 142.5, change_5d: 18.2, pct_5y: 62.4, span_label: '近5年', hint: '油厂盘面榨利回暖'}}],
    causal_view: {
      archetype_label: '农牧油脂与软商品',
      role: '中游冶炼加工',
      headline: '现货贴水保护仍在，但日线短中期分歧',
      plain_explain: '样本油厂去库 2 万吨，盘面榨利温和修复。',
      seasonal_anchor: '11–3月美豆衔接期与双节育肥备货易走基差',
      drivers: ['大豆到港与样本油厂周度压榨量', '油厂豆粕物理库存与未执行合同'],
      thermometers: [{id: 'price_5y', label: '价格历史位置（近5年）', value_text: '3180 元/吨', pct: 24.5, hint: '位于近5年 24.5% 分位'}],
      tug_of_war: [{id: 'near_reality', horizon: '近端现实', bull_score: 67, bull_points: ['豆粕库存较上期去库 2 万吨'], bear_points: ['远月升水']}],
      scenarios: [{id: 'confirm_bull', title: '情景 A · 偏多驱动确认条件', if_condition: '若压榨去库延续', then_implication: '基差走强支撑盘面'}],
    },
    chain_canvas: {
      chain_id: 'oilseed_feed',
      chain_name: '油籽压榨、软商品与生猪养殖链',
      industry_category: '农牧饲渔',
      transmission_note: '进口大豆压榨产出豆粕与豆油。',
      lead_lag: {status: 'futures_leading', label: '期强股弱（期 +1.80% vs 链内A股均值 -0.20%）', detail: '商品盘面先行'},
      stages: [
        {step: 1, stage: '上游资源与原料锚', title: '豆二 / 玉米', metric: '豆二 3650', badge: '成本输入', detail: '上游大豆成本', futures: [{symbol: 'B', name: '豆二', close: 3650, change_1d_pct: 0.8, is_current: false}], equities: []},
        {step: 2, stage: '中游盘面利润阀门', title: '大豆盘面压榨毛利', metric: '142.5 元/吨', badge: '中枢均衡', detail: '同日对齐复算', futures: [], equities: []},
        {step: 3, stage: '本品种期现与库存池', title: '豆粕 · M', metric: '仓单 45559 手', badge: '中游冶炼加工', detail: '当前查看品种', futures: [{symbol: 'M', name: '豆粕', close: 3180, change_1d_pct: 1.8, is_current: true}], equities: []},
        {step: 4, stage: '下游终端与映射A股', title: '海大集团 / 牧原股份', metric: '牧原股份 -0.20%', badge: '农牧饲渔', detail: '映射农牧饲渔图谱', futures: [{symbol: 'LH', name: '生猪', close: 14800, change_1d_pct: -0.4, is_current: false}], equities: [{ticker: '002714', name: '牧原股份', role: '生猪自繁自养成本标杆', change_pct: -0.2}]},
      ],
    },
    research_brief: {
      ...brief,
      pre_interpretation: {
        summary: '预核验解读：样本油厂库存下降 [F1]',
        as_of: '2026-11-25T02:30:00Z',
        references: [{id: 'F1', label: '样本油厂（2026-11-23）'}],
      },
    },
  };
  scope.renderCard(v2Card);
  await tick();
  assert.equal(requests.length, 3, 'V2 causal view, chain canvas and pre-interpretation fire zero extra fetches');
  assert.match(root.text, /现货贴水保护仍在，但日线短中期分歧/);
  assert.match(root.text, /最新日线跳空且持仓异动/);
  assert.match(root.text, /大豆盘面压榨毛利.*142\.5 元\/吨/s);
  assert.match(root.text, /预核验解读：样本油厂库存下降/);
  assert.match(root.text, /情景 A · 偏多驱动确认条件/);
  const stage4 = elements().find(e => e.tag === 'button' && e.attributes['data-step'] === '4');
  stage4.listeners.click();
  assert.match(root.text, /牧原股份 002714/);

  document.body = new Element('body');
  document.querySelectorAll = sel => {
    if (sel.includes('futures-causal-hero')) return [elements().find(e => (e.className || '').includes('futures-causal-hero'))].filter(Boolean);
    if (sel.includes('futures-scenarios-group')) return [elements().find(e => (e.className || '').includes('futures-scenarios-group'))].filter(Boolean);
    return [];
  };
  scope.startFuturesTour();
  assert.match(document.body.text, /今日主线因果卡与 5 年分位温度计/);
  const nextTourBtn = [...document.body.walk()].find(e => e.tag === 'button' && e.textContent === '下一步 →');
  nextTourBtn.listeners.click();
  assert.match(document.body.text, /可证伪情景路标 · 一键入账自动对账/);
  const finishTourBtn = [...document.body.walk()].find(e => e.tag === 'button' && e.textContent === '完成');
  assert.ok(finishTourBtn, 'filtered steps terminate cleanly at last visible target');
  assert.equal(requests.length, 3, 'capability guide runs locally with zero extra network calls');

  console.log('Standalone futures brief, V2 causal canvas and interpretation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
