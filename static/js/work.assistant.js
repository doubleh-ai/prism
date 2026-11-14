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
    if(key==='start_at'||key==='end_at'||key==='due_at'||key==='not_before')return time(value,zone);
    if(key==='duration_minutes')return value+' 分钟';
    if(key==='milestone')return value?'是':'否';
    if(key==='recurrence')return recurrence(value);
    if(key==='cancelled')return value?'已取消':'未取消';
    if(key==='status'){var statuses=kind==='update_project'?{active:'进行中',completed:'已完成',archived:'已归档'}:{todo:'待开始',doing:'进行中',done:'已完成',cancelled:'已取消'};return statuses[value]||'状态待核对';}
    if(key==='priority')return ({low:'较低',normal:'普通',high:'重要'})[value]||'优先级待核对';
    return typeof value==='string'||typeof value==='number'?String(value):'内容待核对';
  }
  function projectPreview(action){
    var kind=action.kind,fields=action.fields||{},before=action.before||{},target=action.target||{},row=element('li'),valid=true,changed=0;
    row.append(element('strong',(target.project_name||'项目')+' · '+(target.title||'时间安排')));
    if(kind==='set_project_dependencies'){
      var changes=target.dependency_changes;
      if(!Array.isArray(changes)||!changes.length)return {row:row,valid:false};
      changes.forEach(function(edge){if(!edge.before_title||!edge.after_title||!Number.isInteger(edge.lag_minutes)||edge.lag_minutes<0){valid=false;return;}row.append(element('p',(target.remove?'移除依赖：':'先后顺序：')+edge.before_title+' → '+edge.after_title+(edge.lag_minutes?'，间隔 '+edge.lag_minutes+' 工作分钟':'')));});
      return {row:row,valid:valid};
    }
    if(kind==='set_project_calendar'){
      var config=before.config||{},days=['一','二','三','四','五','六','日'];
      if(!fields.start_at||!fields.timezone||!Array.isArray(fields.weekdays)||!fields.weekdays.length||!Array.isArray(fields.holidays)||!Number.isInteger(fields.work_start)||!Number.isInteger(fields.work_end)||fields.work_end<=fields.work_start)return {row:row,valid:false};
      row.append(element('p','计划开始：'+(before.configured?time(config.start_at,config.timezone):'未启用')+' → '+time(fields.start_at,fields.timezone)));
      function clock(n){return String(Math.floor(n/60)).padStart(2,'0')+':'+String(n%60).padStart(2,'0');}
      row.append(element('p',fields.timezone+'，'+clock(fields.work_start)+'–'+clock(fields.work_end)+'，'+fields.weekdays.map(function(d){if(!Number.isInteger(d)||d<0||d>6){valid=false;return '待核对';}return '周'+days[d];}).join('、')));
      row.append(element('p',fields.holidays.length?'额外休息日：'+fields.holidays.join('、'):'未设置额外休息日；请按项目实际日历核对。'));
      return {row:row,valid:valid};
    }
    var names={duration_minutes:'工时',assignee:'负责人',not_before:'不早于',milestone:'里程碑'},internal={request_id:true,expected_version:true};
    Object.keys(fields).forEach(function(key){if(internal[key])return;if(!names[key]||!Object.prototype.hasOwnProperty.call(before,key)){valid=false;return;}if(before[key]===fields[key])return;changed++;var oldValue=key==='duration_minutes'&&before[key]===null?'默认估算 60 分钟':fieldValue(key,before[key],kind,target.timezone),newValue=key==='duration_minutes'&&fields[key]===null?'默认估算 60 分钟':fieldValue(key,fields[key],kind,target.timezone);row.append(element('p',names[key]+'：'+oldValue+' → '+newValue));});
    return {row:row,valid:valid&&changed>0};
  }
  function approvalPreview(action){
    var row=element('li'),target=action.target||{},fields=action.fields||{},snapshot=target.approval_snapshot,valid=true;
    if(typeof target.project_name!=='string'||!target.project_name||typeof fields.title!=='string'||!fields.title||typeof fields.reviewer!=='string'||!fields.reviewer||fields.reviewer===state.user||!snapshot||typeof snapshot!=='object'||!Object.prototype.hasOwnProperty.call(snapshot,'task')||!Array.isArray(snapshot.attachments)||!Array.isArray(fields.attachment_ids)||!Object.prototype.hasOwnProperty.call(fields,'task_id'))return {row:row,valid:false};
    row.append(element('strong','发起审批：'+target.project_name+' · '+fields.title),element('p','审批人：'+fields.reviewer));
    if(typeof fields.description==='string'&&fields.description)row.append(element('p','说明：'+fields.description));
    if(fields.task_id!==null){if(!snapshot.task||String(snapshot.task.id)!==String(fields.task_id)||typeof snapshot.task.title!=='string'||!snapshot.task.title)valid=false;else row.append(element('p','关联任务：'+snapshot.task.title));}
    else if(snapshot.task)valid=false;
    var selected=fields.attachment_ids.map(String),files=snapshot.attachments;
    if(new Set(selected).size!==selected.length||files.length!==selected.length)valid=false;
    files.forEach(function(file){if(!file||!selected.includes(String(file.id))||typeof file.filename!=='string'||!file.filename||typeof file.sha256!=='string'||!/^[a-f0-9]{64}$/i.test(file.sha256)||!Number.isSafeInteger(file.version))valid=false;else row.append(element('p','附件：'+file.filename+' · 指纹 '+file.sha256.slice(0,12)+'…'));});
    if(new Set(files.map(function(file){return file&&String(file.id);})).size!==files.length)valid=false;
    row.append(element('p','提交会固定所选任务版本与附件指纹，内容变化后需重新发起。'));
    return {row:row,valid:valid};
  }
  function previewAction(action){
    var fields=action.fields||{},kind=action.kind||'',update=kind.indexOf('update_')===0,before=action.before||{},target=action.target||{},zone=target.timezone||before.timezone||fields.timezone,row=element('li');
    if(kind==='plan_project_task'||kind==='set_project_dependencies'||kind==='set_project_calendar')return projectPreview(action);
    if(kind==='create_project_approval')return approvalPreview(action);
    var types={create_task:'任务',create_project_task:'项目任务',create_event:'日程',create_project:'项目',update_task:'修改任务',update_project_task:'修改项目任务',update_project:'修改项目',update_event:before.recurrence?'修改整组重复日程':'修改日程',update_occurrence:'只调整这一次日程'};
    if(!types[kind])return {row:row,valid:false};
    row.append(element('strong',types[kind]+'：'+(target.project_name?target.project_name+' · ':'')+(update?(target.title||before.title||before.name||'未命名'):(fields.title||fields.name||'未命名'))));
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
  function readProjectSummary(result){
    var wrap=element('div'),project=result&&result.project;
    if(!project||typeof project!=='object'){wrap.append(element('p','项目资料不完整，请到项目页核对。'));return wrap;}
    wrap.append(element('p','项目：'+(project.name||'未命名')));
    if(project.description)wrap.append(element('p',project.description));
    var status={active:'进行中',completed:'已完成',archived:'已归档',todo:'待开始',doing:'进行中',done:'已完成',cancelled:'已取消',pending:'待审批',approved:'已通过',rejected:'已拒绝'};
    if(project.status)wrap.append(element('p','状态：'+(status[project.status]||'未知')));
    function section(key,title,items,limit,truncated,describe){var details=element('details'),summary=element('summary',title);details.open=result.section===key;details.append(summary);if(!Array.isArray(items)){details.append(element('p','未提供。'));wrap.append(details);return;}if(!items.length)details.append(element('p','暂无'+title+'。'));else{var list=element('ul');items.slice(0,limit).forEach(function(item){list.append(element('li',describe(item||{})));});details.append(list);}if(truncated||items.length>limit)details.append(element('p','还有未显示的记录，请到项目页查看。'));wrap.append(details);}
    section('tasks','任务',result.tasks,50,result.tasks_truncated,function(item){return (item.title||'未命名')+' · '+(status[item.status]||'状态未知');});
    section('attachments','附件',result.attachments,30,result.attachments_truncated,function(item){return item.filename||'未命名附件';});
    if(Array.isArray(result.attachments)&&result.attachments.length>=30&&!result.attachments_truncated)wrap.append(element('p','项目摘要最多展示 30 个附件，完整列表请到项目页核对。'));
    section('approvals','审批',result.approvals,50,result.approvals_truncated,function(item){return (item.title||'未命名')+' · '+(status[item.status]||'状态未知');});
    return wrap;
  }
  function render(proposal){state.proposal=proposal;state.unknown=false;var box=$('assistantDetails');box.replaceChildren();$('assistantPreview').hidden=false;['assistantConfirm','assistantModel','assistantCheck','assistantUndo'].forEach(function(id){button(id,false);});$('assistantModelHelp').hidden=true;$('assistantPreviewTitle').textContent='安排预览';
    if(proposal.state==='ready'){var actions=Array.isArray(proposal.actions)?proposal.actions:[];if(!proposal.id||!proposal.digest||!actions.length){notice('预览不完整，请核对结果。');button('assistantCheck',!!proposal.id);return;}var list=element('ul'),valid=true;actions.forEach(function(action){var item=previewAction(action);list.append(item.row);if(!item.valid)valid=false;});box.append(list);button('assistantConfirm',valid);notice(valid?'请核对后确认安排。':'预览信息不完整，暂无法确认；请重新整理或核对。');}
    else if(proposal.state==='clarification'){box.append(element('p',proposal.clarification||'请补充安排内容。'));button('assistantModel',proposal.can_use_model!==false);$('assistantModelHelp').hidden=proposal.can_use_model===false;notice('可以修改上面的要求后重新整理。');}
    else if(proposal.state==='completed'){
      var results=Array.isArray(proposal.results)?proposal.results:[],onlyReads=results.length>0&&results.every(function(result){return result.kind==='read_project';});
      $('assistantPreviewTitle').textContent=onlyReads?'项目概览':'安排已完成';
      var links={create_task:['查看任务','/work'],update_task:['查看任务','/work'],create_event:['查看日历','/calendar'],update_event:['查看日历','/calendar'],update_occurrence:['查看日历','/calendar'],create_project:['查看项目','/projects'],update_project:['查看项目','/projects'],create_project_task:['查看项目','/projects'],update_project_task:['查看项目','/projects'],plan_project_task:['查看项目','/projects'],set_project_dependencies:['查看项目','/projects'],set_project_calendar:['查看项目','/projects'],create_project_approval:['查看项目','/projects'],read_project:['查看项目','/projects']},seen={},conflicts=0,truncated=false;
      results.forEach(function(result){var link=links[result.kind],detail=result.result||{};if(result.kind==='read_project')box.append(readProjectSummary(detail));if(link&&!seen[link[1]]){var a=element('a',link[0]);a.href=link[1];a.className='button button-secondary';box.append(a);seen[link[1]]=true;}if(Array.isArray(detail.conflicts))conflicts+=detail.conflicts.length;if(detail.conflicts_truncated)truncated=true;});
      if(conflicts||truncated)box.append(element('p','存在'+(truncated?'至少 ':' ')+conflicts+' 条日程时间重叠，请查看日历核对。'));
      button('assistantUndo',!!proposal.digest&&proposal.can_undo!==false&&!onlyReads);notice('');if(!onlyReads)$('workRefresh').click();
    }
    else if(proposal.state==='model_rejected'){
      $('assistantPreviewTitle').textContent='本次未生成草稿';
      var reasons={billing:'模型服务因计费问题拒绝了这次请求。',access:'模型服务拒绝了这次访问。',rate_limit:'模型服务当前限制请求频率。'};
      box.append(element('p',reasons[proposal.reason]||'模型服务拒绝了这次请求。'));
      notice('');
    }
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
