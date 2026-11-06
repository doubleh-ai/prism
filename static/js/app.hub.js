/* Account-bound forms. Private values remain in memory, never localStorage. */
(function () {
  'use strict';
  var user, epoch = 0, profileVersion = 0, calendarRows = [], editing = null, pending = null, dirty = false, profileReady = false;
  var futuresRoot = document.getElementById('futuresWorkspace');
  var bodyPage = document.body && document.body.dataset && document.body.dataset.page;
  var embeddedFutures = !!futuresRoot && !bodyPage;
  var futuresActive = false, futuresReady = false, futuresInitializing = null;
  var $ = function (id) { return embeddedFutures && id === 'main' ? futuresRoot : document.getElementById(embeddedFutures && id === 'notice' ? 'futuresNotice' : id); };
  var page = embeddedFutures ? 'futures' : bodyPage;
  var zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  function node(tag, text, cls) { var el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
  function notice(text) { var target=$('notice'); if(target){target.textContent = text || ''; target.hidden = !text;} }
  async function request(path, options) {
    var controller = new AbortController(), timer = setTimeout(function(){controller.abort();},15000);
    try { return await fetch(path,Object.assign({},options,{signal:controller.signal})); }
    finally { clearTimeout(timer); }
  }
  async function identify() {
    var response = await request('/api/me', {credentials:'same-origin',cache:'no-store'});
    if (!response.ok) { $('main').replaceChildren(node('p','请重新登录')); location.replace('/login'); throw new Error('请重新登录'); }
    var me = await response.json();
    if (user && user !== me.user) { $('main').replaceChildren(node('p','账号已经切换，请刷新页面后继续。')); throw new Error('账号已经切换'); }
    user = me.user;
  }
  async function api(path, body, method) {
    if (body) await identify();
    var response = await request(path, {method:method || 'GET', credentials:'same-origin',cache:'no-store',
      headers:{'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest','X-Prism-Account':user || ''},
      body:body ? JSON.stringify(body) : undefined});
    var value = await response.json();
    if (!response.ok) { var error=new Error(typeof value.detail === 'string' ? value.detail : '请求未完成，请刷新核对');error.status=response.status;throw error; }
    return value;
  }
  function field(form, name) {
    var value = form.elements.namedItem(name);
    // Position rows repeat names such as currency. The form-level field is
    // first in document order; RadioNodeList.value is only defined for radios.
    return value && !value.tagName && typeof value.item === 'function' ? value.item(0) : value;
  }
  async function loadProfile() {
    var ticket = ++epoch;
    profileReady = false;
    $('profileForm').querySelector('[type=submit]').disabled = true;
    var value = await api('/api/preferences/research');
    if (ticket !== epoch) return;
    var profile = value.profile, form = $('profileForm');
    profileVersion = profile.version; profileReady = true; dirty = false;
    form.querySelector('[type=submit]').disabled = false;
    ['style','horizon','max_position_pct','max_drawdown_pct'].forEach(function (key) { if (profile.configured) field(form,key).value = profile[key]; });
    field(form,'allow_leverage').checked = !!profile.allow_leverage;
    $('profileVersion').textContent = profile.configured ? '已保存 · 版本 ' + profile.version : '尚未设置；保存后才会用于研究';
  }
  async function saveProfile(event) {
    event.preventDefault(); if (!profileReady) { notice('请先重新读取偏好，再保存修改。'); return; }
    var form = event.currentTarget, button = form.querySelector('[type=submit]');
    button.disabled = true; ++epoch;
    try {
      var value = await api('/api/preferences/research', {expected_version:profileVersion,
        style:field(form,'style').value,horizon:field(form,'horizon').value,
        max_position_pct:Number(field(form,'max_position_pct').value),max_drawdown_pct:Number(field(form,'max_drawdown_pct').value),
        allow_leverage:field(form,'allow_leverage').checked}, 'PUT');
      profileVersion = value.profile.version; dirty = false; $('profileVersion').textContent = '已保存 · 版本 ' + profileVersion;
      notice('偏好已保存，将用于下一轮研究。当前研究继续使用原版本。');
    } catch (error) { notice(error.message + '；若结果未知，请先重新读取，避免覆盖其他设备的修改。'); }
    finally { button.disabled = false; }
  }
  function dateInput(date) { return new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).format(date); }
  function localInput(value) {
    var parts = new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));
    var out={}; parts.forEach(function (part) { out[part.type]=part.value; });
    return out.year+'-'+out.month+'-'+out.day+'T'+out.hour+':'+out.minute;
  }
  function displayTime(value) { return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,month:'short',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value)); }
  function resolveTime(form, name, refresh, exact) {
    var local=field(form,name).value, choice=field(form,name+'Fold');
    var possibilities=window.WorkTasks.possibleInstants(local,zone);
    if (refresh) {
      choice.replaceChildren(node('option','请选择这一次时间')); choice.firstChild.value='';
      possibilities.forEach(function (item) { var option=node('option','UTC偏移 '+item.offset+' 分钟 · '+item.iso); option.value=item.iso; choice.appendChild(option); });
      choice.hidden=possibilities.length!==2;
      if (exact) choice.value=new Date(exact).toISOString();
    }
    if (!possibilities.length) throw new Error('该本地时间因夏令时切换而不存在，请选择其他时间');
    if (possibilities.length===1) return possibilities[0].iso;
    if (!possibilities.some(function (item) { return item.iso===choice.value; })) throw new Error('这一天存在两次相同时间，请选择对应的时区偏移');
    return choice.value;
  }
  function recurrenceUI() {
    var form=$('eventForm'), frequency=field(form,'frequency').value, endType=field(form,'endType').value;
    $('recurrenceOptions').hidden=!frequency; $('weekdays').hidden=frequency!=='weekly';
    $('untilLabel').hidden=endType!=='until'; $('countLabel').hidden=endType!=='count';
    field(form,'until').required=!!frequency&&endType==='until'; field(form,'count').required=!!frequency&&endType==='count';
  }
  function recurrenceBody(form) {
    var frequency=field(form,'frequency').value;if(!frequency)return null;
    var interval=Number(field(form,'interval').value), count=Number(field(form,'count').value), endType=field(form,'endType').value;
    if(!Number.isInteger(interval)||interval<1||interval>12)throw new Error('重复间隔须为 1 至 12');
    if(endType==='count'&&(!Number.isInteger(count)||count<1||count>366))throw new Error('重复次数须为 1 至 366');
    if(endType==='until'&&!field(form,'until').value)throw new Error('请选择结束日期');
    var weekdays=Array.from(form.querySelectorAll('[name=weekday]:checked')).map(function(input){return Number(input.value);}); if(frequency==='weekly'&&!weekdays.length){var date=field(form,'start').value.slice(0,10).split('-').map(Number);if(date.length===3)weekdays=[(new Date(Date.UTC(date[0],date[1]-1,date[2])).getUTCDay()+6)%7];}
    return {frequency:frequency,interval:interval,weekdays:weekdays,until:endType==='until'?field(form,'until').value:null,count:endType==='count'?count:null};
  }
  function eventBody(form) { return {title:field(form,'title').value,description:field(form,'description').value,
    start_at:resolveTime(form,'start',false),end_at:resolveTime(form,'end',false),timezone:zone,recurrence:recurrenceBody(form)}; }
  function editEvent(row) {
    editing=row; pending=null; dirty=false; var form=$('eventForm');
    field(form,'title').value=row.title; field(form,'description').value=row.description || ''; var recurrence=row.recurrence || null; field(form,'frequency').value=recurrence?recurrence.frequency:''; field(form,'interval').value=recurrence?recurrence.interval:1; field(form,'endType').value=recurrence&&recurrence.count?'count':'until'; field(form,'until').value=recurrence&&recurrence.until||''; field(form,'count').value=recurrence&&recurrence.count||12; form.querySelectorAll('[name=weekday]').forEach(function(input){input.checked=!!recurrence&&(recurrence.weekdays||[]).includes(Number(input.value));}); recurrenceUI();
    field(form,'start').value=localInput(row.start_at); field(form,'end').value=localInput(row.end_at);
    ['start','end'].forEach(function (name) { resolveTime(form,name,true,row[name+'_at']); });
    $('eventHeading').textContent='编辑日程 · 版本 '+row.version; form.scrollIntoView({block:'center',behavior:'smooth'});
  }
  function resetEvent() { editing=null; pending=null; dirty=false; $('eventForm').reset(); $('eventHeading').textContent='安排一件事'; ['startFold','endFold'].forEach(function (key) { field($('eventForm'),key).hidden=true; }); recurrenceUI(); }
  async function loadCalendar() {
    var ticket=++epoch, day=$('calendarDay').value;
    if (!day) return;
    var start=window.WorkTasks.possibleInstants(day+'T00:00',zone)[0];
    if (!start) throw new Error('该日期的本地零点不可用，请选择另一日期');
    var last=new Date(Date.parse(day+'T12:00:00Z')+7*86400000).toISOString().slice(0,10);
    var end=window.WorkTasks.possibleInstants(last+'T00:00',zone)[0];
    if (!end) throw new Error('结束日期的本地零点不可用');
    var value=await api('/api/work/calendar?start='+encodeURIComponent(start.iso)+'&end='+encodeURIComponent(end.iso));
    if (ticket!==epoch) return;
    calendarRows=value.occurrences || value.events || []; var list=$('calendarList'); list.replaceChildren();
    var rows=calendarRows.map(function (row) { return {kind:'event',row:row,time:row.start_at}; }).concat((value.tasks||[]).map(function(row){return {kind:'task',row:row,time:row.due_at};}));
    rows.sort(function(a,b){return a.time.localeCompare(b.time);});
    rows.forEach(function(item){
      var row=item.row, card=node('article'); card.append(node('h3',row.title));
      card.append(node('p',displayTime(item.time)+(item.kind==='event'?' — '+displayTime(row.end_at):' · 任务截止'),'muted'));
      if(item.kind==='event') {
        if(row.description) card.append(node('p',row.description));
        var edit=node('button',row.series_id?'编辑整组':'编辑','secondary'); edit.onclick=async function(){if(dirty&&!confirm('替换当前未保存的日程内容？'))return;edit.disabled=true;try{await identify();var value=await api('/api/work/calendar/'+encodeURIComponent(row.series_id||row.id));await identify();if(!value.event)throw new Error('未能读取整组日程');editEvent(value.event);}catch(error){notice(error.message+'；请重新读取日程。');}finally{edit.disabled=false;}};card.append(edit);
        var cancel=node('button',row.occurrence_id?'取消本次':'取消日程','secondary'); cancel.onclick=async function(){
          if(!confirm('取消这条日程？已有记录会保留。'))return;
          cancel.disabled=true;
          try { var body={request_id:crypto.randomUUID(),expected_version:row.version,cancelled:true}; var path='/api/work/calendar/'+encodeURIComponent(row.series_id||row.id); if(row.occurrence_id)path+='/occurrences/'+encodeURIComponent(row.occurrence_id);else Object.assign(body,{title:row.title,description:row.description,start_at:row.start_at,end_at:row.end_at,timezone:row.timezone,recurrence:row.recurrence||null}); await api(path,body,'PUT');await loadCalendar(); }
          catch(error){notice(error.message+'；请刷新核对取消结果。');cancel.textContent='请刷新核对';}
        };card.append(cancel);
        var reminder=node('select'), reminderLabel=node('label','导入日历后的提醒 ');
        [0,5,15,30,60].forEach(function(minutes){
          var option=node('option',minutes?'提前 '+minutes+' 分钟':'无提醒');option.value=String(minutes);reminder.append(option);
        });
        reminderLabel.append(reminder);
        var exportUrl='/api/work/calendar/'+encodeURIComponent(row.series_id||row.id)+'.ics',link=node('a','导出至日历 ↗');
        link.href=exportUrl;reminder.onchange=function(){link.href=exportUrl+'?reminder_minutes='+reminder.value;};
        card.append(reminderLabel,link);
      } else {var taskLink=node('a','打开任务 ↗');taskLink.href='/work';card.append(taskLink);}
      list.append(card);
    });
    if(!rows.length)list.append(node('p','这一周还没有日程或到期任务。','muted'));
    if(value.truncated)list.append(node('p','记录超过显示上限，请改选起始日期查看后续记录。','notice'));
  }
  async function saveEvent(event) {
    event.preventDefault();var form=event.currentTarget,button=form.querySelector('[type=submit]'),sent=false;if(pending&&pending.unknown){notice('上次保存结果未知，请先刷新核对，草稿仍保留。');return;}button.disabled=true;++epoch;
    try {
      var body=eventBody(form);if(editing){body.expected_version=editing.version;body.cancelled=false;}
      var fingerprint=JSON.stringify(body);
      if(pending&&pending.fingerprint!==fingerprint)throw new Error('上次保存结果未知，请先刷新核对；若未保存，点击“新建另一条”后再提交新内容');
      if(!pending)pending={fingerprint:fingerprint,id:crypto.randomUUID()};body.request_id=pending.id;
      sent=true;var result=await api('/api/work/calendar'+(editing?'/'+encodeURIComponent(editing.series_id||editing.id):''),body,editing?'PUT':'POST');
      resetEvent();notice((result.conflicts||[]).length?'已保存。与 '+result.conflicts.length+' 条日程时间重叠，请核对安排。':'日程已保存，可在其他设备查看。');await loadCalendar();
    }catch(error){if(sent&&pending)pending.unknown=true;notice(error.message+(sent?'；请先刷新核对，草稿仍保留。':''));}finally{button.disabled=false;}
  }
  var portfolioVersion=0, portfolioReady=false, portfolioDirty=false, portfolioUnknown=false, previewEpoch=0, portfolioEpoch=0, previewPositions=null, valuationEpoch=0;
  function portfolioNotice(value){var el=$('portfolioNotice');if(el)el.textContent=value||'';}
  function currencySelect(value){var select=node('select');['CNY','USD','HKD'].forEach(function(code){var option=node('option',code);option.value=code;select.append(option);});select.value=value||'CNY';return select;}
  function inputInstant(input){var date=new Date(input);if(!input||!Number.isFinite(date.getTime()))throw new Error('请选择有效的日期和时间');return date.toISOString();}
  function positionTimeUI(){var use=$('useObservedPriceTime').checked;document.querySelectorAll('#positions .price-time').forEach(function(label){label.hidden=use;label.querySelector('input').required=!use;});}
  function invalidatePreview(){previewEpoch++;previewPositions=null;var result=$('importResult');if(result)result.replaceChildren();if($('importApply'))$('importApply').hidden=true;}
  function futuresEquityUI(){var has=Array.from($('positions').children).some(function(row){return row.querySelector('[name=asset_class]').value==='futures';}),label=$('futuresEquityLabel'),basis=$('futuresEquityBasisLabel');if(label)label.hidden=!field($('portfolioForm'),'futures_equity').value&&!has;if(basis)basis.hidden=!has;}
  function positionRow(value){
    var isNew=!value;value=value||{};var row=node('details',undefined,'position-row'),summary=node('summary'),fields=node('div',undefined,'position-fields');row.open=isNew;row.append(summary,fields);
    var typeLabel=node('label','类型'),type=node('select');type.name='asset_class';[['security','证券'],['futures','期货']].forEach(function(pair){var o=node('option',pair[1]);o.value=pair[0];type.append(o);});type.value=value.asset_class||'security';typeLabel.append(type);fields.append(typeLabel);
    [['标的代码','asset_id'],['数量（期货为手数）','quantity'],['价格','price']].forEach(function(spec){var label=node('label',spec[0]),input=node('input');input.name=spec[1];input.value=value[spec[1]]||'';input.required=true;if(spec[1]==='asset_id')input.maxLength=40;label.append(input);fields.append(label);});
    var label=node('label','币种（期货仅支持 CNY）'),select=currencySelect(value.currency);select.name='currency';label.append(select);fields.append(label);
    var sideLabel=node('label','方向','futures-field'),side=node('select');side.name='side';[['long','多'],['short','空']].forEach(function(pair){var o=node('option',pair[1]);o.value=pair[0];side.append(o);});side.value=value.side||'long';sideLabel.append(side);fields.append(sideLabel);
    var multiplierLabel=node('label','合约乘数（必填）','futures-field'),multiplier=node('input');multiplier.name='multiplier';multiplier.inputMode='decimal';multiplier.value=value.multiplier||'';multiplierLabel.append(multiplier);fields.append(multiplierLabel);
    var timeLabel=node('label','价格时间','price-time'),time=node('input');time.type='datetime-local';time.name='price_as_of';if(value.price_as_of)time.value=localInput(value.price_as_of);timeLabel.append(time);fields.append(timeLabel);
    var advanced=node('details',undefined,'wide');advanced.append(node('summary','来源与期货规则（可选）'));var grid=node('div',undefined,'form-grid');
    [['公开 HTTPS 来源网址','source_url','url',false],['经纪商保证金比例（%）','broker_margin_rate_pct','text',true],['规则核对时间','terms_as_of','datetime-local',true],['最后交易日','last_trading_date','date',true]].forEach(function(spec){var l=node('label',spec[0],spec[3]?'futures-field':''),i=node('input');i.name=spec[1];i.type=spec[2];i.value=value[spec[1]]?(spec[2]==='datetime-local'?localInput(value[spec[1]]):value[spec[1]]):'';l.append(i);grid.append(l);});advanced.append(grid);fields.append(advanced);
    function updateSummary(){summary.textContent=(type.value==='futures'?'期货':'证券')+' · '+(row.querySelector('[name=asset_id]').value||'新持仓')+' · 数量 '+(row.querySelector('[name=quantity]').value||'—')+' / 价格 '+(row.querySelector('[name=price]').value||'—');}
    row.addEventListener('input',updateSummary);row.addEventListener('change',updateSummary);row.addEventListener('invalid',function(){row.open=true;},true);
    function updateType(){updateSummary();multiplier.required=type.value==='futures';row.querySelectorAll('.futures-field').forEach(function(el){el.hidden=type.value!=='futures';});futuresEquityUI();}type.onchange=updateType;
    var remove=node('button','移除','secondary');remove.type='button';remove.onclick=function(){row.remove();portfolioDirty=true;invalidatePreview();futuresEquityUI();};fields.append(remove);$('positions').append(row);updateType();positionTimeUI();
  }
  function renderRisk(risk){
    var root=$('riskResults');root.replaceChildren();if(!risk)return;
    var names={pass:'通过',breach:'超出限制',not_evaluated:'未评估'},labels={inputs:'输入完整性',freshness:'数据时效',currency:'币种一致性',net_equity:'净权益',concentration:'单一标的仓位',position_concentration:'单一标的仓位',max_position:'单一标的仓位',drawdown:'回撤',leverage:'杠杆',futures_equity:'期货账户权益',futures_terms:'期货合约规则',futures_margin:'期货保证金',futures_delivery:'期货交割日期',market_coverage:'行情覆盖',futures_equity_basis:'期货权益基准',market_recency:'行情时效',market_dates:'行情日期',broker_terms_verification:'经纪商规则核验'};
    root.append(node('h3','风险检查 · '+(names[risk.overall]||'未知')));if(risk.input_basis==='unchanged_snapshot_with_observed_market_prices_estimate')root.append(node('p','按原持仓与资金不变估算，不是实际账户余额。','muted'));if(risk.reference_check&&Array.isArray(risk.reference_check.breaches)&&risk.reference_check.breaches.length)root.append(node('p','原快照有 '+risk.reference_check.breaches.length+' 项超限；本次资料不完整，不能据此判断风险已改善。'));
    var details=node('details'),summary=node('summary','检查详情');details.append(summary);
    (risk.rules||[]).forEach(function(rule){var item=node('div',undefined,'risk-rule');item.append(node('strong',(labels[rule.id]||String(rule.id))+' · '+(names[rule.status]||'未知')),node('p',rule.reason||''));if(rule.observed!==null&&rule.observed!==undefined||rule.limit!==null&&rule.limit!==undefined)item.append(node('p','观察值 '+String(rule.observed??'未提供')+' · 限制 '+String(rule.limit??'未提供')+' '+String(rule.unit||''),'muted'));if(rule.status==='pass')details.append(item);else root.append(item);});
    (risk.assumptions||[]).forEach(function(text){details.append(node('p',String(text),'muted'));});details.append(node('p','偏好版本 '+String(risk.profile_version??'未知')+' · 持仓版本 '+String(risk.portfolio_version??'未知'),'muted'));root.append(details);
    if((risk.scenarios||[]).length){var scenarios=node('details');scenarios.append(node('summary','期货情景详情'));risk.scenarios.forEach(function(scenario){scenarios.append(node('p','不利变动 '+String(scenario.adverse_move_pct)+'% · 损失 '+String(scenario.futures_loss)+' · 剩余权益 '+String(scenario.remaining_futures_equity??'未提供')+' '+String(scenario.unit||'')+' · 估算保证金 '+String(scenario.estimated_margin??'未提供')+' · 保证金缺口 '+String(scenario.margin_shortfall??'未提供')+' · '+String(scenario.assumption||'')));});root.append(scenarios);}
  }
  async function refreshValuation(version){
    var ticket=++valuationEpoch, readEpoch=portfolioEpoch, draftEpoch=previewEpoch;
    portfolioNotice('正在核对已保存持仓的行情…');
    try{
      var value=await api('/api/preferences/portfolio/reprice',{expected_version:version},'POST');await identify();
      if(ticket!==valuationEpoch||readEpoch!==portfolioEpoch||draftEpoch!==previewEpoch||portfolioDirty||portfolioUnknown||portfolioVersion!==version||!$('riskResults'))return;
      if(!value||value.portfolio_version!==version||!value.risk||!Array.isArray(value.positions)||value.positions.length>100)throw new Error('对价结果不完整');
      renderRisk(value.risk);var root=$('riskResults'),details=node('details');details.append(node('summary','行情依据'));
      var reasons={stale_cache:'来源仅有过期缓存',market_time_unknown:'行情时间不明',instrument_or_basis_unknown:'标的或报价口径不明',invalid_price:'价格无效',market_time_out_of_range:'行情时间过旧或在未来',observation_not_newer_than_reference:'行情未晚于原参考时点',concrete_contract_price_missing:'未取得该具体合约结算价',instrument_not_covered:'该标的或币种暂未覆盖',source_unavailable:'来源暂不可用',source_timeout:'来源超时',request_budget:'本次查询时间已用完',report_not_found:'对应日期报告未找到',access_denied:'来源拒绝访问',http_error:'来源请求未完成',network_error:'来源连接未完成',invalid_source_data:'来源内容不完整'};
      value.positions.forEach(function(p){var text=String(p.asset_id||'未标明')+' · ';if(p.status==='collected'){text+=String(p.price)+' · '+(p.basis==='exchange_settlement'?'具体合约结算价':p.basis==='daily_close'?'日收盘价':'报价')+' · '+(p.precision==='date'?String(p.data_as_of):displayTime(p.price_as_of))+' · '+({tencent:'腾讯财经',akshare:'新浪日线适配器',baostock:'BaoStock',exchange_daily:'交易所日报'}[p.source]||'来源未标明');}else text+=(reasons[p.reason]||'行情未取得');details.append(node('p',text));if(p.source_url){try{var url=new URL(p.source_url);if(url.protocol==='https:'&&!url.username&&!url.password){var link=node('a','查看来源');link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';details.append(link);}}catch(_){}}});
      root.append(details);portfolioNotice(value.status==='partial'?'部分行情未取得；已有资料已保留，组合风险未完整评估。':'行情对价为估算，原持仓记录未改动。');
    }catch(error){if(ticket===valuationEpoch&&readEpoch===portfolioEpoch&&draftEpoch===previewEpoch&&!portfolioDirty)portfolioNotice('行情对价未完成：'+error.message+'；仍显示原快照检查。');}
  }
  async function previewImport(){
    var ticket=++previewEpoch;previewPositions=null;$('importResult').replaceChildren();$('importApply').hidden=true;$('importNotice').textContent='正在解析…';
    try{var form=$('portfolioForm'),text=$('importText').value;if(new TextEncoder().encode(text).length>65536)throw new Error('文本超过 64 KiB');var value=await api('/api/preferences/portfolio/import-preview',{csv_text:text,currency:field(form,'currency').value,price_as_of:field(form,'observed_at').value?inputInstant(field(form,'observed_at').value):null},'POST');await identify();if(ticket!==previewEpoch||!$('importDetails').open)return;if(!Array.isArray(value.positions)||!value.counts)throw new Error('预览结果不完整');previewPositions=value.positions;var table=node('table'),head=node('tr'),keys=['asset_class','asset_id','quantity','price','currency','price_as_of','source_url','side','multiplier','broker_margin_rate_pct','terms_as_of','last_trading_date'];var headings=['类型','代码','数量','价格','币种','价格时间','来源','方向','合约乘数','保证金比例（%）','规则核对时间','最后交易日'];keys.forEach(function(k,i){head.append(node('th',headings[i]));});table.append(head);value.positions.forEach(function(position){var tr=node('tr');keys.forEach(function(k){tr.append(node('td',position[k]==null?'':String(position[k])));});table.append(tr);});$('importResult').append(table);$('importNotice').textContent='共 '+value.counts.total+' 条：证券 '+value.counts.security+'，期货 '+value.counts.futures+'。'+(value.ignored_columns?'有 '+String(value.ignored_columns)+' 列未使用。':'')+'应用将替换当前未保存的持仓草稿。';$('importApply').hidden=false;
    }catch(error){if(ticket===previewEpoch)$('importNotice').textContent=error.message;}
  }
  async function loadPortfolio(){
    invalidatePreview();++valuationEpoch;var ticket=++portfolioEpoch;portfolioReady=false;$('portfolioForm').querySelector('[type=submit]').disabled=true;portfolioNotice('正在读取…');
    try{await identify();var value=await api('/api/preferences/portfolio');await identify();if(ticket!==portfolioEpoch)return;var portfolio=value.portfolio;if(!portfolio)throw new Error('读取结果不完整');var form=$('portfolioForm');portfolioVersion=portfolio.version||0;['currency','observed_at','cash','debt','equity_peak','peak_as_of','net_flows_since_peak','futures_equity'].forEach(function(key){field(form,key).value=portfolio[key]===undefined||portfolio[key]===null?(key==='currency'?'CNY':key==='cash'||key==='debt'||key==='net_flows_since_peak'?'0':''):['observed_at','peak_as_of'].includes(key)?localInput(portfolio[key]):portfolio[key];});field(form,'share_with_agent').checked=!!portfolio.share_with_agent;field(form,'allow_market_pricing').checked=!!portfolio.allow_market_pricing;field(form,'futures_equity_marked').checked=portfolio.futures_equity_basis==='marked_to_reference_prices';$('positions').replaceChildren();$('useObservedPriceTime').checked=!(portfolio.positions||[]).some(function(position){return position.price_as_of&&portfolio.observed_at&&Date.parse(position.price_as_of)!==Date.parse(portfolio.observed_at);});(portfolio.positions||[]).forEach(positionRow);if(!portfolio.observed_at)field(form,'observed_at').value=localInput(new Date());positionTimeUI();futuresEquityUI();renderRisk(value.risk);portfolioDirty=false;portfolioUnknown=false;portfolioReady=true;form.querySelector('[type=submit]').disabled=false;$('portfolioVersion').textContent=portfolio.configured?'已保存 · 版本 '+portfolioVersion:'尚未设置';portfolioNotice('');if(portfolio.allow_market_pricing)refreshValuation(portfolioVersion);}
    catch(error){if(ticket===portfolioEpoch)portfolioNotice(error.message+'；请重新读取。');}
  }
  async function savePortfolio(event){
    event.preventDefault();if(!portfolioReady||portfolioUnknown){portfolioNotice('请先重新读取并核对，再保存。');return;}var form=event.currentTarget,button=form.querySelector('[type=submit]'),sent=false,saveTicket=previewEpoch;++valuationEpoch;button.disabled=true;
    try{var positions=Array.from($('positions').children).map(function(row){var obj={};var type=row.querySelector('[name=asset_class]').value;var allowed=['asset_id','quantity','price','currency','price_as_of','source_url'];if(type==='futures')allowed=allowed.concat(['side','multiplier','broker_margin_rate_pct','terms_as_of','last_trading_date']);allowed.forEach(function(key){obj[key]=row.querySelector('[name='+key+']').value.trim();});if(type==='futures'){obj.asset_class='futures';if(obj.currency!=='CNY')throw new Error('期货币种仅支持 CNY，请手动核对并选择');if(!/^(?:\d+)(?:\.\d+)?$/.test(obj.multiplier)||Number(obj.multiplier)<=0)throw new Error('请填写有效的期货合约乘数');if(!/^\d+$/.test(obj.quantity))throw new Error('期货数量须为整数手数');['broker_margin_rate_pct','terms_as_of','last_trading_date'].forEach(function(key){if(!obj[key])obj[key]=null;});if(obj.terms_as_of)obj.terms_as_of=inputInstant(obj.terms_as_of);}if(!/^(?:\d+)(?:\.\d+)?$/.test(obj.quantity)||Number(obj.quantity)<=0||!/^(?:\d+)(?:\.\d+)?$/.test(obj.price)||Number(obj.price)<=0)throw new Error('数量和价格须为正十进制');obj.price_as_of=inputInstant($('useObservedPriceTime').checked?field(form,'observed_at').value:obj.price_as_of);if(obj.source_url){var u=new URL(obj.source_url);if(u.protocol!=='https:'||u.username||u.password||u.hostname==='localhost'||!u.hostname.includes('.'))throw new Error('来源须为公开 HTTPS 网址');}else obj.source_url=null;return obj;});var observed=inputInstant(field(form,'observed_at').value);var cash=field(form,'cash').value.trim(),debt=field(form,'debt').value.trim();if(!/^\d+(?:\.\d+)?$/.test(cash)||!/^\d+(?:\.\d+)?$/.test(debt))throw new Error('现金和负债须为非负十进制');var peak=field(form,'equity_peak').value.trim(),peakTime=field(form,'peak_as_of').value.trim(),flows=field(form,'net_flows_since_peak').value.trim()||'0';if(peak&&!/^(?:\d+)(?:\.\d+)?$/.test(peak)||peak&&Number(peak)<=0)throw new Error('权益高点须为正十进制');if(Boolean(peak)!==Boolean(peakTime))throw new Error('权益高点与高点时间须同时填写');if(peakTime)peakTime=inputInstant(peakTime);if(!/^[+-]?\d+(?:\.\d+)?$/.test(flows))throw new Error('净入金须为带符号十进制');if(field(form,'futures_equity_marked').checked&&positions.some(function(p){return p.asset_class==='futures';})&&(!field(form,'futures_equity').value.trim()||positions.some(function(p){return p.asset_class==='futures'&&p.price_as_of!==observed;})))throw new Error('请填写参考时点的期货账户权益，并让期货参考价格时间与记录时间一致；也可取消该基准确认');sent=true;var value=await api('/api/preferences/portfolio',{expected_version:portfolioVersion,currency:field(form,'currency').value,observed_at:observed,cash:cash,debt:debt,futures_equity:field(form,'futures_equity').value.trim()||null,share_with_agent:field(form,'share_with_agent').checked,allow_market_pricing:field(form,'allow_market_pricing').checked,futures_equity_basis:field(form,'futures_equity_marked').checked?'marked_to_reference_prices':'unverified',positions:positions,equity_peak:peak||null,peak_as_of:peakTime||null,net_flows_since_peak:flows},'PUT');await identify();portfolioVersion=value.portfolio.version;if(saveTicket===previewEpoch){portfolioDirty=false;if($('importNotice'))$('importNotice').textContent='';Array.from($('positions').children).forEach(function(row){row.open=false;});}$('portfolioVersion').textContent='已保存 · 版本 '+portfolioVersion;renderRisk(value.risk);portfolioNotice(saveTicket===previewEpoch?'已保存。':'已保存提交时的内容；之后的修改仍未保存。');if(saveTicket===previewEpoch&&value.portfolio.allow_market_pricing)refreshValuation(portfolioVersion);}
    catch(error){if(sent&&![400,401,403,409,413,422,429].includes(error.status)){portfolioUnknown=true;portfolioNotice(error.message+'；请重新读取并核对结果，当前输入会保留至重新读取。');}else{if([401,403,409].includes(error.status))portfolioReady=false;portfolioNotice(error.message+'；当前输入已保留。'+(error.status===409?'请先重新读取并核对版本。':''));}}finally{button.disabled=false;}
  }
  function metric(label,value){var box=node('div',undefined,'metric');box.append(node('span',label),node('strong',value===null||value===undefined?'待核查':String(value)));return box;}
  async function loadFutures(event){
    if(event)event.preventDefault();var ticket=++epoch,form=$('futuresForm'),button=form.querySelector('[type=submit]');button.disabled=true;
    $('futuresResult').replaceChildren();notice('正在读取研究资料…');
    try {
      var value=await api('/api/futures/'+$('futureSymbol').value+'/snapshot');if(ticket!==epoch)return;
      var hasSeries=value.series_status!=='unavailable'&&value.latest&&typeof value.latest.close==='number'&&Number.isFinite(value.latest.close),warnings=[];
      if(!hasSeries)warnings.push('连续日线暂未取得，技术指标未计算；已取得的其他资料仍可查看。');
      if(hasSeries&&value.calendar_age_days>4)warnings.push('最新日线距当前已有 '+value.calendar_age_days+' 个日历日，请核查停市或来源延迟。');
      if(value.date_alignment==='mixed')warnings.push('资料日期不同，请按各自日期比较。');notice(warnings.join(' '));
      var root=$('futuresResult'),overview=node('section',undefined,'card');overview.append(node('h2',value.name+(hasSeries?' · 连续日线':' · 研究数据')));
      overview.append(node('p',(hasSeries?'日线日期 '+value.data_as_of+' · ':'')+'采集 '+value.collected_at+(value.cached?' · 缓存数据':''),'muted'));
      if(hasSeries){var metrics=node('div',undefined,'metrics');metrics.append(metric('最近收盘',value.latest.close),metric('日变动（%）',value.technical.change_pct),metric('20日均值',value.technical.sma20));overview.append(metrics,node('p',value.notice,'muted'));}root.append(overview);
      if(hasSeries){var tech=node('section',undefined,'card');tech.append(node('h2','技术与成交持仓'));var values=node('div',undefined,'metrics');values.append(metric('60日均值',value.technical.sma60),metric('成交量',value.positioning.volume),metric('持仓量变化',value.positioning.open_interest_change));tech.append(values,node('p',value.positioning.notice,'muted'));root.append(tech);}
      var exchange=value.exchange_daily||{},curve=exchange.curve||{},members=exchange.member_positioning||value.member_positioning||{};
      var display=function(v){return v===null||v===undefined||v===''?'未提供':String(v);};
      var sourceLink=function(url,label){try{var parsed=new URL(url);if(parsed.protocol!=='https:'||parsed.username||parsed.password)return null;var a=node('a',label);a.href=parsed.href;a.target='_blank';a.rel='noopener noreferrer';return a;}catch(_){return null;}};
      var makeTable=function(headers,rows){var wrap=node('div',undefined,'table-scroll'),table=node('table'),head=node('thead'),hr=node('tr'),body=node('tbody');headers.forEach(function(h){hr.append(node('th',h));});head.append(hr);rows.forEach(function(row){var tr=node('tr');row.forEach(function(cell){tr.append(node('td',display(cell)));});body.append(tr);});table.append(head,body);wrap.append(table);return wrap;};
      var market=node('section',undefined,'card');market.append(node('h2','期限结构与会员持仓'));
      if(exchange.status==='collected'){
        var exchangeDate=exchange.date_provenance==='request_parameter'?'查询日期 ':'交易所日期 ';
        market.append(node('h3','期限结构'),node('p',exchangeDate+display(exchange.data_as_of)+' · 当日成交量最高合约 '+display(exchange.active_contract)+'（非交易建议）'+(exchange.transport_authenticated===false?' · 公开来源未独立核验':''),'muted'));
        if(curve.status==='collected'){
          var shapes={contango:'远月升水',backwardation:'远月贴水',flat:'平水'};
          market.append(node('p',display(curve.near_contract)+'（结算 '+display(curve.near_settlement)+'） → '+display(curve.far_contract)+'（结算 '+display(curve.far_settlement)+'）'),node('p','远月减近月：'+display(curve.far_minus_near)+(exchange.quote_unit?' '+exchange.quote_unit:'')+' · '+(shapes[curve.shape]||'结构未知')));
        }else market.append(node('p',curve.status==='insufficient'?'可比较的合约不足，期限结构未计算。':'期限结构状态未知。','muted'));
        var contracts=Array.isArray(exchange.contracts)?exchange.contracts:[],contractDetails=node('details');contractDetails.append(node('summary','合约明细与原表'));
        if(contracts.length)contractDetails.append(makeTable(['合约','交割月','收盘','结算','成交量','持仓量'],contracts.map(function(c){return [c.contract,c.delivery_month,c.close,c.settlement,c.volume,c.open_interest];})));else contractDetails.append(node('p','未提供合约明细。','muted'));
        var dailySource=sourceLink(exchange.source_url,'查看交易所原表 ↗');if(dailySource)contractDetails.append(dailySource);if(exchange.transport_authenticated===false&&exchange.notice)contractDetails.append(node('p',exchange.notice,'muted'));market.append(contractDetails);
      }else market.append(node('h3','期限结构'),node('p',exchange.error_code==='automated_access_restricted'?'交易所限制自动读取，合约资料暂未取得；已取得的其他资料仍可查看。':exchange.status==='not_covered'?'该品种的交易所合约数据尚未覆盖。':exchange.status==='unavailable'?'未取得交易所合约数据。':'交易所合约数据状态未知。','muted'));
      market.append(node('h3','会员持仓'));
      if(members.status==='collected'){
        market.append(node('p','交易所日期 '+display(members.data_as_of)+' · 合约 '+display(members.contract),'muted'));
        var sides=members.sides||{},labels={volume:'成交量',long:'多头持仓',short:'空头持仓'};
        ['volume','long','short'].forEach(function(key){var side=sides[key];if(!side){market.append(node('p',labels[key]+'：未提供','muted'));return;}market.append(node('p',labels[key]+'：前 '+display(side.ranked_members)+' 名合计 '+display(side.quantity_sum)+'，增减 '+display(side.change_sum)));});
        market.append(node('p',members.notice||'会员排名为交易所披露的分项数据，多空排名差不代表净资金流。','muted'));
        var memberDetails=node('details');memberDetails.append(node('summary','会员明细与原表'));
        ['volume','long','short'].forEach(function(key){var side=sides[key],rows=side&&Array.isArray(side.members)?side.members:[];memberDetails.append(node('h3',labels[key]));if(rows.length)memberDetails.append(makeTable(['排名','会员','数量','增减'],rows.map(function(m){return [m.rank,m.member,m.quantity,m.change];})));else memberDetails.append(node('p','未提供会员明细。','muted'));});
        var memberSource=sourceLink(members.source_url,'查看交易所原表 ↗');if(memberSource)memberDetails.append(memberSource);market.append(memberDetails);
      }else{var memberStatus={unavailable:'未取得会员持仓数据。',not_covered:'该品种的会员持仓数据尚未覆盖。',not_reported:'交易所未披露该合约的会员持仓。'};market.append(node('p',memberStatus[members.status]||'会员持仓数据状态未知。','muted'));if(members.notice)market.append(node('p',members.notice,'muted'));}
      var dates=value.contract_dates||{},params=value.exchange_parameters||{},terms=node('details');terms.append(node('summary','合约日期与交易所参数'));
      [[dates,'合约日期'],[params,'交易参数']].forEach(function(pair){var report=pair[0],label=pair[1];terms.append(node('h3',label));
        if(report.status!=='collected'){terms.append(node('p',report.status==='not_covered'?'该品种尚未覆盖。':'本次未取得。','muted'));return;}
        terms.append(node('p','资料日期 '+display(report.data_as_of),'muted'));var rows=Array.isArray(report.contracts)?report.contracts:[];
        if(label==='合约日期')terms.append(makeTable(['合约','上市日','列示到期日','交割开始','交割结束'],rows.map(function(c){return [c.contract,c.listed_on,c.listed_expiry_date,c.delivery_start,c.delivery_end];})));
        else terms.append(makeTable(['合约','一般买保证金（%）','一般卖保证金（%）','套保买保证金（%）','套保卖保证金（%）','涨停幅度（%）','跌停幅度（%）','另有特殊限制'],rows.map(function(c){return [c.contract,c.spec_long_margin_pct,c.spec_short_margin_pct,c.hedge_long_margin_pct,c.hedge_short_margin_pct,c.upper_limit_pct,c.lower_limit_pct,c.additional_limit_notice_required?'须查原公告':'未报告'];})));
        terms.append(node('p',report.notice,'muted'));var link=sourceLink(report.reference_url,'查看官方参数页面 ↗');if(link)terms.append(link);
      });
      if(dates.status==='collected'||params.status==='collected')market.append(node('p','已取得'+(dates.status==='collected'?'合约日期':'')+(dates.status==='collected'&&params.status==='collected'?'及':'')+(params.status==='collected'?'交易所参数':'')+'；实际经纪商要求与交割资格仍须核对。','muted'));
      else market.append(node('p','合约日期与交易参数暂缺。','muted'));
      market.append(terms);root.append(market);
      var fund=node('section',undefined,'card');
      fund.append(node('h2','基本面'));
      var receipt=value.warehouse_receipt;
      if(receipt && receipt.status==='collected'){
        fund.append(node('h3','交易所仓单 · '+receipt.table),node('p',(receipt.date_provenance==='request_parameter'?'查询日期 ':'')+receipt.data_as_of+' · '+receipt.row+' '+receipt.quantity+' '+receipt.unit+'，较上一期变化 '+receipt.change+' '+receipt.unit));
        var reportLink=sourceLink(receipt.source_url,'查看当日仓单原表 ↗');
        if(reportLink)fund.append(reportLink);
        fund.append(node('p',receipt.notice||'仅覆盖该表已登记仓单，社会库存仍待核查。','muted'));
      }else if(receipt){
        fund.append(node('p',receipt.error_code==='automated_access_restricted'?'交易所限制自动读取，仓单资料暂未取得；已取得的其他资料仍保留。':receipt.status==='not_covered'?'该品种的仓单来源尚未接入。':'未取得该日期的官方仓单，已取得的其他资料仍保留。','muted'));
      }
      fund.append(node('h3','待补证的关键问题'));value.fundamentals.forEach(function(item){var p=node('p',item.topic+'：尚未采集 · '),a=node('a','查看官方来源 ↗');a.href=item.official_url;a.target='_blank';a.rel='noopener noreferrer';p.append(a);fund.append(p);});value.gaps.forEach(function(gap){fund.append(node('p',gap,'muted'));});root.append(fund);
      if(hasSeries){var source=node('p','行情来源：'),link=node('a',value.source.name+' ↗');link.href=value.source.url;link.target='_blank';link.rel='noopener noreferrer';source.append(link);root.append(source);}
    }catch(error){notice(error.message);}finally{button.disabled=false;}
  }
  async function init(){
    (embeddedFutures ? futuresRoot : document).querySelectorAll('form').forEach(function(form){
      form.onsubmit=function(event){event.preventDefault();notice('页面尚未准备好，请刷新后再试。');};
      if(page!=='futures')form.addEventListener('input',function(){dirty=true;});
    });
    if(page==='preferences'){
      $('profileForm').onsubmit=saveProfile; $('portfolioForm').onsubmit=savePortfolio; $('portfolioForm').addEventListener('input',function(event){if(event.target.id==='importFile')return;portfolioDirty=true;if(!portfolioReady){portfolioEpoch++;portfolioNotice('输入已更改；请重新读取并核对后保存。');}invalidatePreview();futuresEquityUI();}); $('portfolioForm').addEventListener('change',function(){portfolioDirty=true;invalidatePreview();futuresEquityUI();}); $('addPosition').onclick=function(){positionRow();portfolioDirty=true;invalidatePreview();}; $('useObservedPriceTime').onchange=positionTimeUI; $('portfolioReload').onclick=function(){if((!portfolioDirty&&!portfolioUnknown)||confirm('重新读取将替换未保存的持仓输入，继续吗？'))loadPortfolio();}; $('portfolioDetails').addEventListener('toggle',function(){if(!this.open){invalidatePreview();return;}if(!portfolioReady&&!portfolioUnknown)loadPortfolio();});
      if($('importDetails')){ $('importDetails').addEventListener('toggle',function(){if(!this.open)invalidatePreview();});$('importPreview').onclick=previewImport;$('importApply').onclick=function(){if(!previewPositions)return;var positions=previewPositions;invalidatePreview();$('positions').replaceChildren();var form=$('portfolioForm'),observed=field(form,'observed_at').value;$('useObservedPriceTime').checked=!positions.some(function(p){return p.price_as_of&&(!observed||Date.parse(p.price_as_of)!==Date.parse(inputInstant(observed)));});positions.forEach(positionRow);portfolioDirty=true;futuresEquityUI();$('importNotice').textContent='已应用到草稿；请保存并检查。';};$('importFile').onchange=async function(event){event.stopPropagation();invalidatePreview();var file=this.files[0];if(!file)return;if(file.size>65536||!file.name.toLowerCase().endsWith('.csv')){$('importNotice').textContent='请选择不超过 64 KiB 的 CSV 文件。';return;}var ticket=previewEpoch;try{var bytes=await file.arrayBuffer();var text=new TextDecoder('utf-8',{fatal:true}).decode(bytes);if(ticket===previewEpoch){$('importText').value=text;$('importNotice').textContent='文件已读取，请解析预览。';}}catch(error){if(ticket===previewEpoch)$('importNotice').textContent='文件读取失败，请确认文件为有效 UTF-8 编码。';}}; }
      $('profileForm').querySelector('[type=submit]').disabled=true;
      $('profileReload').onclick=function(){if(!dirty||confirm('重新读取将替换未保存的偏好，继续吗？'))loadProfile().catch(function(e){notice(e.message);});};
    }
    document.querySelectorAll('.rail nav a').forEach(function(a){if(new URL(a.href).pathname===location.pathname)a.setAttribute('aria-current','page');});
    try {
      await identify();
      if(page==='preferences')await loadProfile();
      if(page==='calendar'){
        $('calendarDay').value=dateInput(new Date());var initial=new Date();initial.setMinutes(0,0,0);initial.setHours(initial.getHours()+1);field($('eventForm'),'start').value=localInput(initial);field($('eventForm'),'end').value=localInput(new Date(initial.getTime()+3600000));$('calendarZone').textContent='时间按 '+zone+' 显示与保存';
        $('calendarLoad').onclick=function(){loadCalendar().catch(function(e){notice(e.message);});};$('eventForm').onsubmit=saveEvent;
        field($('eventForm'),'frequency').onchange=recurrenceUI;field($('eventForm'),'endType').onchange=recurrenceUI;recurrenceUI();
        $('eventNew').onclick=function(){if((!pending&&!dirty)||confirm('当前有未保存内容或上次结果未知，请先核对列表。仍要另建一条吗？'))resetEvent();};
        ['start','end'].forEach(function(name){field($('eventForm'),name).onchange=function(){try{resolveTime($('eventForm'),name,true);notice('');}catch(error){notice(error.message);}};});
        await loadCalendar();
      }
      if(page==='futures'){var symbol=embeddedFutures ? null : new URLSearchParams(location.search).get('symbol');if(['RB','CU','AU','M','SC'].includes(symbol))$('futureSymbol').value=symbol;$('futuresForm').onsubmit=loadFutures;futuresReady=true;}
    }catch(error){notice(error.message);}
  }
  window.addEventListener('beforeunload',function(event){if(dirty||pending||portfolioDirty||portfolioUnknown){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pageshow',function(event){if(event.persisted)location.reload();});
  document.addEventListener('visibilitychange',async function(){
    if(embeddedFutures&&!futuresActive)return;
    if(document.hidden){$('main').style.visibility='hidden';return;}
    try{await identify();$('main').style.visibility='';}catch(error){notice(error.message);$('main').style.visibility='';}
  });
  window.PrismHub={api:api,resolveTime:resolveTime,loadCalendar:loadCalendar,loadFutures:loadFutures,
    activateFutures:async function(){
      futuresActive=true;
      if(!futuresReady){if(!futuresInitializing)futuresInitializing=init().finally(function(){futuresInitializing=null;});await futuresInitializing;}
      else try{await identify();}catch(error){notice(error.message);}
      futuresRoot.style.visibility='';
    },
    suspendFutures:function(){futuresActive=false;}
  };
  if(!embeddedFutures)init();
}());
