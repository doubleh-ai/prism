/* Execute the shipped frontend functions with isolated DOM/API fakes.
 * No browser network, credentials, application imports or production data. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const markedModule = require('../static/lib/marked.min.js');
// Exercise Shanghai market dates from a browser whose local timezone is UTC.
process.env.TZ = 'UTC';

function element() {
  const classes = new Set();
  return {
    innerHTML:'',textContent:'',value:'',style:{},dataset:{},open:false,disabled:false,title:'',
    classList:{add(...names){names.forEach(n=>classes.add(n));},remove(...names){names.forEach(n=>classes.delete(n));},contains(n){return classes.has(n);},toggle(n,on){const next=on===undefined?!classes.has(n):on;if(next)classes.add(n);else classes.delete(n);return next;}},
    addEventListener(){},setAttribute(){},removeAttribute(){},querySelectorAll(){return [];},querySelector(){return null;},appendChild(){},focus(){},scrollTo(){},scrollIntoView(){},
    showModal(){this.open=true;},close(){this.open=false;},
  };
}
function environment() {
  const elements = new Map();
  const listeners={};
  const doc={cookie:'csrf_token=test-csrf',addEventListener(type,callback){(listeners[type]||(listeners[type]=[])).push(callback);},querySelectorAll(){return [];},querySelector(){return null;},createElement:element,documentElement:element(),getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
  const context={console,document:doc,marked:markedModule.marked,TextDecoder,TextEncoder,URL,Intl,Set,Map,Date,JSON,Promise,
    location:{href:''},navigator:{},localStorage:{getItem(){return null;},setItem(){},removeItem(){}},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},
    addEventListener(){},matchMedia(){return {matches:false};},requestAnimationFrame(){return 1;},cancelAnimationFrame(){},setInterval(){return 1;},clearInterval(){},setTimeout,clearTimeout,
    fetch(){throw Error('Unexpected network attempt');},NodeFilter:{SHOW_TEXT:4}};
  context.window=context; vm.createContext(context);
  for(const name of ['app.util.js','app.chat-render.js','app.core.js','app.chat.js','app.vibe.js']) vm.runInContext(fs.readFileSync('static/js/'+name,'utf8'),context,{filename:name});
  const app=context.App;
  const shippedRenderOne=app.renderOne;
  Object.assign(app,{renderAll(){},renderOne(){},loadConvs:async()=>{},_refreshReportsButton(){},_closeMobileSidebar(){},_buildTOC(){},_scrollToBottom(){},_startTypewriter(){},_stopTypewriter(){},_toast(){},selectAC(){return false;},_reportFilesForMessage(){return [];}});
  return {context,app,elements,doc,shippedRenderOne,listeners};
}
const settled=()=>new Promise(setImmediate);

async function conversationOrder() {
  const {app}=environment(),pending={};
  app.api=url=>new Promise(resolve=>{pending[url.split('/').pop()]=resolve;});
  const a=app.openConv('A'),b=app.openConv('B');
  pending.B({ok:true,json:async()=>({messages:[{content:'B contents'}],skill:'B-skill'})});await b;
  pending.A({ok:true,json:async()=>({messages:[{content:'A contents'}],skill:'A-skill'})});await a;
  assert.equal(app.convId,'B');assert.equal(app.msgs[0].content,'B contents');assert.equal(app.skill,'B-skill');
  const c=app.openConv('C'),d=app.openConv('D');
  pending.D({ok:true,json:async()=>({messages:[{content:'D contents'}],skill:'D-skill'})});await d;
  pending.C({ok:false,status:404});await c;
  assert.equal(app.convId,'D','late failure must not roll the user back');
}
async function cachedForbiddenIsNotShown() {
  const {context,app}=environment();let last;
  app.convId='previous';app.msgs=[{content:'previous content'}];
  context._idb={getConv:async()=>({messages:[{content:'revoked content'}]}),putConv:async()=>{}};
  app.renderAll=()=>{last=app.msgs[0].content;};app.api=async()=>({ok:false,status:403});
  await app.openConv('revoked');assert.equal(app.convId,'previous');assert.equal(last,'previous content');
}
function immutableRefsAndMetadata() {
  const {context,app}=environment();
  const ref={t:'ref',url:'https://example.com/source',title:'公开文件',source_kind:'external_web',evidence_status:'retrieved',run_id:'run-A',retrieved_at:'2026-10-21T11:42:00+08:00',published_at:null};
  const research={as_of:'2026-10-21T11:40:00+08:00',run_id:'run-A',timezone:'Asia/Shanghai'};
  const message={_refs:[ref],research};const chunks=[{t:'research',research},{t:'text',c:{type:'text',text:'正文'}},ref];
  const before=JSON.stringify({message,chunks});const first=app._renderMsg(chunks,'正文',false,message);
  for(let i=0;i<20;i++)assert.equal(app._renderMsg(chunks,'正文',false,message),first);
  assert.equal(JSON.stringify({message,chunks}),before,'rendering must never append into persisted refs');
  assert.equal((first.match(/class="ref-item"/g)||[]).length,1);
  assert(first.includes('已采集 · 未核验'));assert(first.includes('发布：未提供'));assert(first.includes('北京时间'));assert(!first.includes('[object Object]'));assert(!first.includes('已核验事实'));
  const other=context.ChatRender.collectRefs([ref],[{...ref,run_id:'run-B'}]);assert.equal(other.length,2,'different runs retain provenance');
  assert(context.ChatRender.formatTime(null)==='未提供');
}
async function backgroundHttpFailure() {
  const {context,app,doc}=environment();let resolve;
  context.fetch=()=>new Promise(r=>{resolve=r;});
  app.convId='A';app.msgs=[];doc.getElementById('input').value='A question';
  const pending=app.send(),aMessages=app.msgs;
  app.convId='B';app.msgs=[{role:'assistant',content:'B contents'}];
  context.Streams.B={active:true,reader:{name:'B-reader'},msgs:app.msgs,msgIdx:0};app._activeReader=context.Streams.B.reader;
  resolve({ok:false,status:503,json:async()=>({error:'temporarily unavailable'})});await pending;
  assert.equal(app.msgs[0].content,'B contents');assert.equal(aMessages[1].content,'temporarily unavailable');
  assert.equal(app._activeReader.name,'B-reader');assert(doc.getElementById('sendBtn').classList.contains('stop-mode'));
}
async function backgroundEvents() {
  const {context,app,doc}=environment();let resolve;
  context.fetch=()=>new Promise(r=>{resolve=r;});
  app.convId='A';app.msgs=[];doc.getElementById('input').value='A question';
  const pending=app.send(),aMessages=app.msgs;
  app.convId='B';app.msgs=[{role:'assistant',content:'B contents',_refs:[]}];
  const research={run_id:'run-A',as_of:'2026-10-21T11:42:00+08:00',timezone:'Asia/Shanghai'};
  const ref={url:'https://example.com/a',title:'A source',run_id:'run-A',source_kind:'external_web',evidence_status:'retrieved',retrieved_at:'2026-10-21T11:43:00+08:00'};
  const call={call_id:'call-A',conv_id:'A',status:'completed',target:{view:'market',ticker:'600000'}};
  const payload=[{research},{text:'A answer'},{ref},{platform_call:call}].map(e=>'data: '+JSON.stringify(e)+'\n').join('')+'data: [DONE]\n';let reads=0;
  resolve({ok:true,status:200,body:{getReader:()=>({read:async()=>reads++?{done:true}:{done:false,value:new TextEncoder().encode(payload)},cancel:async()=>{}})}});await pending;
  assert.equal(app.msgs[0].content,'B contents');assert.equal(app.msgs[0]._refs.length,0);
  assert.equal(aMessages[1].research.run_id,'run-A');assert.equal(aMessages[1]._refs[0].retrieved_at,ref.retrieved_at);
  assert(aMessages[1]._chunks.some(c=>c.t==='platform_call'&&c.call.call_id==='call-A'));
}
async function noDuplicatePostOnNetworkFailure() {
  const {context,app,doc}=environment();let calls=0;
  context.fetch=async()=>{calls++;throw new TypeError('fetch failed');};app.convId='A';app.msgs=[];doc.getElementById('input').value='question';
  await app.send();assert.equal(calls,1);assert(app.msgs[1].content.includes('不会自动重复提交'));
}
async function logoutUsesServer() {
  const {context,app}=environment();let sent;
  app.api=async(url,options)=>{sent={url,options};return {ok:true};};await app.logout();
  assert.equal(sent.url,'/api/logout');assert.equal(sent.options.method,'POST');assert.equal(context.location.href,'/login');
  context.location.href='';app.api=async()=>({ok:false});await app.logout();assert.equal(context.location.href,'','failed logout must not falsely report success');
}
async function vibeRetriesAfterNetworkFailure() {
  const {context,app}=environment();const scheduled=[];let calls=0;const notices=[];
  context.setTimeout=(fn,delay)=>{scheduled.push({fn,delay});return scheduled.length;};context.clearTimeout=()=>{};
  app.api=async()=>{calls++;throw Error('must not contact paused engine');};app._toast=value=>notices.push(value);
  app._pollVibePlan('v');await app._runVibePlan();await app._submitVibeFollowup();await settled();
  assert.equal(calls,0);assert.equal(scheduled.length,0);assert.equal(app._vibePollTimers.v,undefined);
  assert.equal(notices.length,2);assert.match(notices[0],/隔离执行环境尚待验收/);
  const legacy={id:'v',status:'cancelling',title:'Saved research'};
  app._activeVibePlanId='v';app._renderVibePlan(legacy);
  const html=context.document.getElementById('vibePanelInner').innerHTML;
  assert.match(html,/<button class="danger" onclick="App\._cancelVibePlan\(\)">再次请求停止<\/button>/);
  assert.equal(calls,0,'displaying unconfirmed history never sends cancellation automatically');
  app.api=async(url,options)=>{calls++;assert.equal(url,'/api/vibe/plans/v/cancel');assert.equal(options.method,'POST');return {ok:true,json:async()=>({plan:legacy})};};
  await app._cancelVibePlan();await settled();
  assert.equal(calls,1,'only an explicit retry sends one fixed cancellation request');
  assert.equal(scheduled.length,0);assert.equal(app._activeVibePlan.status,'cancelling');
}
async function navigationOpensExistingRecord() {
  const {context,app}=environment();const opened=[];let starts=0;
  app._mode='chat';app.switchMode=mode=>{app._mode=mode;};app._showMarketWorkspace=tab=>{app._mode='market';app._marketWorkspaceTab=tab;};
  vm.runInContext(fs.readFileSync('static/js/app.workbench.js','utf8'),context);
  app._loadWorkbenchHistory=()=>{};app._openVibePlan=async id=>opened.push(['vibe',id]);app._openAnalysisRecord=async id=>opened.push(['analysis',id]);app._openScanJob=async id=>opened.push(['screener',id]);
  app._runVibePlan=app._runAnalysis=()=>{starts++;};
  await app._navigateTarget({view:'vibe',id:'v-existing'});await app._navigateTarget({view:'analysis',id:'a-existing'});await app._navigateTarget({view:'screener',id:'s-existing'});
  assert.equal(JSON.stringify(opened),JSON.stringify([['vibe','v-existing'],['analysis','a-existing'],['screener','s-existing']]));assert.equal(starts,0);
  app._receivePlatformCall({call_id:'call-1',conv_id:'c',target:{view:'screener'},status:'running'});
  app._receivePlatformCall({call_id:'call-1',conv_id:'c',target:{view:'screener'},status:'completed'});
  assert.equal(app._workbenchCalls.length,1);assert.equal(app._workbenchCalls[0].status,'completed');
  const research={run_id:'r',as_of:'2026-10-21T11:42:00+08:00'};
  const html=app._renderMsg([{t:'research',research},{t:'platform_call',call:{call_id:'x',label:'查询',status:'running',target:{view:'market'}}},{t:'platform_call',call:{call_id:'x',label:'查询',status:'completed',target:{view:'market'}}}], '摘要',false,{});
  assert.equal((html.match(/class="platform-call-card"/g)||[]).length,1);assert(html.includes('已完成'));assert(html.includes('摘要'));
}
function controlsAndLiveCapabilityCards() {
  const {context,app,doc,shippedRenderOne}=environment();
  app.convId='A';let owner;
  app.send=function(){owner=this;};app._syncChatStreamControls();
  const button=doc.getElementById('sendBtn');button.onclick.call(button);assert.equal(owner,app,'button click must preserve App receiver');
  vm.runInContext(fs.readFileSync('static/js/app.workbench.js','utf8'),context);
  const bubble=element();doc.getElementById('msgList').querySelectorAll=selector=>selector==='.bubble'?[bubble]:[];
  app.renderQuoteTags=app._glossifyTerms=app._linkifyReports=html=>html;app.fillQuoteChips=()=>{};
  app.msgs=[{role:'assistant',content:'not revealed yet',_displayLen:0,_chunks:[{t:'text',c:'not revealed yet'},{t:'status',c:'已使用内置研究方法'},{t:'platform_call',call:{call_id:'live',status:'running',label:'查询行情',target:{view:'market'}}}]}];
  context.Streams.A={active:true,msgs:app.msgs,msgIdx:0};shippedRenderOne.call(app,0);
  assert(bubble.innerHTML.includes('查询行情'));assert(bubble.innerHTML.includes('进行中'));assert(bubble.innerHTML.includes('已使用内置研究方法'));
  assert(!bubble.innerHTML.includes('not revealed yet'),'metadata must not expose prose early');
}
function hostileSourcesAndOptionsRemainData() {
  const {context,app,listeners}=environment();
  for(const url of ['java\nscript:void(0)','java\tscript:void(0)','javascript:void(0)','data:text/html,<script>void(0)</script>','vbscript:msgbox(1)','file:///etc/passwd','https://username:password@example.invalid/']) {
    assert.equal(context.safeUrl(url),'',url);
    assert(!context.ChatRender.renderChunk({t:'ref',url,title:'untrusted'}).includes('href='),url);
  }
  assert.equal(context.safeUrl('https://example.com/doc?q=1'),'https://example.com/doc?q=1');
  assert.equal(context.safeUrl('/api/reports/owned/document.html'),'/api/reports/owned/document.html');
  assert(!context.render('[source](javascript:alert%281%29)').includes('href="javascript:'));
  const malicious="\\');globalThis.__probe=true;//<img src=x onerror=probe>";
  const html=context.ChatRender.renderChunk({t:'ask',q:'question',opts:[malicious]});
  assert(!html.includes('onclick='));assert(!html.includes('<img'));assert(html.includes('data-ask-answer='));
  let answer;app.answerAsk=value=>{answer=value;};app._bindGlossClicks();
  const option={disabled:false,dataset:{askAnswer:malicious}};
  listeners.click.at(-1)({target:{closest(selector){return selector==='.ask-opt[data-ask-answer]'?option:null;}},preventDefault(){}});
  assert.equal(answer,malicious,'untrusted option must reach the API as literal data');assert.equal(context.__probe,undefined);
}
function timeFormatsPreserveMeaning() {
  const {context}=environment();const fmt=context.ChatRender.formatTime;
  const iso='2026-10-21T03:43:38Z',seconds=1792554218;
  const expected=fmt(iso);assert(expected.includes('北京时间'));assert(expected.includes('11:43'));
  for(const value of [seconds,String(seconds),seconds*1000,String(seconds*1000)])assert.equal(fmt(value),expected);
  assert(fmt('2026-10-21T11:43:38').includes('时区未标注'));
  assert(!fmt('2026-10-21T11:43:38').includes('北京时间'));
  assert.equal(fmt(null),'未提供');assert.equal(fmt('invalid'),'invalid（时区未标注）');
}
async function marketDatesDoNotDependOnBrowserTimezone() {
  const {context,app,doc}=environment();
  vm.runInContext(fs.readFileSync('static/js/app.market.js','utf8'),context,{filename:'app.market.js'});
  const midnight=Date.parse('2026-10-21T00:00:00+08:00')/1000;
  assert.equal(new Date(midnight*1000).getDate(),20,'the test browser must really be in UTC');
  let chartOptions;
  context.echarts={init(){return {setOption(options){chartOptions=options;}};}};
  app.renderKlineChart(element(),[
    {ts:Date.parse('2026-01-01T00:00:00+08:00')/1000,open:10,close:11,low:9,high:12,volume:100},
    {ts:midnight,open:11,close:12,low:10,high:13,volume:200},
  ]);
  for(const axis of chartOptions.xAxis)assert.equal(JSON.stringify(axis.data),JSON.stringify(['2026-01-01','2026-10-21']));
  const card=element();doc.getElementById('marketPanelInner').querySelector=selector=>selector==='.market-quote-card'?card:null;
  app.api=async url=>{
    assert.equal(url,'/api/quote/600000');
    return {ok:true,json:async()=>({name:'600000',price:12,data_timestamp:midnight,data_kind:'daily_close',source:'fixture'})};
  };
  await app._renderQuoteCard('600000','测试股票');
  assert(card.innerHTML.includes('行情日期（日线收盘数据）：2026/10/21 北京时间'));
  assert(!card.innerHTML.includes('00:00'),'a daily close is a date, not a midnight realtime quote');
  assert(!card.innerHTML.includes('行情时间：'));
}
async function completedAnalysisUsesRecordedDuration() {
  const {context,app,doc}=environment();
  vm.runInContext(fs.readFileSync('static/js/app.analysis.js','utf8'),context,{filename:'app.analysis.js'});
  app.switchMode=mode=>{assert.equal(mode,'analysis');};
  app._connectAnalysisStream=()=>{throw Error('completed history must not start or attach research');};
  let record={status:'done',ticker:'600000',started_at:1577836800,report:{},timings:{total_seconds:12},elapsed:99};
  const requests=[];
  app.api=async(url,options)=>{
    requests.push(url);assert(!options||!options.method||options.method==='GET');
    return {ok:true,status:200,json:async()=>record};
  };
  await app._openAnalysisRecord('completed-old','600000');app._tickElapsed();
  assert.equal(app._analysisStreaming,false);
  assert.equal(doc.getElementById('apElapsed').textContent,'已用 12秒','completed timing must not count wall time since the old start');
  record={status:'done',ticker:'600000',started_at:1577836800,report:{}};
  await app._openAnalysisRecord('duration-unknown','600000');app._tickElapsed();
  assert.equal(doc.getElementById('apElapsed').textContent,'耗时未记录','missing timing must not inherit the previous record or invent elapsed days');
  assert.deepEqual(requests,['/api/analysis/completed-old/report','/api/analysis/duration-unknown/report']);
}
async function managementDoesNotRequestCapabilityHistory() {
  const {context,app,doc}=environment();const requests=[];let finishOld;
  app.switchMode=mode=>{app._mode=mode;};app._mode='market';
  vm.runInContext(fs.readFileSync('static/js/app.workbench.js','utf8'),context,{filename:'app.workbench.js'});
  app.api=url=>{requests.push(url);return new Promise(resolve=>{finishOld=resolve;});};
  const pending=app._loadWorkbenchHistory();
  app.switchMode('admin');await settled();
  assert.equal(doc.getElementById('workbenchContext').style.display,'none');
  assert.equal(requests.length,1);assert(!requests.some(url=>url.includes('view=admin')));
  assert.equal(app._workbenchHistoryLoading,false);assert.equal(doc.getElementById('workbenchCallCount').textContent,'0');
  finishOld({ok:false,status:503});await pending;assert.equal(app._workbenchHistoryError,'','late capability failure must not raise a management alert');
  assert.equal(app._capabilityView({id:'admin-control',target_view:'admin'}),'');
  assert(!app._renderPlatformCallHTML({call_id:'invalid-admin',target:{view:'admin'}}).includes('data-platform-call='));
  app._openWorkbenchPanel('history');assert.equal(app._workbenchPanel,'capabilities');assert.equal(doc.getElementById('workbenchHistoryTab').hidden,true);
  app.api=async url=>{requests.push(url);return {ok:true,json:async()=>({calls:[]})};};
  app.switchMode('market');await settled();
  assert.equal(doc.getElementById('workbenchContext').style.display,'');assert.equal(doc.getElementById('workbenchHistoryTab').hidden,false);
  assert.equal(requests.at(-1),'/api/capability-calls?view=market&limit=30');
  app.convId='original-chat';app.switchMode('chat');await settled();assert.equal(requests.at(-1),'/api/convs/original-chat/capability-calls');
  const before=requests.length;app.switchMode('unsupported-view');await settled();assert.equal(requests.length,before);assert.equal(doc.getElementById('workbenchContext').style.display,'none');
}

(async()=>{
  await conversationOrder();await cachedForbiddenIsNotShown();immutableRefsAndMetadata();await backgroundHttpFailure();await backgroundEvents();await noDuplicatePostOnNetworkFailure();await logoutUsesServer();await vibeRetriesAfterNetworkFailure();await navigationOpensExistingRecord();controlsAndLiveCapabilityCards();hostileSourcesAndOptionsRemainData();timeFormatsPreserveMeaning();
  await marketDatesDoNotDependOnBrowserTimezone();await completedAnalysisUsesRecordedDuration();await managementDoesNotRequestCapabilityHistory();
  console.log('workbench runtime ok: 15 scenarios');
})().catch(error=>{console.error(error);process.exitCode=1;});
