/* Shipped read-only futures panel against finite synthetic replies; no network.
 *
 * Four required journeys:
 * 1. all four dimensions available render every value, date and source;
 * 2. a partially covered product shows the reason code instead of a zero;
 * 3. a product without data renders the four missing blocks without throwing;
 * 4. data dates and source names stay beside the numbers (including a deep link).
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const tick = () => new Promise(setImmediate);
const script = fs.readFileSync('static/js/app.hub.js', 'utf8');
function el(tag) {
  return {
    tagName: typeof tag === 'string' ? tag.toUpperCase() : undefined,
    textContent: '',
    className: '',
    style: {},
    value: '',
    hidden: false,
    disabled: false,
    children: [],
    listeners: {},
    attrs: {},
    classList: {
      list: [],
      add(name) {
        if (!this.list.includes(name)) this.list.push(name);
      },
      remove(name) {
        this.list = this.list.filter((item) => item !== name);
      },
      contains(name) {
        return this.list.includes(name);
      },
    },
    append(...items) {
      this.children.push(...items);
    },
    addEventListener(name, handler) {
      this.listeners[name] = handler;
    },
    replaceChildren(...items) {
      this.children = items;
    },
    setAttribute(name, value) {
      this.attrs[name] = String(value);
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
    },
  };
}

function text(item) {
  if (item === null || item === undefined) return '';
  if (typeof item === 'string' || typeof item === 'number') return String(item);
  if (Array.isArray(item)) return item.map(text).join('');
  let out = typeof item.textContent === 'string' ? item.textContent : '';
  if (item.children && item.children.length) out += item.children.map(text).join('');
  return out;
}

function setup(search = '') {
  const catalog = el('div');
  const rows = {};
  for (const symbol of ['CU', 'AU', 'IF', 'M']) {
    const row = el('tr');
    row.setAttribute('data-symbol', symbol);
    rows[symbol] = row;
  }
  catalog.querySelectorAll = (selector) => (selector === 'tr[data-symbol]' ? Object.values(rows) : []);
  const submit = el();
  const form = el();
  form.querySelector = () => submit;
  const els = {
    main: el(),
    notice: el(),
    futuresForm: form,
    futuresResult: el(),
    futureSymbol: el(),
    futuresResult: el(),
    futuresNotice: el(),
    futuresPanelNotice: el(),
    futuresPanelCatalog: catalog,
    futuresPanelDetail: el(),
    futuresPanelHint: el(),
    futuresPanelReload: el(),
  };
  els.futuresWorkspace = el();
  els.futuresWorkspace.querySelectorAll = () => [form];
  els.futureSymbol.options = ['RB','CU','AU','IF','M','SC','I'].map(value=>({value}));
  const requests = [];
  const context = {
    console,
    URL,
    URLSearchParams,
    Intl,
    Date,
    JSON,
    Number,
    String,
    Promise,
    Math,
    AbortController,
    Object,
    Error,
    setTimeout,
    clearTimeout,
    location: { pathname: '/prism', search, replace() {} },
    confirm: () => true,
    addEventListener() {},
    document: {
      body: { dataset: {} },
      getElementById: (id) => els[id],
      createElement: el,
      querySelectorAll: () => [],
      addEventListener() {},
    },
    fetch(path, options) {
      return new Promise((resolve, reject) => requests.push({ path, options, resolve, reject }));
    },
  };
  Object.defineProperty(context, 'localStorage', {
    get() {
      throw Error('private persistent cache forbidden');
    },
  });
  context.window = context;
  vm.createContext(context);
  vm.runInContext(script, context);
  assert.ok(context.PrismHub, 'futures hub must be exposed on the page shell');
  return { els, requests, context, rows, form, submit };
}

const respond = (request, value, ok = true) => request.resolve({ ok, json: async () => value });

async function activate(harness) {
  const opening = harness.context.PrismHub.activateFutures();
  assert.equal(harness.requests.length, 1, 'activation reads only the current account');
  assert.equal(harness.requests[0].path, '/api/me');
  respond(harness.requests[0], { user: 'synthetic-A' });
  await opening;
}

function waitFor(harness, count, label) {
  return (async () => {
    for (let index = 0; index < 20 && harness.requests.length < count; index += 1) await tick();
    assert.ok(harness.requests.length >= count, `missing request: ${label}`);
  })();
}

const collected = (dimension, label, dataAsOf, source, values) => ({
  dimension,
  label,
  status: 'collected',
  available: true,
  data_as_of: dataAsOf,
  source,
  reason_code: null,
  reason: null,
  values,
  gaps: [],
});

const unavailable = (dimension, label, reasonCode, reason) => ({
  dimension,
  label,
  status: 'unavailable',
  available: false,
  data_as_of: null,
  source: null,
  reason_code: reasonCode,
  reason,
  values: null,
  gaps: [],
});

const SINA = { name: '新浪财经', url: 'https://finance.sina.com.cn/futures/quotes/CU0.shtml' };
const SHFE_DAILY = { name: '交易所公开日报', url: 'https://www.shfe.com.cn/data/dailydata/kx/kx20261120.dat' };
const SHFE_WARRANT = { name: '上海期货交易所', url: 'https://www.shfe.com.cn/data/dailydata/ck/20261120dailyStock.dat' };
const SCI = { name: '生意社现期表', url: 'https://www.100ppi.com/basis/' };

function panel(value) {
  return Object.assign(
    {
      trading_day: '2026-11-20',
      generated_at: '2026-11-22T15:00:00+08:00',
      notice: '只读面板；每个维度独立显示数据日期与来源，缺失维度不估算、不补零。',
    },
    value,
  );
}

function fullPayload(symbol = 'CU', name = '铜') {
  return panel({
    symbol,
    name,
    exchange: 'SHFE',
    dimensions: {
      snapshot: collected('snapshot', '最新行情', '2026-11-21', SINA, {
        latest_close: 70120,
        change_pct: 0.53,
        volume: 120000,
        open_interest: 251000,
      }),
      spread: collected('spread', '跨期价差', '2026-11-20', SHFE_DAILY, {
        near_contract: 'cu2612',
        near_delivery_month: '2026-12',
        near_settlement: 70000,
        far_contract: 'cu2701',
        far_delivery_month: '2027-01',
        far_settlement: 70200,
        far_minus_near: 200,
        shape: 'contango',
        shape_label: '远月升水（远月结算价高于近月）',
        quote_unit: '元/吨',
        contract_months: [
          { contract: 'cu2612', delivery_month: '2026-12', settlement: 70000, volume: 100, open_interest: 80 },
          { contract: 'cu2701', delivery_month: '2027-01', settlement: 70200, volume: 50, open_interest: 40 },
        ],
      }),
      inventory: collected('inventory', '仓单库存', '2026-11-20', SHFE_WARRANT, {
        quantity: 17000,
        change: -250,
        unit: '吨',
        segment: '仓库仓单',
      }),
      basis: collected('basis', '现货基差', '2026-11-19', SCI, {
        spot_price: 70500,
        futures_contract: 'cu2612',
        futures_price: 70000,
        basis: 500,
        basis_direction: 'spot_premium',
        basis_label: '现货升水（基差为正）',
        source_published_basis: 480,
        quote_unit: '元/吨',
      }),
    },
  });
}

function blockOf(harness, dimension) {
  return harness.els.futuresPanelDetail.children.find(
    (child) => child && child.attrs && child.attrs['data-dimension'] === dimension,
  );
}

async function loadPanel(harness, symbol, payload, ok = true) {
  const running = harness.context.PrismHub.loadFuturesPanel(symbol);
  const request = harness.requests[harness.requests.length - 1];
  assert.equal(request.path, '/api/futures/panel/' + symbol);
  if (ok) respond(request, payload);
  else request.reject(Error('查询较频繁，请稍后再试'));
  await running;
}

async function allDimensionsRenderCompletely() {
  const harness = setup();
  await activate(harness);
  await loadPanel(harness, 'CU', fullPayload());
  const blocks = harness.els.futuresPanelDetail.children.filter(
    (child) => child && child.attrs && child.attrs['data-dimension'],
  );
  assert.equal(blocks.length, 4);
  assert.deepEqual(
    blocks.map((block) => block.attrs['data-status']),
    ['collected', 'collected', 'collected', 'collected'],
  );
  const body = text(harness.els.futuresPanelDetail);
  for (const wanted of [
    '铜 · CU',
    '最近收盘',
    '70120',
    '120000',
    '251000',
    'cu2612',
    'cu2701',
    '合约月份清单',
    '70000',
    '70200',
    '远月减近月',
    '远月升水',
    '17000',
    '-250',
    '70500',
    '500',
    '现货升水',
  ])
    assert.ok(body.includes(wanted), wanted);
  assert.ok(!body.includes('暂无数据'));
  assert.ok(harness.els.futuresPanelReload.hidden === false);

  // Selecting another product through the server-rendered catalog reads one panel.
  harness.els.futureSymbol.value = 'RB';
  const button = el('button');
  button.setAttribute('data-symbol', 'AU');
  button.parentNode = harness.rows.AU;
  harness.els.futuresPanelCatalog.listeners.click({ target: button });
  const clicked = harness.requests[harness.requests.length - 1];
  assert.equal(clicked.path, '/api/futures/panel/AU');
  respond(clicked, fullPayload('AU', '黄金'));
  await tick();
  await tick();
  assert.ok(text(harness.els.futuresPanelDetail).includes('黄金 · AU'));
  assert.ok(harness.rows.AU.classList.contains('is-selected'));
  assert.equal(harness.els.futureSymbol.value, 'AU', 'research and panel follow the same selection');
  assert.ok(!harness.rows.CU.classList.contains('is-selected'));
}

async function partialCoverageShowsReasonCodesNotZero() {
  const harness = setup();
  await activate(harness);
  const payload = panel({
    symbol: 'SC',
    name: '原油',
    exchange: 'INE',
    dimensions: {
      snapshot: collected('snapshot', '最新行情', '2026-11-21', SINA, {
        latest_close: 520,
        change_pct: -0.82,
        volume: 90000,
        open_interest: 41000,
      }),
      spread: unavailable(
        'spread',
        '跨期价差',
        'spread_unavailable:contract_months_unavailable',
        '合约日历与结算日报暂未取得。',
      ),
      inventory: unavailable('inventory', '仓单库存', 'inventory_unavailable', '未取得交易所仓单日报；不估算、不补零。'),
      basis: unavailable(
        'basis',
        '现货基差',
        'basis_unavailable:challenge_unsolved',
        '未能通过生意社安全检查，现期表暂未取得。',
      ),
    },
  });
  await loadPanel(harness, 'SC', payload);
  const detail = harness.els.futuresPanelDetail;
  assert.ok(text(detail).includes('四个维度中 1 个可用'));
  assert.equal(blockOf(harness, 'snapshot').attrs['data-status'], 'collected');
  const checks = {
    spread: 'spread_unavailable:contract_months_unavailable',
    inventory: 'inventory_unavailable',
    basis: 'basis_unavailable:challenge_unsolved',
  };
  for (const [dimension, code] of Object.entries(checks)) {
    const block = blockOf(harness, dimension);
    assert.equal(block.attrs['data-status'], 'unavailable');
    const rendered = text(block);
    assert.ok(rendered.includes('暂无数据'), dimension + ' must show 暂无数据');
    assert.ok(rendered.includes(code), dimension + ' must show ' + code);
    assert.ok(!/[0-9]/.test(rendered), dimension + ' missing values must not render digits or zero placeholders');
  }
  assert.ok(text(detail).includes('520'));
  assert.ok(!text(blockOf(harness, 'basis')).includes('505'));
}

async function noDataProductDoesNotThrow() {
  const harness = setup();
  await activate(harness);
  const payload = panel({
    symbol: 'IF',
    name: '沪深300股指期货',
    exchange: 'CFFEX',
    dimensions: {
      snapshot: unavailable('snapshot', '最新行情', 'snapshot_unavailable', '连续日线行情暂未取得。'),
      spread: unavailable(
        'spread',
        '跨期价差',
        'spread_unavailable:contract_months_not_covered',
        '该品种没有可核对的完整挂牌合约月份清单。',
      ),
      inventory: unavailable('inventory', '仓单库存', 'inventory_unavailable', '该品种未接入交易所仓单数据源。'),
      basis: unavailable('basis', '现货基差', 'basis_unavailable:symbol_not_covered', '该品种不在生意社现期表中。'),
    },
  });
  await loadPanel(harness, 'IF', payload);
  const blocks = harness.els.futuresPanelDetail.children.filter(
    (child) => child && child.attrs && child.attrs['data-dimension'],
  );
  assert.equal(blocks.length, 4);
  for (const block of blocks) {
    assert.equal(block.attrs['data-status'], 'unavailable');
    const rendered = text(block);
    assert.ok(rendered.includes('暂无数据'));
    assert.ok(rendered.includes('原因码：'));
    assert.ok(!/[0-9]/.test(rendered));
  }
  assert.ok(text(harness.els.futuresPanelDetail).includes('四个维度中 0 个可用'));

  // A failed read surfaces as a visible notice and resolves instead of throwing.
  await loadPanel(harness, 'M', null, false);
  assert.match(harness.els.futuresPanelNotice.textContent, /品种数据未完成/);
  assert.equal(harness.els.futuresPanelNotice.hidden, false);
}

async function datesAndSourcesStayBesideNumbers() {
  const harness = setup();
  await activate(harness);
  await loadPanel(harness, 'CU', fullPayload());
  const expectations = {
    snapshot: ['数据日期 2026-11-21', '新浪财经'],
    spread: ['数据日期 2026-11-20', '交易所公开日报'],
    inventory: ['数据日期 2026-11-20', '上海期货交易所'],
    basis: ['数据日期 2026-11-19', '生意社现期表'],
  };
  for (const [dimension, wanted] of Object.entries(expectations)) {
    const rendered = text(blockOf(harness, dimension));
    for (const entry of wanted) assert.ok(rendered.includes(entry), dimension + ' ' + entry);
  }
  const detailText = text(harness.els.futuresPanelDetail);
  assert.ok(detailText.includes('跨期价差交易日 2026-11-20'));
  for (const value of ['70120', '70000', '17000', '70500']) assert.ok(detailText.includes(value));

  // The deep link reads the linked symbol once after activation.
  const deep = setup('?mode=futures&symbol=AU');
  const opening = deep.context.PrismHub.activateFutures();
  assert.equal(deep.requests.length, 1);
  respond(deep.requests[0], { user: 'synthetic-A' });
  await waitFor(deep, 2, 'deep-linked panel read');
  assert.equal(deep.requests[1].path, '/api/futures/panel/AU');
  respond(deep.requests[1], fullPayload('AU', '黄金'));
  await opening;
  await tick();
  assert.ok(text(deep.els.futuresPanelDetail).includes('黄金 · AU'));
  assert.ok(deep.rows.AU.classList.contains('is-selected'));
}

async function datedComparisonStaysBoundToTheVisibleSelection() {
  const h = setup();
  const controls = {start: el('input'), end: el('input')};
  const button = el('button'), form = el('form');
  form.elements = {namedItem: name => controls[name]};
  form.querySelector = () => button;
  form.reset = () => { controls.start.value = ''; controls.end.value = ''; };
  Object.assign(h.els, {futuresCompare: el('details'), futuresCompareForm: form,
    futuresCompareNotice: el('p'), futuresCompareResult: el('div')});
  await activate(h);
  await loadPanel(h, 'M', panel({symbol:'M', name:'豆粕', exchange:'DCE', dimensions:{},
    weekly_research:{last_observed:null, historical:false, reading:['本周仅公布压榨计划。'],
      rows:[{period:'2026-11-20', scope:'样本油厂', label:'预估压榨', value:205, unit:'万吨',
        known:'2026-11-13T17:56', url:'https://example.org/meal-plan'}], sources:[]}}));
  const forecast = text(h.els.futuresPanelDetail);
  assert.match(forecast, /产业周报 · 待披露实绩.*预估压榨 205 万吨/s);
  assert.doesNotMatch(forecast, /周报 · null/);
  const rb = fullPayload('RB', '螺纹钢');
  rb.dimensions.snapshot.values.closes = [];
  rb.weekly_research = {last_observed:'2026-11-19',historical:true,reading:['等待本周终端采购'],rows:[
    {period:'2026-11-19',scope:'35城',label:'社会库存',value:310.22,unit:'万吨',known:'2026-11-19T14:16',url:'https://example.org/weekly'}
  ],sources:['https://example.org/weekly']};
  await loadPanel(h, 'RB', rb);
  assert.match(text(h.els.futuresPanelDetail),/历史周报 · 2026-11-19.*等待本周终端采购.*35城.*310.22 万吨/s);
  const box = h.els.futuresCompare;
  box.open = true;
  const start = controls.start.value = '2026-11-19', end = controls.end.value = '2026-11-20';
  const submit = () => form.onsubmit({currentTarget: form, preventDefault() {}});
  const run = submit();
  const response = {symbol:'RB', start, end, unit:'元/吨', changes:{spot:0,main:8,basis:-8,rolled:true},
    basis:[{date:start,contract:'RB2701',spot:3010,main:3000,basis:10},{date:end,contract:'RB2705',spot:3010,main:3008,basis:2}],
    curves:[{date:start,near:3000,far:3020,spread:20},{date:end,near:3010,far:3015,spread:5}],
    pair:['RB2701','RB2705'],spread_change:-15};
  assert.equal(h.requests.at(-1).path,'/api/futures/compare/RB?start='+start+'&end='+end);
  respond(h.requests.at(-1),response);
  await run;
  assert.match(text(h.els.futuresCompareResult), /主力换月：RB2701 → RB2705/);
  assert.match(text(h.els.futuresCompareResult), /两日价差变化 -15/);
  assert.match(h.context.PrismHub.futuresObservationContext('RB'), /2026-11-19 与 2026-11-20.*RB2701.*RB2705/);
  box.open = false;
  assert.doesNotMatch(h.context.PrismHub.futuresObservationContext('RB'), /双日比较/);
  box.open = true;
  const late = submit(), pending = h.requests.at(-1);
  controls.start.value = '2026-11-18'; form.oninput();
  assert.match(h.context.PrismHub.futuresObservationContext('RB'), /尚无本次比较结果/);
  await loadPanel(h, 'CU', fullPayload());
  respond(pending,response);await late;
  assert.equal(box.hidden,true);
  assert.equal(text(h.els.futuresCompareResult),'');
  assert.doesNotMatch(h.context.PrismHub.futuresObservationContext('CU'), /双日比较/);
}

(async () => {
  await datedComparisonStaysBoundToTheVisibleSelection();
  const race=setup();await activate(race);
  const late=race.context.PrismHub.loadFuturesPanel('CU');
  const oldRequest=race.requests.at(-1);
  const fresh=race.context.PrismHub.loadFuturesPanel('AU');
  respond(race.requests.at(-1),fullPayload('AU','黄金'));await fresh;
  respond(oldRequest,fullPayload());await late;
  assert.equal(race.els.futureSymbol.value,'AU');
  assert.ok(text(race.els.futuresPanelDetail).includes('黄金 · AU'));
  assert.equal(race.context.PrismHub.futuresObservationContext('CU'),'');
  assert.match(race.context.PrismHub.futuresObservationContext('AU'),/2026-11-20/);
  await allDimensionsRenderCompletely();
  await partialCoverageShowsReasonCodesNotZero();
  await noDataProductDoesNotThrow();
  await datesAndSourcesStayBesideNumbers();
  console.log('futures panel ok: 6 scenarios');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
