/* Exercise the shipped feedback/diagnostic functions; no network or model. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;

function element() {
  const classes = new Set();
  return {innerHTML:'',textContent:'',value:'',hidden:false,disabled:false,open:false,style:{},dataset:{},
    classList:{add(...values){values.forEach(v=>classes.add(v));},remove(...values){values.forEach(v=>classes.delete(v));},contains(v){return classes.has(v);},toggle(v,on){on=on===undefined?!classes.has(v):on;on?classes.add(v):classes.delete(v);}},
    addEventListener(){},setAttribute(){},querySelectorAll(){return [];},querySelector(){return null;},focus(){},showModal(){this.open=true;},close(){this.open=false;}};
}
function env() {
  const elements=new Map(),listeners={},timers=new Map();let timer=0;
  const document={cookie:'csrf_token=synthetic',documentElement:element(),addEventListener(){},querySelectorAll(){return [];},querySelector(){return null;},getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
  const ctx={console,document,crypto,AbortController,WeakSet,URL,Intl,Date,JSON,Map,Set,Promise,
    location:{pathname:'/',search:'?token=DO_NOT_UPLOAD_QUERY',hash:'#DO_NOT_UPLOAD_FRAGMENT',href:'http://local.test/'},navigator:{},
    localStorage:{getItem(){return null;},setItem(){}},sessionStorage:{getItem(){return null;},setItem(){}},matchMedia(){return {matches:false};},
    addEventListener(type,fn){(listeners[type]||=[]).push(fn);},
    setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout(id){timers.delete(id);},setInterval(){return 1;},clearInterval(){},
    fetch(){throw Error('Unexpected external request');}};
  ctx.window=ctx;vm.createContext(ctx);
  for(const name of ['app.util.js','app.core.js','app.diagnostics.js','app.feedback.js','app.admin.js']) {
    vm.runInContext(fs.readFileSync('static/js/'+name,'utf8'),ctx,{filename:name});
  }
  const app=ctx.App;app.user='synthetic-owner';app.isAdmin=false;app._mode='industry';
  app.msgs=[{content:'DO_NOT_UPLOAD_CONVERSATION'}];app._capabilitySnapshot={secret:'DO_NOT_UPLOAD_RESULT'};
  app._releaseData={version:'v0.21.0'};document.getElementById('feedbackCategory').value='bug';
  document.getElementById('feedbackSummary').value='浅色行业文字';document.getElementById('feedbackDescription').value='行业列表的文字和背景接近，请检查。';
  return {ctx,app,document,listeners,timers};
}
const response=(data,status=200)=>({ok:status<400,status,headers:{get(){return '';}},json:async()=>data});
const tick=()=>new Promise(setImmediate);
function delayed(app) {const queue=[];app.api=(url,options)=>new Promise(resolve=>queue.push({url,options,resolve}));return queue;}
function item(id=1, changes={}) {return {id,category:'bug',title:'行业显示',description:'合成反馈内容',page:'/',feature:'industry',status:'triaging',revision_number:1,updated_at:1,release:{version:'v0.21.0',revision:'a'.repeat(12)},triage:{mode:'immediate_rule_triage'},updates:[],...changes};}

async function submissionKeepsOneUuidAndNeverRetriesItself() {
  const {app,document}=env(),requests=[];
  app.api=async(url,options)=>{requests.push({url,payload:JSON.parse(options.body)});throw Error('synthetic lost response');};
  await app._submitFeedback();assert.equal(requests.length,1);assert.equal(app._feedbackBusy,false);assert.equal(document.getElementById('feedbackSubmit').disabled,false);
  const first=requests[0].payload;
  assert.deepEqual(Object.keys(first).sort(),['category','description','feature','page','request_id','title']);
  assert.equal(first.page,'/');assert.equal(first.feature,'industry');assert.match(first.request_id,/^[a-f0-9-]{36}$/i);
  assert(!JSON.stringify(first).includes('DO_NOT_UPLOAD'));assert.match(document.getElementById('feedbackMessage').textContent,/尚未确认/);
  assert(document.getElementById('feedbackDescription').value);
  app.api=async(url,options)=>{requests.push({url,payload:JSON.parse(options.body)});return response({ok:true,reused:true,feedback:item()});};
  await app._submitFeedback();assert.equal(requests.length,2);assert.equal(requests[1].payload.request_id,first.request_id);
  assert.equal(app._feedbackSelected.id,1);assert.equal(document.getElementById('feedbackDescription').value,'');
}
async function changedDraftAndRateLimitRemainExplicit() {
  const {app,document}=env(),ids=[];app.api=async(url,options)=>{ids.push(JSON.parse(options.body).request_id);return response({},429);};
  await app._submitFeedback();assert.equal(ids.length,1);assert.match(document.getElementById('feedbackMessage').textContent,/较频繁/);
  document.getElementById('feedbackSummary').value='修改后的问题';await app._submitFeedback();assert.equal(ids.length,2);assert.notEqual(ids[0],ids[1]);
  assert.equal(document.getElementById('feedbackSummary').value,'修改后的问题');
}
async function busySubmissionCannotCreateAnotherRequest() {
  const {app}=env(),queue=delayed(app);const pending=app._submitFeedback();await app._submitFeedback();assert.equal(queue.length,1);
  queue[0].resolve(response({ok:true,feedback:item()}));await pending;assert.equal(app._feedbackBusy,false);
}
async function uuidFallbackIsStillARealVersionFourIdentifier() {
  const {app,ctx}=env();ctx.crypto={getRandomValues:crypto.getRandomValues.bind(crypto)};
  const first=app._feedbackUUID(),second=app._feedbackUUID();
  assert.match(first,/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.notEqual(first,second);
}
async function escapedFeedbackAndOwnedVersusAdminViews() {
  const {app,document}=env(),attack='<img src=x onerror="steal()"><script>run()</script>';
  const row=item(8,{title:attack,description:attack,username:'PRIVATE_OWNER',public_reply:attack,release:{version:attack,revision:attack},updates:[{status:'fixed',public_reply:attack,verification:attack,target_version:attack,created_at:1}]});
  app._feedbackItems=[row];app._renderFeedbackList(false);
  let html=document.getElementById('feedbackContent').innerHTML;assert(!html.includes('PRIVATE_OWNER'));assert(!html.includes('<img'));assert(html.includes('&lt;img'));
  app._feedbackAdmin=false;app._renderFeedbackDetail(row);html=document.getElementById('feedbackContent').innerHTML;
  assert(!html.includes('<script>'));assert(!html.includes('feedback-review-form'));assert(!html.includes('PRIVATE_OWNER'));
  let calls=0;app._feedbackAdmin=true;app.api=async()=>{calls++;throw Error('not authorized');};await app._updateFeedback();assert.equal(calls,0);
  app.isAdmin=true;app._renderFeedbackList(true);assert(document.getElementById('feedbackContent').innerHTML.includes('PRIVATE_OWNER'));
  app._renderFeedbackDetail(row);assert(document.getElementById('feedbackContent').innerHTML.includes('feedback-review-form'));
  assert.equal(app._feedbackPath(8),'/api/admin/feedback/8');app._feedbackAdmin=false;assert.equal(app._feedbackPath(8),'/api/feedback/8');
}
async function ownerAndAdminUseDifferentListEndpoints() {
  const {app,document}=env(),paths=[];app.api=async url=>{paths.push(url);return response({items:[],next_before_id:null});};
  app._feedbackView('admin');await tick();assert(paths.at(-1).startsWith('/api/feedback?'));
  app.isAdmin=true;app._feedbackView('admin');await tick();assert(paths.at(-1).startsWith('/api/admin/feedback?'));
  app._feedbackView('mine');await tick();assert(paths.at(-1).startsWith('/api/feedback?'));assert(!document.getElementById('feedbackContent').innerHTML.includes('feedbackQueueFilter'));
}
function reviewForm(document) {
  document.getElementById('feedbackDecision').value='fixed';document.getElementById('feedbackReply').value='已核对两个主题的显示';
  document.getElementById('feedbackVerification').value='';document.getElementById('feedbackTarget').value='';
}
async function fixedValidationAndConflictDoNotResubmit() {
  const {app,document}=env();app.isAdmin=true;app._feedbackAdmin=true;app._feedbackSelected=item(12);reviewForm(document);const queue=delayed(app);
  await app._updateFeedback();assert.equal(queue.length,0);assert.match(document.getElementById('feedbackMessage').textContent,/验证说明/);
  document.getElementById('feedbackVerification').value='已验证两个主题和手机窄屏，原问题不再出现';document.getElementById('feedbackTarget').value='v0.21.0';
  const pending=app._updateFeedback();assert.equal(queue[0].url,'/api/admin/feedback/12/update');
  const payload=JSON.parse(queue[0].options.body);assert.equal(payload.expected_revision,1);assert.equal(payload.status,'fixed');
  queue[0].resolve(response({},409));await pending;assert.equal(queue.length,1);assert.match(document.getElementById('feedbackMessage').textContent,/已被更新/);assert.equal(app._feedbackSelected.status,'triaging');
  const second=app._updateFeedback();queue[1].resolve(response({ok:true,feedback:item(12,{status:'fixed',revision_number:2,verification:payload.verification,target_version:'v0.21.0'})}));await second;
  assert.equal(app._feedbackSelected.status,'fixed');assert.equal(app._feedbackSelected.revision_number,2);assert.match(document.getElementById('feedbackContent').innerHTML,/v0.21.0/);
  assert(!document.getElementById('feedbackContent').innerHTML.includes('尚未验证修复'));assert.match(document.getElementById('feedbackContent').innerHTML,/最初分诊/);
}
async function lateListCannotReplaceNewForm() {
  const {app,document}=env(),queue=delayed(app);const pending=app._loadFeedback();app._feedbackView('new');
  queue[0].resolve(response({items:[item(91,{title:'STALE_OLD_LIST'})]}));await pending;
  assert.equal(document.getElementById('feedbackContent').hidden,true);assert(!document.getElementById('feedbackContent').innerHTML.includes('STALE_OLD_LIST'));
}
async function lateSubmitCannotEraseNewDraft() {
  const {app,document}=env(),queue=delayed(app);const pending=app._submitFeedback();
  app._feedbackView('new');document.getElementById('feedbackSummary').value='NEW_UNSENT_TITLE';document.getElementById('feedbackDescription').value='NEW_UNSENT_DESCRIPTION';
  queue[0].resolve(response({ok:true,feedback:item(1)}));await pending;
  assert.equal(document.getElementById('feedbackSummary').value,'NEW_UNSENT_TITLE');assert.equal(document.getElementById('feedbackDescription').value,'NEW_UNSENT_DESCRIPTION');assert.equal(document.getElementById('feedbackForm').hidden,false);
}
async function lateUpdateCannotReplaceAnotherDetail() {
  const {app,document}=env();app.isAdmin=true;app._feedbackAdmin=true;app._renderFeedbackDetail(item(10));reviewForm(document);
  document.getElementById('feedbackDecision').value='in_progress';const queue=delayed(app);const save=app._updateFeedback();const next=app._openFeedbackItem(20);
  queue[1].resolve(response({feedback:item(20,{title:'CURRENT_FEEDBACK_B'})}));await next;
  queue[0].resolve(response({ok:true,feedback:item(10,{title:'STALE_FEEDBACK_A',status:'in_progress'})}));await save;
  assert.equal(app._feedbackSelected.id,20);assert(document.getElementById('feedbackContent').innerHTML.includes('CURRENT_FEEDBACK_B'));assert(!document.getElementById('feedbackContent').innerHTML.includes('STALE_FEEDBACK_A'));
}
async function callerCancellationIsMarkedByObjectNotErrorName() {
  const {app,ctx,listeners,timers}=env(),captured=[];app._captureDiagnostic=(kind,message)=>captured.push({kind,message});app._initDiagnostics();
  let actualError;ctx.fetch=(url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{actualError=new Error('synthetic caller abort');actualError.name='AbortError';reject(actualError);},{once:true}));
  const caller=new AbortController(),pending=app.api('/api/admin/operations',{signal:caller.signal});caller.abort();await assert.rejects(pending,/synthetic caller/);
  assert.equal(captured.length,0);assert(app._expectedRequestCancellations.has(actualError));
  listeners.unhandledrejection[0]({reason:actualError});assert.equal(captured.length,0);
  const unrelated=new Error('real unrelated abort failure');unrelated.name='AbortError';listeners.unhandledrejection[0]({reason:unrelated});assert.equal(captured.length,1);assert.equal(captured[0].kind,'promise_rejection');
  const timeout=app.api('/api/owned-timeout');const deadline=[...timers.values()].find(t=>t.delay===30000);assert(deadline);deadline.fn();
  await assert.rejects(timeout,/执行结果尚未确认/);assert.equal(captured.length,2);assert.equal(captured[1].kind,'api_network_error');
}
async function diagnosticsExplainHistoricalScopeAndArchiveBoundary() {
  const {app}=env();app._adminDiagnostics={stats:{error:3,occurrences:23},events:['current_version','historical_version','unattributed'].map((scope,i)=>({id:i+1,version:i?'v0.19.7':'v0.21.0',version_scope:scope,kind:'js_error',message:'synthetic',severity:'error'}))};
  const html=app._renderAdminDiagnosticsTab();assert.match(html,/当前版本/);assert.match(html,/历史版本，待复测/);assert.match(html,/版本未知，待核对/);assert.match(html,/归档不代表修复完成/);assert(!html.includes('影响功能使用'));
}

(async()=>{
  const cases=[submissionKeepsOneUuidAndNeverRetriesItself,changedDraftAndRateLimitRemainExplicit,busySubmissionCannotCreateAnotherRequest,uuidFallbackIsStillARealVersionFourIdentifier,escapedFeedbackAndOwnedVersusAdminViews,ownerAndAdminUseDifferentListEndpoints,fixedValidationAndConflictDoNotResubmit,lateListCannotReplaceNewForm,lateSubmitCannotEraseNewDraft,lateUpdateCannotReplaceAnotherDetail,callerCancellationIsMarkedByObjectNotErrorName,diagnosticsExplainHistoricalScopeAndArchiveBoundary];
  for(const test of cases)await test();
  console.log('feedback frontend ok: '+cases.length+' scenarios');
})().catch(error=>{console.error(error);process.exitCode=1;});
