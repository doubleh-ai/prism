/* Use the shipped adapter/widget, synthetic identities, and no network. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
const template = fs.readFileSync('templates/partials/feedback-dialog.html', 'utf8');
const tick = () => new Promise(setImmediate);
const reply = (data, status=200) => ({ok:status<400,status,json:async()=>data});
function node() {
  return {value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,open:false,style:{},listeners:{},
    addEventListener(name,callback){this.listeners[name]=callback;},
    showModal(){this.open=true;},close(){this.open=false;if(this.listeners.close)this.listeners.close();},
    focus(){this.focused=true;},replaceChildren(){this.innerHTML='';}};
}
function setup(path='/work') {
  const nodes = new Map([...template.matchAll(/id="([^"]+)"/g)].map(match=>[match[1],node()]));
  nodes.set('assistantText',node());nodes.get('assistantText').value='UNSAVED_WORK_REQUEST';
  const entry=node(),events={},queue=[];
  const document={hidden:false,getElementById:id=>nodes.get(id)||null,
    querySelectorAll:selector=>selector==='[data-work-feedback]'?[entry]:[],
    addEventListener(name,callback){events[name]=callback;}};
  const context={document,location:{pathname:path,search:'?token=DO_NOT_UPLOAD',hash:'#DO_NOT_UPLOAD'},
    console,crypto,AbortController,URL,Date,Intl,setTimeout,clearTimeout,
    addEventListener(name,callback){events[name]=callback;},
    fetch(path,options){return new Promise(resolve=>queue.push({path,options,resolve}));}};
  context.window=context;vm.createContext(context);
  for(const asset of ['app.util.js','work.feedback.js','app.feedback.js']) {
    vm.runInContext(fs.readFileSync('static/js/'+asset,'utf8'),context,{filename:asset});
  }
  nodes.get('feedbackCategory').value='bug';
  return {nodes,entry,events,queue,context,app:context.App};
}
const identity=(e,index,user='test-owner')=>{
  assert.equal(e.queue[index].path,'/api/me');e.queue[index].resolve(reply({user}));
};
async function open(e) {
  const pending=e.entry.listeners.click({currentTarget:e.entry});identity(e,e.queue.length-1);await pending;
}
function draft(e) {
  e.nodes.get('feedbackSummary').value='合成任务问题';
  e.nodes.get('feedbackDescription').value='合成反馈，未附带任务正文。';
}
async function openingAndClosingKeepWorkInPlace() {
  for(const path of ['/work','/projects','/calendar']) {
    const e=setup(path);assert.equal(e.queue.length,0,'no background identity/data requests');
    await open(e);assert(e.nodes.get('feedbackDialog').open);
    draft(e);e.nodes.get('feedbackDialog').close();
    assert(e.entry.focused);assert.equal(e.context.location.pathname,path);
    assert.equal(e.nodes.get('assistantText').value,'UNSAVED_WORK_REQUEST');
    await open(e);assert.equal(e.nodes.get('feedbackDescription').value,'合成反馈，未附带任务正文。');
    assert(e.nodes.get('feedbackReviewEntry').hidden);
    assert.equal(e.app._feedbackPayload().page,path);
  }
}
async function submissionAndProgressUseTheSameTicket() {
  const e=setup('/projects');await open(e);draft(e);
  const pending=e.app._submitFeedback();identity(e,1);await tick();
  const request=e.queue[2];assert.equal(request.path,'/api/feedback');
  const payload=JSON.parse(request.options.body);
  assert.deepEqual(Object.keys(payload).sort(),['category','description','feature','page','request_id','title']);
  assert.equal(payload.page,'/projects');assert.equal(payload.feature,'work');
  assert(!JSON.stringify(payload).includes('UNSAVED_WORK_REQUEST'));assert(!JSON.stringify(payload).includes('DO_NOT_UPLOAD'));
  assert.equal(request.options.credentials,'same-origin');assert.equal(request.options.redirect,'error');
  assert.equal(request.options.headers['X-Prism-Account'],'test-owner');
  assert.equal(request.options.headers['X-Requested-With'],'XMLHttpRequest');
  request.resolve(reply({feedback:{id:7,title:payload.title,description:payload.description,category:'bug',status:'triaging',updates:[]}}));
  await tick();identity(e,3);await pending;
  assert.equal(e.app._feedbackSelected.id,7);assert.match(e.nodes.get('feedbackContent').innerHTML,/#7/);
  assert.equal(e.nodes.get('assistantText').value,'UNSAVED_WORK_REQUEST');
}
async function ownerChangeBeforeSubmitPreventsTheWrite() {
  const e=setup();await open(e);draft(e);const pending=e.app._submitFeedback();
  identity(e,1,'different-owner');await pending;
  assert.equal(e.queue.length,2);assert(e.nodes.get('feedbackForm').hidden);
  assert.equal(e.nodes.get('feedbackDescription').value,'');assert.match(e.nodes.get('feedbackMessage').textContent,/账号已切换/);
}
async function ownerChangeAfterReadHidesTheResponse() {
  const e=setup();await open(e);e.app._feedbackView('mine');identity(e,1);await tick();
  e.queue[2].resolve(reply({items:[{id:9,title:'OLD_OWNER_PRIVATE_TITLE',status:'triaging'}]}));
  await tick();identity(e,3,'different-owner');await tick();
  assert(!e.nodes.get('feedbackContent').innerHTML.includes('OLD_OWNER_PRIVATE_TITLE'));
  assert.equal(e.app._feedbackItems.length,0);assert.match(e.nodes.get('feedbackMessage').textContent,/账号已切换/);
}
async function uncertainSubmitKeepsTheIdAndNeverRetriesItself() {
  const e=setup();await open(e);draft(e);const first=e.app._submitFeedback();
  identity(e,1);await tick();const payload=JSON.parse(e.queue[2].options.body);
  e.queue[2].resolve(reply({},503));await tick();identity(e,3);await first;
  assert.equal(e.queue.length,4);assert(e.nodes.get('feedbackDescription').value);
  const second=e.app._submitFeedback();identity(e,4);await tick();
  assert.equal(JSON.parse(e.queue[5].options.body).request_id,payload.request_id);
  e.queue[5].resolve(reply({},429));await tick();identity(e,6);await second;
  assert.equal(e.queue.length,7);
}
async function workerAndAdminEndpointsAreUnavailableHere() {
  const e=setup();
  for(const path of ['/api/admin/feedback?limit=50','/api/feedback/1/update','https://invalid.example/api/feedback']) {
    await assert.rejects(e.app.api(path),/本人的反馈/);
  }
  assert.equal(e.queue.length,0);
}
async function expiredLoginLeavesTheWorkDraftInPlace() {
  const e=setup();const pending=e.entry.listeners.click({currentTarget:e.entry});
  e.queue[0].resolve(reply({},401));await pending;
  assert(e.nodes.get('feedbackDialog').open);assert(e.nodes.get('feedbackForm').hidden);
  assert.match(e.nodes.get('feedbackMessage').textContent,/登录已失效/);
  assert.equal(e.context.location.pathname,'/work');
  assert.equal(e.nodes.get('assistantText').value,'UNSAVED_WORK_REQUEST');
}
async function hiddenDialogRechecksIdentityBeforeDisplayingAgain() {
  const e=setup();await open(e);draft(e);e.document=e.context.document;
  e.document.hidden=true;await e.events.visibilitychange();assert.equal(e.nodes.get('feedbackDialog').style.visibility,'hidden');
  e.document.hidden=false;const pending=e.events.visibilitychange();identity(e,1,'different-owner');await pending;
  assert.equal(e.nodes.get('feedbackDialog').style.visibility,'');assert.equal(e.nodes.get('feedbackDescription').value,'');
  assert.match(e.nodes.get('feedbackMessage').textContent,/账号已切换/);
}
(async()=>{
  const cases=[openingAndClosingKeepWorkInPlace,submissionAndProgressUseTheSameTicket,ownerChangeBeforeSubmitPreventsTheWrite,ownerChangeAfterReadHidesTheResponse,uncertainSubmitKeepsTheIdAndNeverRetriesItself,workerAndAdminEndpointsAreUnavailableHere,expiredLoginLeavesTheWorkDraftInPlace,hiddenDialogRechecksIdentityBeforeDisplayingAgain];
  for(const test of cases)await test();
  console.log('Work feedback runtime ok: '+cases.length+' scenarios');
})().catch(error=>{console.error(error);process.exitCode=1;});
