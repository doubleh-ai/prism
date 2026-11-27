'use strict';

function fundamentalChanges(data) {
  const section = node('section', null, 'futures-card');
  section.append(node('h3', '基本面变化'));
  if (!data.rows.length) {
    section.append(node('p', data.reason || '暂无可比较的两期数据。', 'futures-meta'));
    return section;
  }
  for (const [index, row] of data.rows.entries()) {
    const entry = node('div', null, 'futures-evidence-row');
    entry.id = 'brief-F' + (index + 1);
    entry.append(node('strong', row.label + (row.scope ? ' · ' + row.scope : '')));
    const change = row.change > 0 ? '+' + row.change : row.change;
    entry.append(node('span', '较上期 ' + value(change, row.unit)));
    const dates = node('p', null, 'futures-meta');
    dates.append(row.data_as_of + '　' + value(row.current, row.unit));
    if (row.previous_date && row.previous != null) {
      dates.append(' ／ ' + row.previous_date + '　' + value(row.previous, row.unit));
    }
    const sources = node('p', null, 'futures-meta');
    if (row.stale) sources.append('较早资料 · ');
    sources.append(sourceLink(row.source));
    if (row.previous_source?.url && row.previous_source.url !== row.source?.url) {
      sources.append(' · ', sourceLink({...row.previous_source, name: '上期来源'}));
    }
    entry.append(dates, sources);
    section.append(entry);
  }
  return section;
}

function institutionViews(data) {
  const group = node('section', null, 'futures-panel-group');
  group.append(node('h2', '机构观点', 'futures-section-title'));
  const content = node('div', null, 'futures-card');
  group.append(content);
  if (data.window_days) content.append(node('p', '近 ' + data.window_days + ' 日收录的公开研报', 'futures-meta'));
  const unread = data.unclassified_items || [];
  for (const item of unread) {
    const source = node('p', null, 'futures-meta');
    source.append(item.institution + ' · ' + item.published_on + ' · 原文待核对 · ',
      sourceLink({name: item.title, url: item.url}));
    content.append(source);
  }
  if (!data.items.length && unread.length) return group;
  if (!data.items.length) {
    const text = data.status === 'stale' && data.latest_published_on
      ? '近期暂无收录的观点。上次收录的研报发表于 ' + data.latest_published_on + '。'
      : '暂无可核验的当期机构研报。';
    content.append(node('p', text, 'futures-meta'));
    return group;
  }
  const sides = {bullish: '偏多', bearish: '偏空', neutral: '未给出单边方向', unclassified: '原文方向未明'};
  for (const [index, report] of data.items.entries()) {
    const entry = node('article', null, 'futures-evidence-row');
    entry.id = 'brief-I' + (index + 1);
    const stanceBadge = node('span', sides[report.stance] || report.stance, 'futures-stance-pill');
    stanceBadge.setAttribute('data-stance', report.stance || 'unclassified');
    entry.append(node('h3', report.institution), stanceBadge);
    entry.append(node('p', report.anchor && report.anchor === report.summary ? '“' + report.anchor + '”' : report.summary));
    if (report.anchor && report.anchor !== report.summary) {
      const original = node('details');
      original.append(node('summary', '查看原句'), node('q', report.anchor));
      entry.append(original);
    }
    const context = [report.horizon && '期限 · ' + report.horizon,
      report.condition && '条件 · ' + report.condition].filter(Boolean).join('　');
    if (context) entry.append(node('p', context, 'futures-meta'));
    const source = node('p', null, 'futures-meta');
    const location = report.locator ? ' · ' + report.locator : '';
    source.append(report.published_on + ' · ', sourceLink({name: report.title + location, url: report.url}));
    entry.append(source);
    content.append(entry);
  }
  const missing = data.missing_sides.map(side => sides[side]).filter(Boolean);
  if (missing.length) content.append(node('p', '本期研报未明确给出' + missing.join('、') + '判断。', 'futures-meta'));
  return group;
}

function renderInterpretationPayload(result, output, notice, prefix) {
  output.replaceChildren();
  const summary = node('p', result.summary, 'futures-interpretation-text');
  const when = new Date(result.as_of);
  const asOf = Number.isNaN(when.getTime()) ? '' : new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).format(when);
  output.append(summary);
  if (result.references?.length) {
    const citations = node('details');
    citations.append(node('summary', '查看引用依据（' + result.references.length + '）'));
    const list = node('ul');
    for (const ref of result.references) {
      const label = '[' + ref.id + '] ' + ref.label;
      const item = node('li');
      if (/^[FTI][1-9][0-9]*$/.test(ref.id) && document.getElementById('brief-' + ref.id)) {
        const anchor = node('a', label);
        anchor.href = '#brief-' + ref.id;
        item.append(anchor);
      } else {
        item.textContent = label;
      }
      list.append(item);
    }
    citations.append(list);
    output.append(citations);
  }
  notice.textContent = asOf ? (prefix || '资料截至 ') + asOf + ' 北京时间' : '解读完成';
}

function interpretation(card, owner) {
  const section = node('section', null, 'futures-interpretation');
  section.setAttribute('aria-label', '当前资料解读');
  const button = node('button', '解读当前资料');
  button.type = 'button';
  const notice = node('span', null, 'futures-meta');
  notice.setAttribute('role', 'status');
  const output = node('div', null, 'futures-interpretation-result');
  section.append(button, notice, output);
  if (card.research_brief?.pre_interpretation?.summary) {
    queueMicrotaskSafe(() => {
      renderInterpretationPayload(card.research_brief.pre_interpretation, output, notice, '后台预核验 · 资料截至 ');
    });
  }
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    notice.textContent = '正在解读…';
    try {
      const response = await fetch('/api/futures/' + encodeURIComponent(card.symbol) + '/interpret', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'X-Requested-With': 'XMLHttpRequest', 'X-Request-ID': crypto.randomUUID(), 'X-Prism-Account': owner,
          'X-Futures-Evidence': card.research_brief.evidence_id},
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = typeof result.detail === 'string' ? result.detail : '暂时无法解读，请稍后重试。';
        throw new Error(message);
      }
      renderInterpretationPayload(result, output, notice, '资料截至 ');
      button.textContent = '已解读';
    } catch (error) {
      notice.textContent = error instanceof TypeError ? '网络异常，请稍后重试。' : error.message;
      button.disabled = false;
    }
  });
  return section;
}

function queueMicrotaskSafe(fn) {
  if (typeof queueMicrotask === 'function') queueMicrotask(fn);
  else Promise.resolve().then(fn);
}
