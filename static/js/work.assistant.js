(function(){
  'use strict';
  var $=function(id){return document.getElementById(id);};
  var state={user:null,epoch:0,busy:false,proposal:null,requestId:null,sentText:'',unknown:false,composing:false};
  function notice(text){$('assistantNotice').textContent=text||'';}
  function clearPrivate(){state.proposal=null;state.requestId=null;state.sentText='';state.unknown=false;state.composing=false;$('assistantText').value='';$('assistantDetails').replaceChildren();$('assistantPreview').hidden=true;}
  function button(id,visible){$(id).hidden=!visible;}
  function element(tag,text){var n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);return n;}
  async function call(path,body){var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},75000);try{var headers={Accept:'application/json'};if(body){headers['Content-Type']='application/json';headers['X-Requested-With']='XMLHttpRequest';headers['X-Prism-Account']=state.user;}var response=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal,headers:headers,body:body?JSON.stringify(body):undefined});var value;try{value=await response.json();}catch(_){throw new Error('invalid response');}if(!response.ok){var e=new Error(typeof value.detail==='string'?value.detail:'请求未完成');e.status=response.status;throw e;}return value;}finally{clearTimeout(timer);}}
  async function identify(){var me=await call('/api/me');if(!me||typeof me.user!=='string'||!me.user)throw new Error('无法核对账号');if(state.user&&me.user!==state.user){++state.epoch;state.user=null;clearPrivate();notice('账号已切换，请重新输入安排。');throw new Error('账号已切换');}state.user=me.user;return me.user;}
  function time(value,zone){var d=new Date(value);if(!Number.isFinite(d.getTime()))return '时间待核对';try{return new Intl.DateTimeFormat('zh-CN',{timeZone:zone,year:'numeric',month:'short',day:'numeric',weekday:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'shortOffset'}).format(d);}catch(_){return d.toISOString();}}
  function recurrence(value){
    if(!value)return '不重复';var names={daily:'每天',weekly:'每周',monthly:'每月'},days=['一','二','三','四','五','六','日'];
    var frequency=names[value.frequency]||'重复规则待核对',interval=Number(value.interval||1),selected=Array.isArray(value.weekdays)?value.weekdays.filter(function(day){return Number.isInteger(day)&&day>=0&&day<=6;}).map(function(day){return '周'+days[day];}):[];
    return frequency+(interval>1?'，间隔 '+interval:'')+(selected.length?'（'+selected.join('、')+'）':'')+(value.count?'，共 '+value.count+' 次':value.until?'，至 '+value.until:'');
  }
  function fieldValue(key,value,kind,zone){
    if(value===undefined)return '未提供';if(value===null||value==='')return '未设置';
    if(key==='start_at'||key==='end_at'||key==='due_at')return time(value,zone);
    if(key==='recurrence')return recurrence(value);
    if(key==='cancelled')return value?'已取消':'未取消';
    if(key==='status'){var statuses=kind==='update_project'?{active:'进行中',completed:'已完成',archived:'已归档'}:{todo:'待开始',doing:'进行中',done:'已完成',cancelled:'已取消'};return statuses[value]||'状态待核对';}
    if(key==='priority')return ({low:'较低',normal:'普通',high:'重要'})[value]||'优先级待核对';
    return typeof value==='string'||typeof value==='number'?String(value):'内容待核对';
  }
  function previewAction(action){
    var fields=action.fields||{},kind=action.kind||'',update=kind.indexOf('update_')===0,before=action.before||{},target=action.target||{},zone=target.timezone||before.timezone||fields.timezone,row=element('li');
    var types={create_task:'任务',create_event:'日程',create_project:'项目',update_task:'修改任务',update_project:'修改项目',update_event:before.recurrence?'修改整组重复日程':'修改日程',update_occurrence:'只调整这一次日程'};
    if(!types[kind])return {row:row,valid:false};
    row.append(element('strong',types[kind]+'：'+(update?(target.title||before.title||before.name||'未命名'):(fields.title||fields.name||'未命名'))));
    if(!update){if(fields.start_at)row.append(element('p',time(fields.start_at,fields.timezone)+' 至 '+time(fields.end_at,fields.timezone)));if(fields.due_at)row.append(element('p','截止：'+time(fields.due_at,fields.timezone)));if(fields.recurrence)row.append(element('p','重复：'+recurrence(fields.recurrence)));return {row:row,valid:true};}
    var names={title:'名称',name:'名称',description:'说明',status:'状态',priority:'优先级',due_at:'截止时间',start_at:'开始',end_at:'结束',timezone:'时区',recurrence:'重复',cancelled:'日程状态'},internal={request_id:true,expected_version:true,expected_series_version:true},valid=true,changed=0;
    Object.keys(fields).forEach(function(key){
      if(internal[key])return;
      if(key==='reset_exceptions'){if(fields[key]){row.append(element('p','将重置该系列的单次修改'));changed++;}return;}
      if(!Object.prototype.hasOwnProperty.call(names,key)){valid=false;return;}
      var original=before[key];if(key==='timezone'&&original===undefined)original=target.timezone;
      if(original===undefined){valid=false;return;}
      var oldText=fieldValue(key,original,kind,zone),newText=fieldValue(key,fields[key],kind,fields.timezone||zone);
      if(oldText===newText)return;changed++;row.append(element('p',names[key]+'：'+oldText+' → '+newText));
    });
    if(!changed)valid=false;return {row:row,valid:valid};
  }
  function render(proposal){state.proposal=proposal;state.unknown=false;var box=$('assistantDetails');box.replaceChildren();$('assistantPreview').hidden=false;['assistantConfirm','assistantModel','assistantCheck','assistantUndo'].forEach(function(id){button(id,false);});$('assistantModelHelp').hidden=true;$('assistantPreviewTitle').textContent='安排预览';
    if(proposal.state==='ready'){var actions=Array.isArray(proposal.actions)?proposal.actions:[];if(!proposal.id||!proposal.digest||!actions.length){notice('预览不完整，请核对结果。');button('assistantCheck',!!proposal.id);return;}var list=element('ul'),valid=true;actions.forEach(function(action){var item=previewAction(action);list.append(item.row);if(!item.valid)valid=false;});box.append(list);button('assistantConfirm',valid);notice(valid?'请核对后确认安排。':'预览信息不完整，暂无法确认；请重新整理或核对。');}
    else if(proposal.state==='clarification'){box.append(element('p',proposal.clarification||'请补充安排内容。'));button('assistantModel',proposal.can_use_model!==false);$('assistantModelHelp').hidden=proposal.can_use_model===false;notice('可以修改上面的要求后重新整理。');}
    else if(proposal.state==='completed'){ $('assistantPreviewTitle').textContent='安排已完成';var results=Array.isArray(proposal.results)?proposal.results:[],links={create_task:['查看任务','/work'],update_task:['查看任务','/work'],create_event:['查看日历','/calendar'],update_event:['查看日历','/calendar'],update_occurrence:['查看日历','/calendar'],create_project:['查看项目','/projects'],update_project:['查看项目','/projects']},seen={},conflicts=0,truncated=false;results.forEach(function(result){var link=links[result.kind],detail=result.result||{};if(link&&!seen[link[1]]){var a=element('a',link[0]);a.href=link[1];a.className='button button-secondary';box.append(a);seen[link[1]]=true;}if(Array.isArray(detail.conflicts))conflicts+=detail.conflicts.length;if(detail.conflicts_truncated)truncated=true;});if(conflicts||truncated)box.append(element('p','存在'+(truncated?'至少 ':' ')+conflicts+' 条日程时间重叠，请查看日历核对。'));button('assistantUndo',!!proposal.digest);notice('');$('workRefresh').click();}
    else if(proposal.state==='undone'){ $('assistantPreviewTitle').textContent='已撤销';notice('');$('workRefresh').click();}
    else {state.unknown=true;box.append(element('p',proposal.state==='planning'?'正在整理，请稍后核对。':'结果尚未确定，请先核对。'));button('assistantCheck',true);notice('');}
  }
  function busy(value){state.busy=value;['assistantPlan','assistantConfirm','assistantModel','assistantCheck','assistantUndo'].forEach(function(id){$(id).disabled=value;});}
  async function plan(allowModel){if(state.busy||state.unknown)return;var text=$('assistantText').value.trim();if(!text){notice('请输入要安排的内容。');return;}if(!window.crypto||!crypto.randomUUID){notice('当前环境无法安全整理，请使用安全连接。');return;}var epoch=state.epoch;busy(true);state.requestId=crypto.randomUUID();state.sentText=text;state.proposal=null;notice('正在整理…');try{await identify();if(epoch!==state.epoch)return;var data=await call('/api/work/assistant/proposals',{request_id:state.requestId,text:text,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,allow_model:allowModel});await identify();if(epoch!==state.epoch)return;if(!data||!data.proposal)throw new Error('invalid response');render(data.proposal);}catch(error){if(epoch!==state.epoch)return;if(!error.status||error.status>=500||error.status===408){state.unknown=true;$('assistantPreview').hidden=false;$('assistantDetails').replaceChildren(element('p','整理结果未知，请先核对。'));button('assistantCheck',true);button('assistantConfirm',false);button('assistantModel',false);$('assistantModelHelp').hidden=true;notice('');}else notice(error.message+'，请检查要求后重试。');}finally{busy(false);}}
  async function check(){if(state.busy)return;var epoch=state.epoch;busy(true);notice('正在核对…');try{await identify();if(epoch!==state.epoch)return;var path=state.proposal&&state.proposal.id?'/api/work/assistant/proposals/'+encodeURIComponent(state.proposal.id):'/api/work/assistant/proposals/by-request/'+encodeURIComponent(state.requestId);var data=await call(path);await identify();if(epoch!==state.epoch)return;if(!data||!data.proposal)throw new Error('invalid response');render(data.proposal);}catch(error){if(epoch===state.epoch)notice('暂时无法核对，请稍后重试；原要求已保留。');}finally{busy(false);}}
  async function apply(action){if(state.busy||state.unknown||!state.proposal)return;var proposal=state.proposal,epoch=state.epoch;busy(true);notice(action==='confirm'?'正在确认安排…':'正在撤销…');try{await identify();if(epoch!==state.epoch)return;var result=await call('/api/work/assistant/proposals/'+encodeURIComponent(proposal.id)+'/'+action,{digest:proposal.digest});await identify();if(epoch!==state.epoch)return;if(!result||!result.proposal)throw new Error('invalid response');render(result.proposal);}catch(error){if(epoch!==state.epoch)return;if(error.status===409){state.unknown=true;button('assistantConfirm',false);button('assistantUndo',false);button('assistantCheck',true);notice(action==='undo'?'内容可能已修改，请核对并到原页面处理。':'安排已变化，请先核对。');}else if(!error.status||error.status>=500||error.status===408){state.unknown=true;button('assistantConfirm',false);button('assistantUndo',false);button('assistantCheck',true);notice('操作结果未知，请先核对。');}else notice(error.message+'；原要求已保留。');}finally{busy(false);}}
  $('assistantForm').addEventListener('submit',function(event){event.preventDefault();if(event.isComposing||state.composing)return;plan(false);});$('assistantModel').addEventListener('click',function(){plan(true);});$('assistantConfirm').addEventListener('click',function(){if($('assistantText').value.trim()!==state.sentText){notice('要求已修改，请重新整理后再确认。');return;}apply('confirm');});$('assistantCheck').addEventListener('click',check);$('assistantUndo').addEventListener('click',function(){apply('undo');});$('assistantText').addEventListener('input',function(){if(!state.unknown&&state.proposal&&$('assistantText').value.trim()!==state.sentText){button('assistantConfirm',false);notice('要求已修改，请重新整理。');}});
  $('assistantText').addEventListener('compositionstart',function(){state.composing=true;});$('assistantText').addEventListener('compositionend',function(){state.composing=false;});
  document.addEventListener('visibilitychange',async function(){var section=$('assistantForm').parentElement;if(document.hidden){section.style.visibility='hidden';return;}try{await identify();section.style.visibility='';}catch(error){clearPrivate();notice(error.message==='账号已切换'?'账号已切换，请重新输入安排。':'无法核对账号，请刷新后重试。');section.style.visibility='';}});
  identify().catch(function(){clearPrivate();notice('无法核对账号，请刷新后重试。');});
}());
