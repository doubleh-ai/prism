(() => {
  'use strict';
  const panel = document.getElementById('journal-study');
  if (!panel) return;
  const by = id => panel.querySelector('#journal-' + id);
  const symbol = panel.dataset.symbol;
  const base = '/api/futures/' + encodeURIComponent(symbol);
  const examples = {
    JM: [
      ['供需原因', '近月相对走强，是进口收紧还是焦企补库？'],
      ['近远月价差', '两份合约的价差怎么走？'],
      ['持仓变化', '持仓变化能说明资金在做多吗？'],
    ],
    RB: [
      ['去库原因', '这周螺纹钢去库，是需求回暖还是钢厂减产？'],
      ['厂库与社库', '社库和厂库的变化一致吗？'],
      ['终端采购', '终端采购能印证需求吗？'],
    ],
    M: [
      ['去库原因', '豆粕去库是需求拉动还是压榨变化？'],
      ['油厂库存', '原料豆和豆粕库存变化一致吗？'],
      ['压榨进度', '实际压榨达到计划了吗？'],
    ],
    EC: [
      ['运价指数', '欧线结算运价指数最近一期怎么变了？'],
      ['交割结算', 'EC2611 的三次定价如何影响交割结算？'],
      ['停航影响', '停航公告能说明运力减少吗？'],
    ],
    I: [
      ['矿螺强弱', '铁矿石近期相对螺纹钢偏强还是偏弱？'],
      ['期现基差', '铁矿石当前现货升贴水与盘面方向一致吗？'],
      ['仓单趋势', '铁矿石仓单变化能印证交割压力吗？'],
    ],
    HC: [
      ['卷螺差', '当前热卷相对螺纹钢的卷螺差处于什么位置？'],
      ['期现基差', '热卷现货报价跟上期货盘面变化了吗？'],
      ['仓单库存', '上期所热卷仓库仓单近期增减如何？'],
    ],
    CU: [
      ['期现升贴水', '沪铜当前现货升贴水与上期所仓单如何配合？'],
      ['历史分位', '沪铜收盘价在近5年处于什么历史分位？'],
      ['期限结构', '沪铜近远月结算价差呈现升水还是贴水？'],
    ],
    LC: [
      ['期现基差', '碳酸锂现货报价与期货主力基差收敛了吗？'],
      ['趋势与波动', '碳酸锂当前价格处于上市以来什么分位？'],
      ['持仓博弈', '碳酸锂持仓变化与日线趋势是否共振？'],
    ],
    AU: [
      ['金银比价', '当前沪金银比处于近5年偏高还是偏低区间？'],
      ['期现仓单', '沪金指定交割金库仓单与基差有何变化？'],
      ['中长趋势', '沪金日线均线排列与历史分位如何？'],
    ],
    MA: [
      ['MTO利润锚', '下游PP相对甲醇的MTO盘面价差目前能支撑甲醇吗？'],
      ['基差与仓单', '甲醇现货基差与郑商所仓单最新变化如何？'],
      ['趋势位置', '甲醇日线均线与近5年价格分位处于什么状态？'],
    ],
    SA: [
      ['玻碱传导', '玻璃—纯碱盘面差反映下游玻璃厂承接力如何？'],
      ['基差仓单', '纯碱现货基差与郑商所仓单有何变化？'],
      ['历史分位', '纯碱当前价格在近5年处于什么分位？'],
    ],
    LH: [
      ['养殖利润锚', '盘面猪料比指标当前处于近5年盈利还是亏损分位？'],
      ['均价对照', '生猪现期走势与盘面方向一致吗？'],
      ['趋势结构', '生猪连续日线均线与波动率处于什么状态？'],
    ],
  };
  const fallbackPresets = [
    ['期现基差', symbol + ' 当前现货升贴水与盘面方向一致吗？'],
    ['仓单库存', symbol + ' 最新仓单与交割库存有何变化？'],
    ['历史分位', symbol + ' 收盘价在近5年历史区间处于什么位置？'],
  ];
  const reviewLabels = {
    supported: '原判断得到支持',
    contradicted: '原判断未获支持',
    observed: '已记录核对结果',
    pending: '等待下一期数据披露',
  };
  let owner = null;
  let generation = 0;
  let listGeneration = 0;
  let current = null;
  let busy = false;
  function make(tag, text, cls) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function button(label, action, cls) {
    const element = make('button', label, cls);
    element.type = 'button';
    element.addEventListener('click', action);
    return element;
  }
  function shanghai(value) {
    if (!value) return '';
    const time = new Date(value);
    return Number.isFinite(time.valueOf()) ? new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }).format(time) + ' 北京时间' : String(value);
  }
  function status(message) { by('status').textContent = message; }
  function busyForm(value) {
    busy = value;
    by('submit').disabled = value;
    by('presets').querySelectorAll('button').forEach(b => { b.disabled = value; });
  }
  function reset() {
    generation++;
    listGeneration++;
    current = null;
    busyForm(false);
    by('analysis').replaceChildren();
    by('list').replaceChildren();
    status('');
  }
  function clearAccount() {
    reset();
    by('question').value = '';
    owner = null;
    by('saved').open = false;
  }
  async function api(path, method = 'GET', body) {
    const headers = {'X-Requested-With': 'XMLHttpRequest'};
    if (owner) headers['X-Prism-Account'] = owner;
    if (body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, {method, headers, cache: 'no-store', credentials: 'same-origin',
      body: body ? JSON.stringify(body) : undefined});
    if (response.status === 401) { clearAccount(); location.replace('/login'); throw new Error('请重新登录。'); }
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const detail = typeof data?.detail === 'string' ? data.detail : '';
      if (response.status === 409 && detail.includes('账号')) { clearAccount(); status('账号已切换，请重新读取。'); }
      throw new Error(detail || '请求未完成，请稍后再试。');
    }
    return data;
  }
  async function identify() {
    const user = (await api('/api/me')).user;
    if (!user || (owner && owner !== user)) {
      clearAccount(); status('账号已切换，请重新读取。'); throw new Error('账号已切换，请重新读取。');
    }
    owner = user;
  }
  function evidence(rows) {
    const box = make('details');
    box.append(make('summary', '查看证据（' + rows.length + '）'));
    const list = make('ul', null, 'journal-evidence');
    for (const row of rows) {
      const item = make('li', row.label + ' · ' + row.value);
      const src = make('small', shanghai(row.as_of) + ' ');
      try {
        const url = new URL(row.source_url);
        if (!['https:', 'http:'].includes(url.protocol)) throw new Error();
        const link = make('a', row.source_name || '来源');
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; src.append(link);
      } catch (_) { src.append(row.source_name || ''); }
      item.append(src); list.append(item);
    }
    box.append(list); return box;
  }
  function fillReviewResult(container, next) {
    container.className = 'journal-review';
    container.dataset.status = next.status || 'observed';
    const headRow = make('div', null, 'journal-review-head');
    const modeLabel = next.auto_reconciled ? '后台已自动对账' : '到期复盘核对';
    const badge = make('span', modeLabel + ' · ' + (reviewLabels[next.status] || next.status), 'journal-review-badge');
    badge.dataset.status = next.status || 'observed';
    headRow.append(badge);
    if (next.as_of) headRow.append(make('small', shanghai(next.as_of), 'journal-study-meta'));
    container.replaceChildren(headRow, make('h4', next.headline), make('p', next.explanation));
    if (next.evidence?.length) container.append(evidence(next.evidence));
  }
  async function ask() {
    const question = by('question').value.trim();
    if (busy || !question) { if (!question) by('question').focus(); return; }
    const rev = ++generation, session = by('session').value;
    current = null; by('analysis').replaceChildren(); busyForm(true); status('正在核对资料…');
    try {
      await identify();
      if (rev !== generation) return;
      const answer = await api(base + '/research', 'POST', {question, session});
      if (rev !== generation) return;
      current = answer;
      const output = by('analysis');
      output.append(make('p', answer.question, 'journal-study-meta'), make('h3', answer.headline), make('p', answer.explanation),
        make('p', '研究截至 ' + shanghai(answer.as_of), 'journal-study-meta'));
      if (answer.evidence?.length) output.append(evidence(answer.evidence));
      if (answer.analysis_id && answer.choices?.length) {
        const row = make('div', null, 'journal-study-actions'), select = make('select'), rule = make('p', null, 'journal-study-meta');
        select.setAttribute('aria-label', '选择要跟踪的判断');
        for (const choice of answer.choices) { const opt = make('option', choice.label); opt.value = choice.id; select.append(opt); }
        const showRule = () => { rule.textContent = answer.choices[select.selectedIndex].rule; };
        select.onchange = showRule; showRule();
        const save = button('保存判断', async () => {
          if (current !== answer) return;
          save.disabled = true; select.disabled = true;
          try {
            await api(base + '/judgments', 'POST', {analysis_id: answer.analysis_id, choice_id: select.value});
            if (rev !== generation) return;
            status('判断已保存，到期后系统将自动对账。'); save.textContent = '已保存';
            if (by('saved').open) await loadList();
            else by('saved').open = true;
          } catch (err) { if (rev === generation) { status(err.message); save.disabled = false; select.disabled = false; } }
        }, 'journal-study-primary');
        row.append(select, save); output.append(row, rule);
      }
      status('');
    } catch (err) { if (rev === generation) status(err.message); }
    finally { if (rev === generation) busyForm(false); }
  }
  function renderRecord(item) {
    const card = make('article', null, 'journal-record'), result = make('div');
    card.append(make('h3', item.choice_label), make('p', item.question), make('p', shanghai(item.as_of) + ' · ' + item.rule, 'journal-study-meta'));
    const original = make('details'); original.append(make('summary', '当时的研究'), make('p', item.headline), make('p', item.explanation));
    if (item.evidence?.length) original.append(evidence(item.evidence)); card.append(original);
    if (item.auto_review) {
      fillReviewResult(result, item.auto_review);
    }
    const actions = make('div', null, 'journal-study-actions');
    const review = button(item.auto_review ? '查看复盘明细' : (item.session === 'current' ? '检查下一期' : '查看复盘'), async () => {
      review.disabled = true;
      try {
        const next = await api(base + '/judgments/' + encodeURIComponent(item.id) + '/review');
        if (!card.isConnected) return;
        fillReviewResult(result, next);
      } catch (err) { if (card.isConnected) status(err.message); }
      finally { review.disabled = false; }
    });
    const remove = button('取消跟踪', async () => {
      if (!window.confirm('取消跟踪这条判断？')) return;
      remove.disabled = true;
      try {
        await api(base + '/judgments/' + encodeURIComponent(item.id), 'DELETE');
        if (card.isConnected) { status('已取消跟踪。'); await loadList(); }
      } catch (err) { if (card.isConnected) status(err.message); remove.disabled = false; }
    });
    result.setAttribute('aria-live', 'polite'); actions.append(review, remove); card.append(actions, result); return card;
  }
  async function loadList(cursor) {
    const seq = ++listGeneration, session = by('session').value;
    const list = by('list');
    if (!cursor) list.replaceChildren();
    try {
      await identify(); if (seq !== listGeneration) return;
      const query = new URLSearchParams({session, include_review: '1'}); if (cursor) query.set('cursor', cursor);
      const page = await api(base + '/judgments?' + query);
      if (seq !== listGeneration) return;
      list.querySelector('[data-next]')?.remove();
      if (!page.items.length && !cursor) list.append(make('p', '这个研究时点还没有保存的判断。', 'journal-study-meta'));
      for (const item of page.items) list.append(renderRecord(item));
      if (page.next_cursor) {
        const more = button('更早的判断', () => loadList(page.next_cursor)); more.dataset.next = '1'; list.append(more);
      }
    } catch (err) { if (seq === listGeneration) { status(err.message); } }
  }
  let composing = false, commitTime = -Infinity, suppressSubmit = false;
  const input = by('question');
  input.addEventListener('compositionstart', () => { composing = true; });
  input.addEventListener('compositionend', () => { composing = false; commitTime = performance.now(); });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.isComposing || composing || e.keyCode === 229 || performance.now() - commitTime < 80)) {
      suppressSubmit = true; setTimeout(() => { suppressSubmit = false; }, 0);
    }
  });
  by('form').onsubmit = e => { e.preventDefault(); if (!composing && !suppressSubmit) ask(); };
  const presets = examples[symbol] || fallbackPresets;
  if (presets.length) input.placeholder = '例如：' + presets[0][1];
  for (const [label, question] of presets) {
    by('presets').append(button(label, () => { input.value = question; ask(); }));
  }
  by('saved').addEventListener('toggle', () => { if (by('saved').open) loadList(); });
  by('session').onchange = () => { reset(); if (by('saved').open) loadList(); };
  window.PrismJournalTrackScenario = scenarioId => {
    const picked = presets[scenarioId === 'falsify_bear' && presets[1] ? 1 : 0];
    if (!picked) return;
    by('session').value = 'current';
    input.value = picked[1];
    panel.scrollIntoView?.({behavior: 'smooth', block: 'nearest'});
    ask();
  };
  if (by('saved').open) loadList();
  window.addEventListener('pagehide', clearAccount);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (owner) identify().catch(err => status(err.message));
  });
})();
