'use strict';

const $ = id => document.getElementById(id);
const root = $('futures-content');
const status = $('futures-status');
const retry = $('futures-retry');
const symbol = new URLSearchParams(location.search).get('symbol')?.trim().toUpperCase();
const stateLabels = {
  collected: '有数据', partial: '部分可用', unknown: '待核验', unavailable: '不可用',
  recent: '近期', stale: '较早',
};
const fieldLabels = {
  open: '开盘', high: '最高', low: '最低', close: '收盘', settlement: '结算价',
  volume: '成交量（手）', open_interest: '持仓量（手）',
  listed_expiry_date: '最后交易日', delivery_start: '交割开始', delivery_end: '交割结束',
  upper_limit_pct: '涨停（%）', lower_limit_pct: '跌停（%）',
  spec_long_margin_pct: '投机多头保证金（%）', spec_short_margin_pct: '投机空头保证金（%）',
};

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value != null) element.textContent = value;
  if (className) element.className = className;
  return element;
}

function productLink(code) {
  return '/futures?symbol=' + encodeURIComponent(code);
}

function formatSignedPct(val) {
  if (typeof val !== 'number' || !Number.isFinite(val)) return null;
  return (val > 0 ? '+' : '') + val.toFixed(2) + '%';
}

function pctZone(pct) {
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return 'mid';
  if (pct >= 75) return 'high';
  if (pct <= 25) return 'low';
  return 'mid';
}

function computeEma(values, span) {
  if (!values.length) return [];
  const alpha = 2 / (span + 1);
  const out = [values[0]];
  for (let i = 1; i < values.length; i += 1) {
    out.push(values[i] * alpha + out[i - 1] * (1 - alpha));
  }
  return out;
}

function renderMarketRadar(radar, quantProvider) {
  if (!radar) return null;
  const highs = radar.high_extremes || [];
  const lows = radar.low_extremes || [];
  const margins = radar.margin_extremes || [];
  const rolls = radar.roll_alerts || [];
  const rec = radar.reconciled_summary || {};
  const hasSignals = highs.length || lows.length || margins.length || rolls.length || rec.total_auto_reconciled > 0;
  const wrap = node('section', null, 'futures-radar');
  wrap.setAttribute('aria-label', '全市场极值与自动对账雷达');
  const head = node('div', null, 'futures-radar-head');
  const titleWrap = node('div', null, 'futures-radar-title');
  titleWrap.append(
    node('h2', '全市场异动与对账雷达'),
    node('span', radar.observed_products
      ? '已核验 ' + radar.observed_products + ' / 46 个活跃品种连续序列'
      : '正在按序建立历史分位与跨品种利润缓存', 'futures-meta')
  );
  head.append(titleWrap);
  const badges = node('div', null, 'futures-radar-badges');
  if (rec.total_auto_reconciled > 0) {
    const recBadge = node('span', '自动对账 ' + rec.total_auto_reconciled + ' 条（支持 ' + (rec.supported_count || 0) + '）', 'futures-pill');
    recBadge.setAttribute('data-tone', 'accent');
    badges.append(recBadge);
  }
  if (quantProvider?.configured) {
    const qBadge = node('span', 'Tushare Pro 量化增强已就绪', 'futures-pill');
    qBadge.setAttribute('data-tone', 'accent');
    badges.append(qBadge);
  }
  head.append(badges);
  wrap.append(head);

  if (!hasSignals) {
    return wrap;
  }

  const grid = node('div', null, 'futures-radar-grid');
  const addBucket = (title, tone, items, renderItem) => {
    if (!items.length) return;
    const card = node('div', null, 'futures-radar-bucket');
    card.setAttribute('data-tone', tone);
    card.append(node('h3', title));
    const list = node('div', null, 'futures-radar-pills');
    for (const item of items) list.append(renderItem(item));
    card.append(list);
    grid.append(card);
  };

  addBucket('历史高位拥挤区（≥82%分位）', 'high', highs, item => {
    const a = node('a', item.name + ' ' + item.symbol + ' · ' + Math.round(item.pct_5y) + '%', 'futures-radar-chip');
    a.href = productLink(item.symbol);
    a.title = item.span_label + ' ' + item.pct_5y + '% 分位 · 收盘 ' + item.close + '（' + item.data_as_of + '）';
    return a;
  });

  addBucket('历史低位安全垫（≤18%分位）', 'low', lows, item => {
    const a = node('a', item.name + ' ' + item.symbol + ' · ' + Math.round(item.pct_5y) + '%', 'futures-radar-chip');
    a.href = productLink(item.symbol);
    a.title = item.span_label + ' ' + item.pct_5y + '% 分位 · 收盘 ' + item.close + '（' + item.data_as_of + '）';
    return a;
  });

  addBucket('产业链利润/比价极值', 'margin', margins, item => {
    const a = node('a', item.name + ' · ' + Math.round(item.pct) + '%分位', 'futures-radar-chip');
    a.href = productLink(item.primary_symbol);
    a.title = item.current + ' ' + item.unit + ' · ' + item.hint + '（' + item.data_as_of + '）';
    return a;
  });

  addBucket('连续序列换月跳空提醒', 'warn', rolls, item => {
    const a = node('a', item.name + ' ' + item.symbol + ' · 疑似换月', 'futures-radar-chip');
    a.href = productLink(item.symbol);
    a.title = item.notice;
    return a;
  });

  wrap.append(grid);
  return wrap;
}

function renderUniverse(data) {
  const overview = node('div', null, 'futures-overview');
  const abbreviatedDimensions = {
    market: '行情', technicals: '技术', positioning: '资金', fundamentals: '基本', freshness: '时效',
  };
  let activeArchetype = 'all';

  const radarEl = renderMarketRadar(data.radar, data.quant_provider);
  if (radarEl) overview.append(radarEl);

  if (Array.isArray(data.archetypes) && data.archetypes.length) {
    const filterBar = node('div', null, 'futures-archetype-bar');
    filterBar.setAttribute('role', 'toolbar');
    filterBar.setAttribute('aria-label', '按定价原型筛选品种');
    const allItems = [{key: 'all', label: '全部品种 (' + data.products.length + ')'}, ...data.archetypes];
    const buttons = [];
    for (const item of allItems) {
      const btn = node('button', item.label, 'futures-archetype-chip');
      btn.type = 'button';
      btn.setAttribute('aria-pressed', item.key === 'all' ? 'true' : 'false');
      btn.addEventListener('click', () => {
        activeArchetype = item.key;
        for (const b of buttons) b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
        draw();
      });
      buttons.push(btn);
      filterBar.append(btn);
    }
    overview.append(filterBar);
  }

  const stateKey = node('div', null, 'futures-state-key');
  for (const [statusName, label] of [
    ['collected', '有数据 / 近期'], ['partial', '部分可用 / 较早'], ['unknown', '待核验'],
    ['unavailable', '不可用'],
  ]) {
    const item = node('span', null, 'futures-key-item');
    const mark = node('span', null, 'futures-state');
    mark.setAttribute('data-status', statusName);
    mark.setAttribute('aria-hidden', 'true');
    item.append(mark, node('span', label));
    stateKey.append(item);
  }
  const groups = node('div', null, 'futures-groups');
  overview.append(stateKey, groups);

  function draw() {
    const term = $('futures-search').value.trim().toUpperCase();
    const products = data.products.filter(product => {
      if (activeArchetype !== 'all' && product.archetype !== activeArchetype) return false;
      return [product.name, product.symbol, product.exchange, product.exchange_name, product.archetype_label || '']
        .join(' ').toUpperCase().includes(term);
    });
    const exchanges = new Map();
    for (const product of products) {
      const key = product.exchange_name || product.exchange;
      if (!exchanges.has(key)) exchanges.set(key, []);
      exchanges.get(key).push(product);
    }
    const sections = [];
    for (const [exchange, items] of exchanges) {
      const section = node('section', null, 'futures-group');
      const heading = node('div', null, 'futures-group-heading');
      heading.append(node('h2', exchange), node('span', String(items.length), 'futures-group-count'));
      const grid = node('div', null, 'futures-grid');
      for (const product of items) {
        const link = node('a', null, 'futures-tile');
        link.href = productLink(product.symbol);
        const identity = node('div', null, 'futures-tile-heading');
        const name = node('div', null, 'futures-tile-identity');
        const metaText = product.archetype_label ? product.symbol + ' · ' + product.archetype_label : product.symbol;
        name.append(node('h3', product.name), node('p', metaText, 'futures-meta'));
        const arrow = node('span', '↗', 'futures-tile-arrow');
        arrow.setAttribute('aria-hidden', 'true');
        identity.append(name, arrow);
        link.append(identity);

        if (product.quote_preview && product.quote_preview.close != null) {
          const qp = product.quote_preview;
          const quoteRow = node('div', null, 'futures-tile-quote');
          quoteRow.append(node('strong', String(qp.close)));
          const chgTxt = formatSignedPct(qp.change_pct);
          if (chgTxt) {
            const chg = node('span', chgTxt, 'futures-tile-chg');
            chg.setAttribute('data-dir', qp.change_pct > 0 ? 'up' : qp.change_pct < 0 ? 'down' : 'flat');
            quoteRow.append(chg);
          }
          link.append(quoteRow);
          if (typeof qp.pct_5y === 'number' && Number.isFinite(qp.pct_5y)) {
            const mini = node('div', null, 'futures-tile-pct');
            const barTrack = node('span', null, 'futures-tile-pct-track');
            const barFill = node('i');
            barFill.setAttribute('data-zone', pctZone(qp.pct_5y));
            barFill.setAttribute('style', 'width:' + Math.max(4, Math.min(100, qp.pct_5y)) + '%');
            barTrack.append(barFill);
            mini.append(barTrack, node('em', Math.round(qp.pct_5y) + '%分位'));
            link.append(mini);
          }
        }

        const states = node('div', null, 'futures-states');
        const descriptions = [];
        for (const {key, label} of data.dimensions) {
          const observation = product.dimensions[key];
          const detail = [observation.data_as_of, observation.reason].filter(Boolean).join(' · ');
          const description = label + '：' + (stateLabels[observation.status] || observation.status) +
            (detail ? '（' + detail + '）' : '');
          descriptions.push(description);
          const state = node('span', abbreviatedDimensions[key] || label, 'futures-state');
          state.setAttribute('data-status', observation.status);
          state.title = description;
          state.setAttribute('aria-hidden', 'true');
          states.append(state);
        }
        link.setAttribute('aria-label', product.name + '，' + product.symbol + '。' + descriptions.join('；'));
        link.append(states);
        grid.append(link);
      }
      section.append(heading, grid);
      sections.push(section);
    }
    groups.replaceChildren(...sections);
    stateKey.hidden = products.length === 0;
    status.textContent = products.length ? (data.refreshing ? '正在分批核验数据…' : '') : '没有符合条件的品种。';
  }
  $('futures-search-label').hidden = false;
  $('futures-search').oninput = draw;
  root.replaceChildren(overview);
  draw();
}

function sourceLink(source) {
  if (!source?.url) return node('span', source?.name ?? '来源未提供');
  try {
    const url = new URL(source.url);
    if (url.protocol === 'https:' || url.protocol === 'http:') {
      const link = node('a', source.name);
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      return link;
    }
  } catch (_) {
    return node('span', source.name);
  }
  return node('span', source.name);
}

function value(number, unit) {
  return number == null ? '未提供' : String(number) + (unit ? ' ' + unit : '');
}

function facts(section, fields) {
  const list = node('dl', null, 'futures-facts');
  for (const [label, reading, unit] of fields) {
    list.append(node('dt', label), node('dd', value(reading, unit)));
  }
  section.append(list);
}

function table(section, firstHeader, firstKey, fields, records, label) {
  const ariaLabel = label || firstHeader + '明细表';
  const scroll = node('div', null, 'futures-table-scroll');
  scroll.tabIndex = 0;
  scroll.setAttribute('role', 'region');
  scroll.setAttribute('aria-label', ariaLabel);
  const tbl = node('table');
  const head = tbl.createTHead().insertRow();
  for (const colLabel of [firstHeader, ...fields.map(key => fieldLabels[key] || '现货价')]) {
    const cell = node('th', colLabel);
    cell.scope = 'col';
    head.append(cell);
  }
  const body = tbl.createTBody();
  for (const record of records) {
    const row = body.insertRow();
    const first = node('th', record[firstKey]);
    first.scope = 'row';
    row.append(first);
    for (const key of fields) row.insertCell().textContent = value(record[key]);
  }
  scroll.append(tbl);
  section.append(scroll);
}

function observation(title, item, render, heading = 'h3') {
  const section = node('section', null, 'futures-card');
  section.append(node(heading, title));
  if (item.values) {
    const meta = node('p', null, 'futures-meta');
    meta.append(item.data_as_of ?? '日期未提供', ' · ', sourceLink(item.source));
    section.append(meta);
    render(section, item.values);
    if (item.reason) section.append(node('p', item.reason, 'futures-meta'));
    for (const gap of item.gaps ?? []) section.append(node('p', gap, 'futures-meta'));
  } else {
    section.classList.add('futures-card-empty');
    section.append(node('p', item.reason ?? '暂未取得数据。', 'futures-meta'));
  }
  return section;
}

function renderCausalHero(card) {
  const cv = card.causal_view;
  if (!cv) return null;
  const hero = node('section', null, 'futures-card futures-causal-hero');
  hero.setAttribute('aria-label', '期现核心结论与历史分位坐标');

  const tags = node('div', null, 'futures-causal-tags');
  if (cv.archetype_label) {
    const arch = node('span', cv.archetype_label, 'futures-pill');
    arch.setAttribute('data-tone', 'accent');
    tags.append(arch);
  }
  if (cv.role) tags.append(node('span', '产业链位置 · ' + cv.role, 'futures-pill'));
  if (card.roll_guard?.dominant_contract && card.roll_guard.dominant_oi_share_pct != null) {
    tags.append(node('span',
      '主力合约 ' + card.roll_guard.dominant_contract + '（持仓占比 ' + card.roll_guard.dominant_oi_share_pct + '%）',
      'futures-pill'));
  }
  hero.append(tags);

  if (card.roll_guard?.roll_suspect && card.roll_guard.roll_notice) {
    const alert = node('div', card.roll_guard.roll_notice, 'futures-roll-alert');
    alert.setAttribute('role', 'note');
    hero.append(alert);
  }

  hero.append(node('h2', cv.headline, 'futures-causal-headline'));
  if (cv.plain_explain) hero.append(node('p', cv.plain_explain, 'futures-causal-explain'));
  if (cv.seasonal_anchor) {
    hero.append(node('p', '季节性日历锚：' + cv.seasonal_anchor, 'futures-meta futures-seasonal-note'));
  }

  if (Array.isArray(cv.thermometers) && cv.thermometers.length) {
    const thermoGrid = node('div', null, 'futures-thermometers');
    for (const th of cv.thermometers) {
      const box = node('div', null, 'futures-thermo');
      const top = node('div', null, 'futures-thermo-head');
      top.append(node('span', th.label, 'futures-thermo-label'), node('strong', th.value_text));
      const track = node('div', null, 'futures-thermo-track');
      const fill = node('span', null, 'futures-thermo-fill');
      const pct = Math.max(0, Math.min(100, Number(th.pct) || 0));
      fill.setAttribute('data-zone', pctZone(pct));
      fill.setAttribute('style', 'width:' + pct + '%');
      const pin = node('i', null, 'futures-thermo-pin');
      pin.setAttribute('style', 'left:' + pct + '%');
      track.append(fill, pin);
      const scale = node('div', null, 'futures-thermo-scale');
      scale.append(node('span', '0% 低位'), node('em', pct + '% 分位'), node('span', '100% 高位'));
      box.append(top, track, scale);
      if (th.hint) box.append(node('p', th.hint, 'futures-meta'));
      thermoGrid.append(box);
    }
    hero.append(thermoGrid);
  }

  if (Array.isArray(cv.tug_of_war) && cv.tug_of_war.length) {
    const tugGrid = node('div', null, 'futures-tug-grid');
    for (const side of cv.tug_of_war) {
      const cardEl = node('div', null, 'futures-tug-card');
      const head = node('div', null, 'futures-tug-head');
      head.append(node('h3', side.horizon));
      if (side.bull_score != null) {
        const bearScore = 100 - side.bull_score;
        head.append(node('span', '多 ' + side.bull_score + '% : 空 ' + bearScore + '%', 'futures-meta'));
      }
      cardEl.append(head);
      if (side.bull_score != null) {
        const bar = node('div', null, 'futures-tug-bar');
        const bullFill = node('span', null, 'futures-tug-bull');
        bullFill.setAttribute('style', 'width:' + side.bull_score + '%');
        const bearFill = node('span', null, 'futures-tug-bear');
        bearFill.setAttribute('style', 'width:' + (100 - side.bull_score) + '%');
        bar.append(bullFill, bearFill);
        cardEl.append(bar);
      }
      const points = node('ul', null, 'futures-tug-points');
      for (const p of side.bull_points || []) {
        const li = node('li', '＋ ' + p);
        li.setAttribute('data-side', 'bull');
        points.append(li);
      }
      for (const p of side.bear_points || []) {
        const li = node('li', '－ ' + p);
        li.setAttribute('data-side', 'bear');
        points.append(li);
      }
      if ((side.bull_points?.length || 0) + (side.bear_points?.length || 0) > 0) {
        cardEl.append(points);
      } else if (side.empty_reason) {
        cardEl.append(node('p', side.empty_reason, 'futures-meta'));
      }
      tugGrid.append(cardEl);
    }
    hero.append(tugGrid);
  }

  return hero;
}

function renderChainCanvas(card) {
  const canvas = card.chain_canvas;
  if (!canvas || !Array.isArray(canvas.stages) || !canvas.stages.length) return null;
  const wrap = node('section', null, 'futures-card futures-chain-canvas');
  wrap.setAttribute('aria-label', '期现股全产业链传导流向画布');

  const head = node('div', null, 'futures-chain-head');
  const titleBox = node('div');
  titleBox.append(
    node('h2', '期 · 现 · 股全产业链传导画布 · ' + canvas.chain_name),
    node('p', canvas.transmission_note, 'futures-meta')
  );
  const actions = node('div', null, 'futures-chain-actions');
  if (canvas.lead_lag?.label) {
    const ll = node('span', canvas.lead_lag.label, 'futures-pill');
    ll.setAttribute('data-leadlag', canvas.lead_lag.status || 'aligned');
    ll.title = canvas.lead_lag.detail || '';
    actions.append(ll);
  }
  if (canvas.industry_category) {
    const indLink = node('a', '打开「' + canvas.industry_category + '」产业图谱 ↗', 'futures-chain-link');
    indLink.href = '/prism?mode=industry&category=' + encodeURIComponent(canvas.industry_category);
    actions.append(indLink);
  }
  const warmBtn = node('button', '同步全链行情', 'futures-chain-warm');
  warmBtn.type = 'button';
  const warmStatus = node('span', null, 'futures-meta');
  warmBtn.addEventListener('click', async () => {
    if (warmBtn.disabled) return;
    warmBtn.disabled = true;
    warmBtn.textContent = '正在同步同链品种…';
    try {
      const resp = await fetch('/api/futures/' + encodeURIComponent(card.symbol) + '/warm-chain', {
        method: 'POST',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: {'X-Requested-With': 'XMLHttpRequest'},
      });
      if (!resp.ok) throw new Error('同步未完成，请稍后重试。');
      const updated = await resp.json();
      renderCard(updated);
    } catch (err) {
      warmStatus.textContent = err instanceof Error ? err.message : '同步未完成';
      warmBtn.disabled = false;
      warmBtn.textContent = '同步全链行情';
    }
  });
  actions.append(warmBtn, warmStatus);
  head.append(titleBox, actions);
  wrap.append(head);

  const stageGrid = node('div', null, 'futures-chain-stages');
  const inspector = node('div', null, 'futures-chain-inspector');
  const stageButtons = [];

  function showStage(st, activeBtn) {
    for (const btn of stageButtons) {
      btn.setAttribute('data-active', btn === activeBtn ? 'true' : 'false');
    }
    inspector.replaceChildren();
    const detailText = node('p', st.detail, 'futures-chain-detail');
    inspector.append(detailText);
    if (canvas.lead_lag?.detail && st.step === 4) {
      inspector.append(node('p', canvas.lead_lag.detail, 'futures-meta'));
    }
    const linksRow = node('div', null, 'futures-chain-pills');
    for (const fut of st.futures || []) {
      const chg = formatSignedPct(fut.change_1d_pct);
      const txt = fut.name + ' ' + fut.symbol +
        (fut.close != null ? ' · ' + fut.close : '') +
        (chg ? ' (' + chg + ')' : '');
      const pill = node('a', txt + (fut.is_current ? ' [当前]' : ' ↗'), 'futures-entity-pill');
      pill.href = productLink(fut.symbol);
      pill.setAttribute('data-kind', 'futures');
      if (fut.is_current) pill.setAttribute('data-current', 'true');
      linksRow.append(pill);
    }
    for (const eq of st.equities || []) {
      const chg = formatSignedPct(eq.change_pct);
      const txt = eq.name + ' ' + eq.ticker + '（' + eq.role + '）' + (chg ? ' ' + chg : '') + ' ↗';
      const pill = node('a', txt, 'futures-entity-pill');
      pill.href = '/prism?mode=market&ticker=' + encodeURIComponent(eq.ticker) + '&name=' + encodeURIComponent(eq.name);
      pill.setAttribute('data-kind', 'equity');
      linksRow.append(pill);
    }
    if (linksRow.children.length) inspector.append(linksRow);
  }

  for (const st of canvas.stages) {
    const btn = node('button', null, 'futures-stage-node');
    btn.type = 'button';
    btn.setAttribute('data-step', String(st.step));
    const stepTop = node('div', null, 'futures-stage-top');
    stepTop.append(node('span', '0' + st.step + ' · ' + st.stage, 'futures-stage-kicker'), node('em', st.badge, 'futures-stage-badge'));
    btn.append(stepTop, node('strong', st.title, 'futures-stage-title'), node('span', st.metric, 'futures-stage-metric'));
    btn.addEventListener('click', () => showStage(st, btn));
    stageButtons.push(btn);
    stageGrid.append(btn);
  }

  wrap.append(stageGrid, inspector);
  const defaultIdx = canvas.stages.findIndex(s => s.step === 2 && s.badge !== '待核验');
  const pickIdx = defaultIdx >= 0 ? defaultIdx : Math.min(2, canvas.stages.length - 1);
  showStage(canvas.stages[pickIdx], stageButtons[pickIdx]);
  return wrap;
}

function renderMarginsCard(margins) {
  if (!Array.isArray(margins) || !margins.length) return null;
  const section = node('section', null, 'futures-card');
  section.append(node('h3', '跨品种盘面利润与比价'));
  section.append(node('p', '基于同日连续收盘价确定性复算；缺腿或日期不对齐时不估算。', 'futures-meta'));
  for (const [idx, m] of margins.entries()) {
    const row = node('div', null, 'futures-margin-row');
    row.id = 'brief-M' + (idx + 1);
    const top = node('div', null, 'futures-margin-head');
    top.append(node('strong', m.name));
    if (m.status === 'collected' && m.values) {
      const mv = m.values;
      const pct = mv.pct_5y != null ? mv.pct_5y : mv.pct_1y;
      const reading = mv.current + ' ' + m.unit + (pct != null ? ' · ' + mv.span_label + ' ' + pct + '%分位' : '');
      top.append(node('span', reading, 'futures-margin-val'));
      row.append(top);
      const detailParts = ['公式：' + m.formula, '对齐交易日：' + m.data_as_of];
      if (mv.change_5d != null) detailParts.push('5日变化：' + (mv.change_5d > 0 ? '+' : '') + mv.change_5d + ' ' + m.unit);
      row.append(node('p', detailParts.join(' · '), 'futures-meta'));
      if (mv.hint) row.append(node('p', mv.hint, 'futures-margin-hint'));
    } else {
      top.append(node('span', '待同步关联腿', 'futures-meta'));
      row.append(top, node('p', m.reason || '关联品种行情尚未对齐。', 'futures-meta'));
    }
    section.append(row);
  }
  return section;
}

function renderScenariosCard(causalView) {
  if (!causalView?.scenarios?.length) return null;
  const group = node('section', null, 'futures-panel-group futures-scenarios-group');
  group.append(node('h2', '可证伪情景路标与跟踪锚', 'futures-section-title'));
  const cardEl = node('div', null, 'futures-card');
  if (causalView.drivers?.length) {
    const driverWrap = node('div', null, 'futures-driver-row');
    driverWrap.append(node('span', '核心观测锚点：', 'futures-meta'));
    for (const d of causalView.drivers) {
      driverWrap.append(node('span', d, 'futures-pill'));
    }
    cardEl.append(driverWrap);
  }
  const grid = node('div', null, 'futures-scenario-grid');
  for (const sc of causalView.scenarios) {
    const box = node('article', null, 'futures-scenario-card');
    box.setAttribute('data-scenario', sc.id);
    box.append(
      node('h3', sc.title),
      node('p', '【如果】' + sc.if_condition, 'futures-scenario-if'),
      node('p', '【那么】' + sc.then_implication, 'futures-scenario-then')
    );
    if (typeof document.getElementById === 'function' && $('journal-study')) {
      const trackBtn = node('button', '加入判断账本 · 到期自动对账', 'futures-scenario-track');
      trackBtn.type = 'button';
      trackBtn.addEventListener('click', () => {
        if (typeof window !== 'undefined' && window.PrismJournalTrackScenario) {
          window.PrismJournalTrackScenario(sc.id);
        }
      });
      box.append(trackBtn);
    }
    grid.append(box);
  }
  cardEl.append(grid);
  group.append(cardEl);
  return group;
}

function renderCard(card) {
  $('futures-title').textContent = card.name;
  $('futures-meta').textContent = card.symbol + ' · ' + (card.exchange_name || card.exchange) +
    (card.archetype_label ? ' · ' + card.archetype_label : '');
  $('futures-meta').hidden = false;
  document.title = card.name + ' · 期货研究 · Prism';
  const brief = card.research_brief;
  const causalHero = renderCausalHero(card);
  const chainCanvas = renderChainCanvas(card);

  const market = observation('连续日线', card.market, (section, values) => {
    section.append(node('p', values.series_symbol + ' 连续序列', 'futures-meta'));
    const freshness = card.dimensions.freshness;
    if (freshness?.reason) section.append(node('p', freshness.reason, 'futures-meta'));
    const hero = node('div', null, 'futures-price');
    const headline = node('div');
    headline.append(node('strong', value(values.close)));
    if (values.quote_unit) headline.append(node('span', values.quote_unit, 'futures-price-unit'));
    hero.append(node('span', '收盘'), headline);
    section.append(hero);
    const points = values.bars.filter(bar => Number.isFinite(bar.close));
    if (points.length > 1) {
      const legend = node('div', null, 'futures-chart-legend');
      const lClose = node('span', '— 收盘价', 'futures-legend-close');
      const lEma20 = node('span', '— EMA20 短均线', 'futures-legend-ema20');
      const lEma60 = node('span', '-- EMA60 中均线', 'futures-legend-ema60');
      legend.append(lClose, lEma20, lEma60);
      const chart = node('canvas', null, 'futures-chart');
      chart.width = 900;
      chart.height = 190;
      chart.setAttribute('role', 'img');
      chart.setAttribute('aria-label', '连续日线收盘价及 EMA20/EMA60 均线走势，' + points[0].date + ' 至 ' + points.at(-1).date + '。详细数据见下方日线历史。');
      const span = node('p', null, 'futures-chart-dates');
      span.append(node('span', points[0].date), node('span', points.at(-1).date));
      section.append(legend, chart, span);
      const prices = points.map(point => point.close);
      const ema20 = computeEma(prices, 20);
      const ema60 = computeEma(prices, 60);
      const allVals = [...prices, ...ema20, ...ema60];
      const min = Math.min(...allVals);
      const max = Math.max(...allVals);
      if (typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(() => {
          const ctx = chart.getContext('2d');
          if (!ctx) return;
          const accent = typeof getComputedStyle === 'function' ? getComputedStyle(chart).color : '#256451';
          const plotSeries = (arr, color, width, dash = []) => {
            ctx.save();
            ctx.strokeStyle = color;
            ctx.lineWidth = width;
            if (ctx.setLineDash) ctx.setLineDash(dash);
            ctx.beginPath();
            arr.forEach((price, index) => {
              const x = 6 + index * (chart.width - 12) / (arr.length - 1);
              const y = max === min ? chart.height / 2 : 14 + (max - price) * (chart.height - 28) / (max - min);
              if (index === 0) ctx.moveTo(x, y);
              else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.restore();
          };
          plotSeries(ema60, '#6366f1', 1.6, [6, 4]);
          plotSeries(ema20, '#d97706', 1.9);
          plotSeries(prices, accent, 2.8);
        });
      }
    }
    facts(section, ['open', 'high', 'low', 'volume', 'open_interest'].map(key =>
      [fieldLabels[key], values[key], ['volume', 'open_interest'].includes(key) ? null : values.quote_unit]));
    if (values.bars.length) {
      const history = node('details');
      history.append(node('summary', '近 ' + values.bars.length + ' 条日线'));
      table(history, '交易日', 'date', ['open', 'high', 'low', 'close'], [...values.bars].reverse(), '连续日线历史');
      section.append(history);
    }
  }, 'h2');
  market.classList.add('futures-card-market');

  const panels = node('div', null, 'futures-panels');
  const marketAnalysis = node('div', null, 'futures-panel-group');
  const physical = node('div', null, 'futures-panel-group');
  const contracts = node('div', null, 'futures-panel-group');
  const physicalTitle = node('h2', '基本面', 'futures-section-title');
  const contractTitle = node('h2', '合约数据', 'futures-section-title');
  physical.append(physicalTitle);
  contracts.append(contractTitle);
  const trend = brief?.technical;
  let trendObservation = card.technicals;
  if (trend && !trendObservation.values) {
    trendObservation = {...trendObservation,
      reason: [trend.label, trend.reason, trendObservation.reason].filter(Boolean).join(' '),
    };
  }
  marketAnalysis.append(
    node('h2', '技术与持仓', 'futures-section-title'),
    observation('技术面', trendObservation, (section, v) => {
      let metrics = section;
      if (trend) {
        section.id = 'brief-T1';
        section.append(node('strong', trend.label), node('p', trend.reason, 'futures-meta'));
        metrics = node('details');
        metrics.append(node('summary', '查看技术指标'));
        section.append(metrics);
      }
      facts(metrics, [
        ['20 / 60 日指数均线', [value(v.ema20), value(v.ema60)].join(' / ')],
        ['14 日趋势强度（ADX）', v.adx14], ['14 日平均真实波幅（ATR）', v.atr14],
        ['20 / 60 日年化波动率', [value(v.volatility20_pct, '%'), value(v.volatility60_pct, '%')].join(' / ')],
        ['20 / 60 / 250 日区间位置', [v.range20_pct, v.range60_pct, v.range250_pct].map(n => value(n, '%')).join(' / ')],
      ]);
    }),
    observation('资金面 · 成交与持仓', card.positioning, (section, v) => {
      facts(section, [
        ['成交量', v.volume, '手'], ['持仓量', v.open_interest, '手'],
        ['持仓较上一条日线变化' + (v.previous_date ? '（' + v.previous_date + '）' : ''), v.change, '手'],
        ['成交量 / 持仓量', v.volume_to_interest],
      ]);
      section.append(node('p', '连续序列可能换月。手数反映市场参与度，资金净额须有独立的资金数据。', 'futures-meta'));
    }),
    observation('交易所会员排名', card.rankings, (section, v) => {
      const labels = {volume: '成交', long: '持买', short: '持卖'};
      section.append(node('p', v.contract.toUpperCase(), 'futures-meta'));
      facts(section, Object.entries(v.sides).map(([key, side]) =>
        [labels[key] + '上榜 ' + side.ranked_members + ' 家合计', side.quantity_sum, '手']));
      if (v.notice) section.append(node('p', v.notice, 'futures-meta'));
    }),
  );

  if (causalHero) panels.append(causalHero);
  if (chainCanvas) panels.append(chainCanvas);
  panels.append(market);

  const marginsCard = renderMarginsCard(card.margins);
  if (marginsCard) physical.append(marginsCard);

  physical.append(
    observation('现货基差', card.basis, (section, v) => facts(section, [
      ['现货价', v.spot_price, v.quote_unit], [(v.futures_contract?.toUpperCase() || '合约未提供') + ' 期货价', v.futures_price, v.quote_unit],
      ['现货减期货', v.basis, v.quote_unit],
      ['基差率（基差 / 现货价）', v.basis_rate_pct, '%'],
    ])),
    observation('仓单库存', card.inventory, (section, v) => {
      facts(section, [['仓单', v.quantity, v.unit], ['日变化', v.change, v.unit]]);
      const scope = [v.segment, v.basis].filter(Boolean).join(' · ');
      if (scope) section.append(node('p', scope, 'futures-meta'));
    }),
    observation('现货历史', card.spot_history, (section, v) => {
      section.append(node('p', v.first_date + ' 至 ' + v.last_date + ' · ' + v.unit, 'futures-meta'));
      table(section, '日期', 'date', ['price'], v.observations, '现货历史');
    }),
  );
  if (card.benchmark) {
    physical.prepend(observation(card.benchmark.values?.label || '品种专项资料', card.benchmark, (section, v) => {
      facts(section, v.rows.map(row => [row.label, row.value, row.unit]));
      section.append(node('p', v.scope, 'futures-meta'));
    }));
    physical.prepend(physicalTitle);
  }
  contracts.append(
    observation('跨期价差', card.spread, (section, v) => facts(section, [
      [(v.near_contract?.toUpperCase() || '合约未提供') + ' 近月结算', v.near_settlement, v.quote_unit],
      [(v.far_contract?.toUpperCase() || '合约未提供') + ' 远月结算', v.far_settlement, v.quote_unit],
      ['远月减近月', v.far_minus_near, v.quote_unit],
    ])),
    observation('合约日报', card.settlements, (section, v) => {
      if (v.quote_unit) section.append(node('p', v.quote_unit, 'futures-meta'));
      table(section, '合约', 'contract', ['close', 'settlement', 'volume', 'open_interest'], v.contracts, '合约日报');
    }),
    observation('交易与交割日历', card.contract_dates, (section, v) =>
      table(section, '合约', 'contract', ['listed_expiry_date', 'delivery_start', 'delivery_end'], v.contracts, '交易与交割日历')),
    observation('交易参数', card.contract_parameters, (section, v) =>
      table(section, '合约', 'contract', ['upper_limit_pct', 'lower_limit_pct',
        'spec_long_margin_pct', 'spec_short_margin_pct'], v.contracts, '交易参数')),
  );
  if (brief?.fundamentals) physicalTitle.after(fundamentalChanges(brief.fundamentals));
  panels.append(physical, marketAnalysis);
  const scenariosCard = renderScenariosCard(card.causal_view);
  if (scenariosCard) panels.append(scenariosCard);
  if (brief?.institutions) panels.append(institutionViews(brief.institutions));
  panels.append(contracts);
  if (brief?.interpret_available && root.dataset?.account) {
    root.replaceChildren(interpretation(card, root.dataset.account), panels);
  } else root.replaceChildren(panels);
}

let overviewTimer;
async function load(background = false) {
  clearTimeout(overviewTimer);
  if (!background) {
    root.setAttribute('aria-busy', 'true');
    status.textContent = '正在读取…';
    retry.hidden = true;
  }
  try {
    let data = null;
    if (!background && !symbol) {
      const boot = $('futures-bootstrap');
      if (boot && typeof boot.textContent === 'string' && boot.textContent.trim()) {
        try {
          data = JSON.parse(boot.textContent);
        } catch (_) {
          data = null;
        }
        boot.textContent = '';
        if (typeof boot.remove === 'function') boot.remove();
      }
    }
    if (!data) {
      const path = symbol ? '/api/futures/' + encodeURIComponent(symbol) + '/card' : '/api/futures/universe';
      const response = await fetch(path, {cache: 'no-store'});
      if (response.status === 401 || response.status === 403) {
        location.assign('/login');
        return;
      }
      if (response.status === 404) {
        status.textContent = '该品种不在期货分析目录中。';
        return;
      }
      if (!response.ok) throw new Error(response.status === 429 ? '查询较频繁，请稍后重试。' : '资料暂时无法读取。');
      data = await response.json();
    }
    status.textContent = '';
    if (symbol) {
      renderCard(data);
      if (typeof window !== 'undefined' && window.PrismFuturesCopilot) {
        const prod = data.product || {};
        const pct5 = data.percentiles?.close_pct_5y;
        const headline = data.causal_view?.headline || '';
        window.PrismFuturesCopilot.setContext({
          surface: 'futures_detail',
          symbol: prod.symbol || symbol,
          name: prod.name || symbol,
          summary: [pct5 != null ? ('近5年 ' + pct5 + '% 分位') : '', headline].filter(Boolean).join(' · ')
        });
      }
    } else {
      const focused = root.contains && root.contains(document.activeElement) && document.activeElement.getAttribute('href');
      renderUniverse(data);
      if (typeof window !== 'undefined' && window.PrismFuturesCopilot) {
        window.PrismFuturesCopilot.setContext({
          surface: 'futures_radar',
          summary: '期货研究 V2 全市场极值与自动对账雷达'
        });
      }
      if (focused) [...root.querySelectorAll('.futures-tile')].find(link => link.getAttribute('href') === focused)?.focus();
      if (data.refreshing) overviewTimer = setTimeout(() => load(true), 10000);
    }
  } catch (error) {
    if (background) return;
    status.textContent = error instanceof TypeError ? '资料暂时无法读取。' : error.message;
    retry.hidden = false;
    retry.focus();
  } finally {
    root.setAttribute('aria-busy', 'false');
  }
}

const GUIDE_STORAGE_KEY = 'prism-futures-guide-seen-v1';
const OVERVIEW_TOUR_STEPS = [
  {
    sel: '.futures-radar',
    title: '全市场异动与自动对账雷达',
    text: '一进页面先看这里：系统自动筛出近 5 年价格高低极值（≥82% / ≤18% 分位）、产业链利润极值与主力换月跳空提醒，不用逐个品种翻找。',
    note: '运转机制：后台每 30 分钟自动巡检 46 个品种并重算雷达，无需手动刷新触发。',
  },
  {
    sel: '.futures-archetype-bar, #futures-search-label',
    title: '按产业定价逻辑筛选与搜索',
    text: '按「黑色建材、有色贵金属、能化航运、农牧油脂」等产业原型一键归类，也可在右上角直接输入品种名称或代码定位。',
    note: null,
  },
  {
    sel: '.futures-tile',
    title: '品种卡片：5 年分位条与五维核验',
    text: '卡片直观展示最新收盘、涨跌幅与 5 年历史分位位置；底部五格色块实事求是标明行情、技术、资金、基本面与时效状态，点进任意卡片即可打开深度因果画布。',
    note: null,
  },
  {
    sel: null,
    title: '分析触发与对账机制一览',
    text: '① 分位、产业利润与因果卡片每次打开实时秒出；② 触发极值或新研报的品种由后台自动预热 AI 解读，其余品种按需点击生成并缓存复用；③ 加入账本的判断每 30 分钟自动到期对账。',
    note: '随时点击右上角「?」可再次打开本指引。',
  },
];

const DETAIL_TOUR_STEPS = [
  {
    sel: '.futures-causal-hero',
    title: '今日主线因果卡与 5 年分位温度计',
    text: '打开即秒出：用直白语言提炼当前核心矛盾与季节性锚点，配合近 5 年历史分位温度计一眼看清价格、基差和利润处于历史高位还是低位安全垫。',
    note: '确定性量化与本体规则实时拼装（60 秒内存缓存），零大模型等待。',
  },
  {
    sel: '.futures-chain-canvas',
    title: '四段式产业链传导与期股联动',
    text: '串联「上游原料 → 中游盘面利润阀门 → 本品种期现库存 → 下游映射 A 股」，自动识别期强股弱 / 股强期弱，点击关联 A 股可直接跳回 Prism 个股研究。',
    note: null,
  },
  {
    sel: '.futures-interpretation, .futures-card-market',
    title: 'AI 证据解读（智能预热 + 按需生成）',
    text: '异动品种后台会自动预核验生成解读；若尚未预热，点击「解读当前资料」即可即时触发，每句结论均附带 [F1] / [T1] 可溯源锚点并按数据指纹缓存复用。',
    note: null,
  },
  {
    sel: '.futures-scenarios-group',
    title: '可证伪情景路标 · 一键入账自动对账',
    text: '情景 A / B 给出明确的「如果…那么…」观测条件。点击卡片上的「加入判断账本」，系统会在后台每 30 分钟自动跟踪行情与库存，到期自动结算支持或证伪。',
    note: null,
  },
  {
    sel: '#journal-study',
    title: '我的判断账本与单项查证',
    text: '页面底部集中沉淀你的历史判断与自动对账记录；如需单独核验某项细分指标或查看历史回放时点，展开「自定义单项指标查证」即可。',
    note: '随时点击顶栏右上角「?」可重看本页指引。',
  },
];

let tourState = null;

function markGuideSeen() {
  try {
    localStorage.setItem(GUIDE_STORAGE_KEY, '1');
  } catch (_) { /* Storage may be restricted in private browsing. */ }
  const banner = $('futures-guide-banner');
  if (banner) banner.hidden = true;
}

function findVisibleTarget(selector) {
  if (!selector || typeof document.querySelectorAll !== 'function') return null;
  for (const el of document.querySelectorAll(selector)) {
    if (!el.hidden && (typeof el.getClientRects !== 'function' || el.getClientRects().length > 0)) {
      return el;
    }
  }
  return null;
}

function positionFuturesTour() {
  if (!tourState?.tip) return;
  const tip = tourState.tip;
  const target = tourState.target;
  const vp = typeof window !== 'undefined' ? window.visualViewport : null;
  const vx = vp ? vp.offsetLeft : 0;
  const vy = vp ? vp.offsetTop : 0;
  const vw = vp ? vp.width : (document.documentElement.clientWidth || 1024);
  const vh = vp ? vp.height : (typeof window !== 'undefined' ? window.innerHeight : 768);
  const margin = 12;
  const gap = 10;
  tip.style.width = Math.max(240, Math.min(360, vw - margin * 2)) + 'px';
  const size = typeof tip.getBoundingClientRect === 'function' ? tip.getBoundingClientRect() : {width: 340, height: 210};
  let left = vx + (vw - size.width) / 2;
  let top = vy + (vh - size.height) / 2;
  if (target && typeof target.getBoundingClientRect === 'function') {
    const rect = target.getBoundingClientRect();
    left = rect.left;
    top = rect.bottom + gap;
    if (top + size.height > vy + vh - margin) {
      top = rect.top - size.height - gap;
    }
  }
  tip.style.left = Math.max(vx + margin, Math.min(left, vx + vw - size.width - margin)) + 'px';
  tip.style.top = Math.max(vy + margin, Math.min(top, vy + vh - size.height - margin)) + 'px';
}

function closeFuturesTour(restoreFocus = false) {
  if (!tourState) return;
  if (typeof window !== 'undefined' && tourState.reposition) {
    window.removeEventListener('resize', tourState.reposition);
    window.removeEventListener('scroll', tourState.reposition, true);
    window.removeEventListener('keydown', tourState.onKey);
    if (window.visualViewport) {
      window.visualViewport.removeEventListener('resize', tourState.reposition);
      window.visualViewport.removeEventListener('scroll', tourState.reposition);
    }
  }
  if (tourState.target?.classList?.remove) tourState.target.classList.remove('futures-tour-spot');
  if (tourState.overlay?.remove) tourState.overlay.remove();
  if (tourState.tip?.remove) tourState.tip.remove();
  tourState = null;
  if (restoreFocus) $('futures-help-btn')?.focus?.();
}

function renderFuturesTourStep() {
  if (!tourState || !document.body) return;
  const step = tourState.steps[tourState.index];
  if (!step) {
    closeFuturesTour(true);
    return;
  }
  if (tourState.target?.classList?.remove) tourState.target.classList.remove('futures-tour-spot');
  if (tourState.overlay?.remove) tourState.overlay.remove();
  if (tourState.tip?.remove) tourState.tip.remove();

  const target = findVisibleTarget(step.sel);
  if (target && typeof target.scrollIntoView === 'function') {
    target.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }
  tourState.target = target;
  if (target?.classList?.add) target.classList.add('futures-tour-spot');

  const overlay = node('div', null, 'futures-tour-overlay');
  overlay.id = 'futures-tour-overlay';
  overlay.addEventListener('click', () => {
    if (!tourState) return;
    if (tourState.index >= tourState.steps.length - 1) closeFuturesTour(true);
    else {
      tourState.index += 1;
      renderFuturesTourStep();
    }
  });

  const tip = node('div', null, 'futures-tour-tip');
  tip.id = 'futures-tour-tip';
  tip.setAttribute('role', 'dialog');
  tip.setAttribute('aria-modal', 'true');
  tip.setAttribute('aria-label', step.title);
  if (!tip.style) tip.style = {};

  const head = node('div', null, 'futures-tour-head');
  head.append(
    node('strong', step.title),
    node('span', (tourState.index + 1) + ' / ' + tourState.steps.length, 'futures-tour-idx')
  );
  const closeBtn = node('button', '×', 'futures-tour-close');
  closeBtn.type = 'button';
  closeBtn.setAttribute('aria-label', '关闭指引');
  closeBtn.addEventListener('click', () => closeFuturesTour(true));
  head.append(closeBtn);

  const body = node('p', step.text, 'futures-tour-body');
  tip.append(head, body);
  if (step.note) tip.append(node('p', step.note, 'futures-tour-note'));

  const acts = node('div', null, 'futures-tour-acts');
  if (tourState.index > 0) {
    const prevBtn = node('button', '← 上一步', 'futures-tour-btn');
    prevBtn.type = 'button';
    prevBtn.addEventListener('click', () => {
      tourState.index -= 1;
      renderFuturesTourStep();
    });
    acts.append(prevBtn);
  }
  const isLast = tourState.index >= tourState.steps.length - 1;
  const nextBtn = node('button', isLast ? '完成' : '下一步 →', 'futures-tour-btn futures-tour-btn-primary');
  nextBtn.type = 'button';
  nextBtn.addEventListener('click', () => {
    if (isLast) closeFuturesTour(true);
    else {
      tourState.index += 1;
      renderFuturesTourStep();
    }
  });
  acts.append(nextBtn);
  tip.append(acts);

  tourState.overlay = overlay;
  tourState.tip = tip;
  document.body.append(overlay, tip);
  positionFuturesTour();
  nextBtn.focus?.();
}

function startFuturesTour() {
  if (!document.body) return;
  markGuideSeen();
  closeFuturesTour(false);
  const rawSteps = symbol ? DETAIL_TOUR_STEPS : OVERVIEW_TOUR_STEPS;
  const activeSteps = rawSteps.filter(s => !s.sel || findVisibleTarget(s.sel));
  const steps = activeSteps.length ? activeSteps : [OVERVIEW_TOUR_STEPS.at(-1)];
  const reposition = () => positionFuturesTour();
  const onKey = ev => {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      closeFuturesTour(true);
    }
  };
  tourState = {steps, index: 0, target: null, overlay: null, tip: null, reposition, onKey};
  if (typeof window !== 'undefined' && window.addEventListener) {
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('keydown', onKey);
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', reposition);
      window.visualViewport.addEventListener('scroll', reposition);
    }
  }
  renderFuturesTourStep();
}

try {
  const theme = localStorage.getItem('cl-theme');
  if (theme === 'light' || theme === 'dark') document.documentElement.dataset.theme = theme;
} catch (_) { /* System theme remains available. */ }
try {
  const guideBanner = $('futures-guide-banner');
  if (guideBanner && !localStorage.getItem(GUIDE_STORAGE_KEY)) guideBanner.hidden = false;
} catch (_) { /* Keep guide accessible via header ? button. */ }
$('futures-help-btn')?.addEventListener?.('click', startFuturesTour);
$('futures-guide-start')?.addEventListener?.('click', startFuturesTour);
$('futures-guide-dismiss')?.addEventListener?.('click', markGuideSeen);
$('all-futures').hidden = !symbol;
retry.addEventListener('click', () => load());
load();
