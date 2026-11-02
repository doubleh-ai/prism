/* Account-bound forms. Private values remain in memory, never localStorage. */
(function () {
  'use strict';
  var user, epoch = 0, profileVersion = 0, calendarRows = [], editing = null, pending = null, dirty = false, profileReady = false;
  var $ = function (id) { return document.getElementById(id); };
  var page = document.body.dataset.page;
  var zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  function node(tag, text, cls) { var el = document.createElement(tag); if (text !== undefined) el.textContent = text; if (cls) el.className = cls; return el; }
  function notice(text) { $('notice').textContent = text || ''; $('notice').hidden = !text; }
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
    if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '请求未完成，请刷新核对');
    return value;
  }
  function field(form, name) { return form.elements.namedItem(name); }
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
  function eventBody(form) { return {title:field(form,'title').value,description:field(form,'description').value,
    start_at:resolveTime(form,'start',false),end_at:resolveTime(form,'end',false),timezone:zone}; }
  function editEvent(row) {
    editing=row; pending=null; dirty=false; var form=$('eventForm');
    field(form,'title').value=row.title; field(form,'description').value=row.description;
    field(form,'start').value=localInput(row.start_at); field(form,'end').value=localInput(row.end_at);
    ['start','end'].forEach(function (name) { resolveTime(form,name,true,row[name+'_at']); });
    $('eventHeading').textContent='编辑日程 · 版本 '+row.version; form.scrollIntoView({block:'center',behavior:'smooth'});
  }
  function resetEvent() { editing=null; pending=null; dirty=false; $('eventForm').reset(); $('eventHeading').textContent='安排一件事'; ['startFold','endFold'].forEach(function (key) { field($('eventForm'),key).hidden=true; }); }
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
    calendarRows=value.events; var list=$('calendarList'); list.replaceChildren();
    var rows=value.events.map(function (row) { return {kind:'event',row:row,time:row.start_at}; }).concat(value.tasks.map(function(row){return {kind:'task',row:row,time:row.due_at};}));
    rows.sort(function(a,b){return a.time.localeCompare(b.time);});
    rows.forEach(function(item){
      var row=item.row, card=node('article'); card.append(node('h3',row.title));
      card.append(node('p',displayTime(item.time)+(item.kind==='event'?' — '+displayTime(row.end_at):' · 任务截止'),'muted'));
      if(item.kind==='event') {
        if(row.description) card.append(node('p',row.description));
        var edit=node('button','编辑','secondary'); edit.onclick=function(){if(!dirty||confirm('替换当前未保存的日程内容？'))editEvent(row);};card.append(edit);
        var cancel=node('button','取消日程','secondary'); cancel.onclick=async function(){
          if(!confirm('取消这条日程？已有记录会保留。'))return;
          cancel.disabled=true;
          try { await api('/api/work/calendar/'+row.id,{request_id:crypto.randomUUID(),expected_version:row.version,
            title:row.title,description:row.description,start_at:row.start_at,end_at:row.end_at,timezone:row.timezone,cancelled:true},'PUT');await loadCalendar(); }
          catch(error){notice(error.message+'；请刷新核对取消结果。');cancel.disabled=false;}
        };card.append(cancel);
        var link=node('a','导出至日历 ↗');link.href='/api/work/calendar/'+row.id+'.ics';card.append(link);
      } else {var taskLink=node('a','打开任务 ↗');taskLink.href='/work';card.append(taskLink);}
      list.append(card);
    });
    if(!rows.length)list.append(node('p','这一周还没有日程或到期任务。','muted'));
    if(value.truncated)list.append(node('p','记录超过显示上限，请改选起始日期查看后续记录。','notice'));
  }
  async function saveEvent(event) {
    event.preventDefault();var form=event.currentTarget,button=form.querySelector('[type=submit]');button.disabled=true;++epoch;
    try {
      var body=eventBody(form);if(editing){body.expected_version=editing.version;body.cancelled=false;}
      var fingerprint=JSON.stringify(body);
      if(pending&&pending.fingerprint!==fingerprint)throw new Error('上次保存结果未知，请先刷新核对；若未保存，点击“新建另一条”后再提交新内容');
      if(!pending)pending={fingerprint:fingerprint,id:crypto.randomUUID()};body.request_id=pending.id;
      var result=await api('/api/work/calendar'+(editing?'/'+editing.id:''),body,editing?'PUT':'POST');
      resetEvent();notice(result.conflicts.length?'已保存。与 '+result.conflicts.length+' 条日程时间重叠，请核对安排。':'日程已保存，可在其他设备查看。');await loadCalendar();
    }catch(error){notice(error.message);}finally{button.disabled=false;}
  }
  function metric(label,value){var box=node('div',undefined,'metric');box.append(node('span',label),node('strong',value===null||value===undefined?'待核查':String(value)));return box;}
  async function loadFutures(event){
    if(event)event.preventDefault();var ticket=++epoch,form=$('futuresForm'),button=form.querySelector('button');button.disabled=true;
    $('futuresResult').replaceChildren();notice('正在读取日线和计算指标…');
    try {
      var value=await api('/api/futures/'+$('futureSymbol').value+'/snapshot');if(ticket!==epoch)return;
      notice(value.calendar_age_days>4?'最新数据距当前已有 '+value.calendar_age_days+' 个日历日，请核查停市或来源延迟。':'');
      var root=$('futuresResult'),overview=node('section',undefined,'card');overview.append(node('h2',value.name+' · 连续日线'));
      overview.append(node('p','数据日期 '+value.data_as_of+' · 采集 '+value.collected_at+(value.cached?' · 5分钟内缓存':''),'muted'));
      var metrics=node('div',undefined,'metrics');metrics.append(metric('最近收盘',value.latest.close),metric('日变动（%）',value.technical.change_pct),metric('20日均值',value.technical.sma20));overview.append(metrics,node('p',value.notice,'muted'));root.append(overview);
      var tech=node('section',undefined,'card');tech.append(node('h2','技术与成交持仓'));var values=node('div',undefined,'metrics');values.append(metric('60日均值',value.technical.sma60),metric('成交量',value.positioning.volume),metric('持仓量变化',value.positioning.open_interest_change));tech.append(values,node('p',value.positioning.notice,'muted'));root.append(tech);
      var fund=node('section',undefined,'card');fund.append(node('h2','基本面 · 待补证的关键问题'));value.fundamentals.forEach(function(item){var p=node('p',item.topic+'：尚未采集 · '),a=node('a','查看官方来源 ↗');a.href=item.official_url;a.target='_blank';a.rel='noopener noreferrer';p.append(a);fund.append(p);});value.gaps.forEach(function(gap){fund.append(node('p',gap,'muted'));});var action=node('a','在对话中继续综合研究 ↗','button');action.href='/?futures='+value.symbol;fund.append(action);root.append(fund);
      var source=node('p','行情来源：'),link=node('a',value.source.name+' ↗');link.href=value.source.url;link.target='_blank';link.rel='noopener noreferrer';source.append(link);root.append(source);
    }catch(error){notice(error.message);}finally{button.disabled=false;}
  }
  async function init(){
    document.querySelectorAll('form').forEach(function(form){
      form.onsubmit=function(event){event.preventDefault();notice('页面尚未准备好，请刷新后再试。');};
      if(page!=='futures')form.addEventListener('input',function(){dirty=true;});
    });
    if(page==='preferences'){
      $('profileForm').onsubmit=saveProfile;
      $('profileForm').querySelector('[type=submit]').disabled=true;
      $('profileReload').onclick=function(){if(!dirty||confirm('重新读取将替换未保存的偏好，继续吗？'))loadProfile().catch(function(e){notice(e.message);});};
    }
    document.querySelectorAll('.rail nav a').forEach(function(a){if(new URL(a.href).pathname===location.pathname)a.setAttribute('aria-current','page');});
    try {
      await identify();
      if(page==='preferences')await loadProfile();
      if(page==='calendar'){
        $('calendarDay').value=dateInput(new Date());$('calendarZone').textContent='时间按 '+zone+' 显示与保存';
        $('calendarLoad').onclick=function(){loadCalendar().catch(function(e){notice(e.message);});};$('eventForm').onsubmit=saveEvent;
        $('eventNew').onclick=function(){if((!pending&&!dirty)||confirm('当前有未保存内容或上次结果未知，请先核对列表。仍要另建一条吗？'))resetEvent();};
        ['start','end'].forEach(function(name){field($('eventForm'),name).onchange=function(){try{resolveTime($('eventForm'),name,true);notice('');}catch(error){notice(error.message);}};});
        await loadCalendar();
      }
      if(page==='futures'){var symbol=new URLSearchParams(location.search).get('symbol');if(['RB','CU','AU','M','SC'].includes(symbol))$('futureSymbol').value=symbol;$('futuresForm').onsubmit=loadFutures;}
    }catch(error){notice(error.message);}
  }
  window.addEventListener('beforeunload',function(event){if(dirty||pending){event.preventDefault();event.returnValue='';}});
  window.addEventListener('pageshow',function(event){if(event.persisted)location.reload();});
  document.addEventListener('visibilitychange',async function(){
    if(document.hidden){$('main').style.visibility='hidden';return;}
    try{await identify();$('main').style.visibility='';}catch(error){notice(error.message);$('main').style.visibility='';}
  });
  window.PrismHub={api:api,resolveTime:resolveTime,loadCalendar:loadCalendar,loadFutures:loadFutures};
  init();
}());
