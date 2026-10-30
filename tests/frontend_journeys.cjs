/* Real shipped script order, controlled responses and no network or credentials. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const marked = require('../static/lib/marked.min.js');
const template = fs.readFileSync('templates/app.html','utf8');
const scripts = [...template.matchAll(/src="\/static\/js\/([^"?]+)(?:\?[^\"]*)?"/g)].map(m=>m[1]);
function element() {
  const classes=new Set(),children=new Map(),attributes={};
  return {innerHTML:'',textContent:'',value:'',dataset:{},style:{},open:false,hidden:false,disabled:false,
    classList:{add(...n){n.forEach(x=>classes.add(x));},remove(...n){n.forEach(x=>classes.delete(x));},contains(n){return classes.has(n);},toggle(n,on){if(on===undefined)on=!classes.has(n);on?classes.add(n):classes.delete(n);return on;}},
    addEventListener(){},setAttribute(k,v){attributes[k]=v;},getAttribute(k){return attributes[k]||'';},removeAttribute(k){delete attributes[k];},querySelectorAll(){return [];},querySelector(s){if(!children.has(s))children.set(s,element());return children.get(s);},appendChild(){},remove(){},focus(){},scrollTo(){},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;}};
}
function env() {
  const elements=new Map(),timers=new Map(),listeners={},storageReads=[],charts=[];let timer=0;
  const doc={cookie:'',hidden:false,visibilityState:'visible',documentElement:element(),addEventListener(k,fn){(listeners[k]||=[]).push(fn);},querySelectorAll(){return [];},querySelector(){return null;},createElement:element,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
  const ctx={console,document:doc,marked:marked.marked,TextEncoder,TextDecoder,URL,URLSearchParams,Intl,Date,JSON,Map,Set,Promise,AbortController,
    navigator:{},location:{href:''},localStorage:{getItem(k){storageReads.push(k);return null;},setItem(){},removeItem(){}},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},matchMedia(){return {matches:false};},addEventListener(){},getComputedStyle(){return {getPropertyValue(){return '';}};},requestAnimationFrame(){return 1;},cancelAnimationFrame(){},setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout(id){timers.delete(id);},setInterval(){return 1;},clearInterval(){},fetch(){throw new Error('Network is forbidden in frontend regression');},confirm(){return true;},NodeFilter:{SHOW_TEXT:4},echarts:{init(){const chart={setOption(o){this.option=o;},dispose(){},resize(){},dispatchAction(){}};charts.push(chart);return chart;}}};
  ctx.window=ctx;vm.createContext(ctx);
  for(const script of scripts)vm.runInContext(fs.readFileSync('static/js/'+script,'utf8'),ctx,{filename:script});
  const app=ctx.App;app.user='test-owner';app._toast=()=>{};app._closeMobileSidebar=()=>{};app._renderWatchlist=()=>{};app._refreshWatchlist=()=>{};
  const rawApi=app.api;app.api=()=>{throw new Error('Unexpected API call');};
  return {ctx,app,doc,elements,timers,storageReads,charts,listeners,rawApi};
}
const response=data=>({ok:true,status:200,json:async()=>data});
const tick=()=>new Promise(setImmediate);
function delayed(app){const queue=[];app.api=(url,options)=>new Promise(resolve=>queue.push({url,options,resolve}));return queue;}
const bar=close=>({ts:Date.parse('2026-10-21T00:00:00+08:00')/1000,open:close,close,low:close,high:close,volume:1});
async function actualChartOrder() {
  assert(scripts.indexOf('app.indicator-chart.js')>scripts.indexOf('app.market.js'));
  for(const zone of ['UTC','America/Los_Angeles','Asia/Shanghai']){process.env.TZ=zone;const {app,charts}=env();app.renderKlineChart(element(),[bar(1)]);assert.equal(charts[0].option.xAxis[0].data[0],'2026-10-21');assert.match(app._quoteTimeLabel({data_kind:'daily_close',data_timestamp:bar(1).ts}),/2026.*10.*21/);assert(!app._quoteTimeLabel({data_kind:'daily_close',data_timestamp:bar(1).ts}).includes('00:00'));}
}
async function klineIdentityAndReverseResponses() {
  const {app}=env();app._currentTicker='AAA';app._lastKline=[bar(111)];app._klineIdentity={ticker:'AAA',period:'1d'};
  app._showMarketWorkspace=()=>{};app._renderQuoteCard=()=>{};app._renderFundamentals=()=>{};
  const queue=delayed(app);app._selectStock('BBB');assert.equal(app._lastKline,null);
  app._selectStock('CCC');queue[1].resolve(response({kline:[bar(333)]}));await tick();queue[0].resolve(response({kline:[bar(222)]}));await tick();
  assert.equal(app._klineIdentity.ticker,'CCC');assert.equal(app._lastKline[0].close,333);
  let payload;app._currentTicker='DDD';app.api=async(url,opts)=>{payload=JSON.parse(opts.body);return response({points:[]});};await app._evalIndicator('indicator','DDD',{});assert(!payload.kline,'old CCC data must never be submitted as DDD');
}
async function lateIndicatorDoesNotApply() {
  const {app}=env();app._currentTicker='A';app._currentPeriod='1d';const queue=delayed(app);
  const first=app._evalIndicator('id','A',{});app._currentTicker='B';app._invalidateKline();queue[0].resolve(response({points:[{t:1,v:9}]}));assert.equal(await first,null);
}
async function savedReportSelection() {
  const {app,doc}=env(),queue=delayed(app);app._renderReportSidebar=()=>{};let rendered;
  app._renderSavedReportDetail=report=>{rendered=report.id;};
  const a=app._openSavedReport('A'),b=app._openSavedReport('B');queue[1].resolve(response({id:'B'}));await b;queue[0].resolve(response({id:'A'}));await a;
  assert.equal(app._activeSavedReportId,'B');assert.equal(rendered,'B');
  const c=app._openSavedReport('C');app._activeSavedReportId='';app._renderReportLibraryHome();queue[2].resolve(response({id:'C'}));await c;assert.notEqual(rendered,'C');assert.match(doc.getElementById('reportsPanelInner').innerHTML,/完成/);
}
async function vibeSelectionAndAction() {
  const {app}=env(),queue=delayed(app);app._renderVibeHistory=()=>{};
  const a=app._openVibePlan('A'),b=app._openVibePlan('B');queue[1].resolve(response({plan:{id:'B',status:'complete',title:'B'}}));await b;queue[0].resolve(response({plan:{id:'A',status:'complete',title:'A'}}));await a;
  assert.equal(app._activeVibePlanId,'B');assert.equal(app._activeVibePlan.id,'B');const cancel=app._cancelVibePlan();assert(queue[2].url.includes('/B/cancel'));queue[2].resolve(response({plan:{id:'B',status:'cancelled'}}));await cancel;
}
async function stoppedVibeRequestCannotReturn() {
  const {app}=env(),queue=delayed(app);app._activeVibePlanId='A';app._pollVibePlan('A');app._stopVibePolling('A');await tick();assert.equal(queue.length,0);assert.equal(app._vibePlans.length,0);assert.equal(app._activeVibePlan,null);
}

async function scanSelectionAndReconnect() {
  const {app,doc,timers}=env(),queue=delayed(app);app._loadRecentScans=()=>{};
  const a=app._openScanJob('A'),b=app._openScanJob('B');queue[1].resolve(response({job:{id:'B',status:'running',matches:[]}}));await b;queue[0].resolve(response({job:{id:'A',status:'completed',matches:[]}}));await a;assert.equal(app._scanJobId,'B');
  app.api=async()=>{throw Error('offline');};await app._pollScanJob();assert.match(doc.getElementById('marketScanState').textContent,/连接中断/);assert(timers.size>0);assert.equal(doc.getElementById('marketScanStart').disabled,true);
  let read;app.api=async url=>{read=url;return response({job:{id:'B',status:'partial',evaluated:2,failed:3,matches:[]}});};const scheduled=[...timers.values()].at(-1);await scheduled.fn();await tick();assert(read.endsWith('/B'));assert.match(doc.getElementById('marketScanState').textContent,/部分完成/);assert.match(doc.getElementById('marketScanResults').innerHTML,/覆盖不完整/);
}
async function capabilitySnapshotDoesNotReexecute() {
  const {app,doc}=env(),urls=[];app._platformCalls.C={call_id:'C',label:'指标计算',status:'completed',target:{view:'indicators',ticker:'600519',id:'i'}};
  app.api=async url=>{urls.push(url);return response({call:app._platformCalls.C,params:{ticker:'600519',period:'1w',params:{window:8}},result:{points:[{t:1,v:7}]}});};await app._openPlatformCall('C');assert.deepEqual(urls,['/api/capabilities/calls/C']);assert.match(doc.getElementById('capabilityResultBody').innerHTML,/600519/);assert.match(doc.getElementById('capabilityResultBody').innerHTML,/1w/);assert.match(doc.getElementById('capabilityResultBody').innerHTML,/window/);
}
async function accountWatchlistAndFailure() {
  const {app,storageReads}=env();app.api=async()=>response({items:[{ticker:'A',name:'A'}]});await app._loadWatchlist();assert(!storageReads.includes('prism-watchlist'));
  app.api=async(url,opts)=>opts?{ok:false,status:503}:response({items:[{ticker:'A',name:'A'}]});assert.equal(await app._addToWatchlist('B','B'),null);assert.equal(app._watchlist.length,1);assert.equal(app._watchlist[0].ticker,'A');
  app.user='second-owner';app.api=async()=>response({items:[]});await app._loadWatchlist();assert.equal(app._watchlist.length,0);
}
async function confirmedWatchlistActivityIsPrivate() {
  const {app}=env(),events=[];let items=[],saveFails=false;
  app._trackUiEvent=(...args)=>events.push(args);
  app.api=async(url,options)=>{
    assert.equal(url,'/api/watchlist');
    if(options){if(saveFails)return {ok:false,status:503};items=JSON.parse(options.body).items;}
    return response({items:items.map(item=>({...item}))});
  };
  assert.equal(await app._addToWatchlist('600519','private stock name'),true);
  assert.deepEqual(events,[['market','watchlist_add']]);
  assert.equal(await app._addToWatchlist('600519','private stock name'),false);
  saveFails=true;assert.equal(await app._removeFromWatchlist('600519'),null);assert.equal(events.length,1);
  assert.equal(app._watchlist.length,1,'failed mutation preserves the confirmed account list');
  saveFails=false;assert.equal(await app._removeFromWatchlist('600519'),true);
  assert.deepEqual(events,[['market','watchlist_add'],['market','watchlist_remove']]);
  assert.equal(await app._removeFromWatchlist('600519'),false);assert.equal(events.length,2);
}
async function analysisShowsOnlyObservedStages() {
  const {app,doc,ctx}=env();let saved;
  const stages=['analysts','debate','research'].map(stage=>{const el=element();el.dataset.stage=stage;return el;});
  doc.querySelectorAll=()=>stages;app._renderAgentGrid=()=>{};app._renderFinalReport=()=>{};app._setAnalysisMode=()=>{};
  ctx.sessionStorage.setItem=(key,value)=>{saved=JSON.parse(value);};app._analysisId='run';app._analysisStreaming=true;
  assert(!/apProgressFill|\d%/.test(app._buildProgressBar()));
  app._handleAnalysisEvent({type:'stage',stage:'queued',completed_nodes:0,progress_pct:40});
  assert.match(doc.getElementById('apStatus').textContent,/尚未开始/);assert.equal(stages.filter(el=>el.classList.contains('active')).length,0);
  app._handleAnalysisEvent({type:'stage',stage:'analysts',completed_nodes:0});
  app._handleAnalysisEvent({type:'agent_done',agent:'market',completed_nodes:1});
  assert.match(doc.getElementById('apProgressState').textContent,/已完成 1 个节点/);
  app._handleAnalysisEvent({type:'stage',stage:'research',completed_nodes:2});
  assert.deepEqual(stages.filter(el=>el.classList.contains('active')).map(el=>el.dataset.stage),['research']);
  assert.equal(saved.completedNodes,2);assert(!('progress' in saved));
  ctx.sessionStorage.getItem=()=>JSON.stringify({...saved,progress:99});app._connectAnalysisStream=()=>{};
  app._analysisCompletedNodes=0;app._restoreAnalysisState();assert.equal(app._analysisCompletedNodes,2);
  app._handleAnalysisEvent({type:'done',report:{},timings:{total_seconds:3}});
  assert.match(doc.getElementById('apProgressState').textContent,/分析完成.*已完成 2 个节点/);
  assert(!doc.getElementById('apProgressState').textContent.includes('%'));
}
async function researchLedgerAndUntrustedData() {
  const {app,doc,ctx}=env();const fixture={run:{run_id:'R',question:'研究',status:'partial',as_of:'2026-10-21T08:00:00+08:00'},documents:[{document_id:'D',title:'<img src=x onerror=evil()>',url:'javascript:evil()',content_scope:'search_snippet',published_at:null}],passages:[{passage_id:'P',document_id:'D',excerpt:'<script>evil()</script>',locator:'搜索摘要'}],claims:[{claim_id:'C',kind:'fact',statement:'候选事实',supports:['P'],citation_status:'valid',semantic_status:'unreviewed'}],report_revisions:[{revision_id:'V',revision:1,created_at:1,narrative:'原版本正文',blocks:[],acceptance_status:'partial'}],relations:[]};
  app.api=async()=>response(fixture);await app._openResearchRun('R');assert.match(doc.getElementById('researchBody').innerHTML,/事实候选/);assert.match(doc.getElementById('researchBody').innerHTML,/语义待核验/);
  app._researchTab='report';app._renderResearch();assert.match(doc.getElementById('researchBody').innerHTML,/原版本正文/);
  app._researchTab='evidence';app._renderResearch();let html=doc.getElementById('researchBody').innerHTML;assert(!html.includes('<img'));assert(!html.includes('href="javascript:'));assert.match(html,/搜索线索/);
  app._showResearchEvidence('claim','C');assert.match(doc.getElementById('researchEvidence').innerHTML,/引用结构有效/);assert(!doc.getElementById('researchEvidence').innerHTML.includes('<script>'));
  app._researchTab='industry';app._renderResearch();html=doc.getElementById('researchBody').innerHTML;for(const label of ['上游供给','认证周期','替代路线','成本传导','下游需求'])assert(html.includes(label));assert.match(html,/尚无可追溯/);
  assert(ctx.ChatRender.renderResearch({run_id:'R',as_of:1}).includes('data-research-run="R"'));
  assert(!/sandbox="[^"]*allow-same-origin/.test(template));
}
async function configuredModelSelection() {
  const {app,doc,ctx}=env();ctx.localStorage.getItem=()=> 'unconfigured';
  app.api=async()=>response([{id:'unconfigured',name:'未配置模型',configured:false,default:false},{id:'configured',name:'可选模型',configured:true,default:true}]);
  await app.loadModels();assert.equal(app.model,'configured');assert.match(doc.getElementById('modelSelect').innerHTML,/unconfigured" disabled/);assert.match(doc.getElementById('modelSelect').title,/以执行结果为准/);
  app.setModel('unconfigured');assert.equal(app.model,'configured');
  app.api=async()=>response([{id:'unconfigured',name:'未配置模型',configured:false,default:false}]);await app.loadModels();assert.equal(app.model,'');assert.equal(doc.getElementById('modelConfigurationNotice').hidden,false);
  let requests=0;ctx.fetch=()=>{requests++;throw Error('must not start');};doc.getElementById('input').value='question';await app.send();assert.equal(requests,0);
}
async function analysisUsesNativeOnce() {
  const {app,doc}=env();let resolve,starts=0,sends=0;
  app._analysisTicker='600519';app._analysisName='合成名称';
  app.newConv=()=>{starts++;return new Promise(r=>{resolve=r;});};
  app.send=async()=>{sends++;assert.match(doc.getElementById('input').value,/600519.*合成名称/);};
  const pending=app._runAnalysis();await app._runAnalysis();assert.equal(starts,1);
  resolve(true);await pending;assert.equal(sends,1);assert.equal(app._analysisStarting,false);
}
async function analysisStartCannotReplaceHistory() {
  const {app}=env(),queue=delayed(app);let resolve,sends=0;
  app._analysisTicker='NEW';app._renderAgentGrid=()=>{};app.switchMode=()=>{};
  app.newConv=()=>new Promise(r=>{resolve=r;});app.send=async()=>{sends++;};
  const started=app._runAnalysis();
  const old=app._openAnalysisRecord('EXISTING','OLD');queue[0].resolve(response({ticker:'OLD',status:'failed'}));await old;
  resolve(true);await started;assert.equal(app._analysisId,'EXISTING');assert.equal(sends,0);
}
async function adminDiagnosticFailuresAreUnknown() {
  for(const kind of ['http','network','invalid-json','invalid-shape']) {
    const {app}=env();let rendered=false;app._renderAdminUsers=()=>{rendered=true;};
    app.api=async url=>{
      if(url==='/api/admin/users')return response({users:[],current_user:'synthetic-admin'});
      if(url.includes('/diagnostics')) {
        if(kind==='network')throw Error('offline');
        if(kind==='http')return {ok:false,status:503};
        if(kind==='invalid-json')return {ok:true,json:async()=>{throw Error('invalid JSON');}};
        return response({});
      }
      return response({});
    };
    await app._loadAdminUsers();assert(rendered,'one failed panel must not prevent the other panels loading');
    const html=app._renderAdminDiagnosticsTab();assert.match(html,/当前状态未知/);assert(!html.includes('admin-stat-card'));
  }
  const {app}=env();app._adminDiagnostics={events:[],stats:{}};
  assert.match(app._renderAdminDiagnosticsTab(),/不代表所有功能正常/);
}
async function adminHealthDoesNotCertifyBusiness() {
  const {app}=env();assert.match(app._renderAdminHealth(),/当前状态未知/);
  app._adminHealth={ok:true,checks:{database:{status:'ok'},native_core:{status:'ok'},tool_catalog:{status:'ok'},provider_configuration:{status:'ok'}},models:[]};
  const html=app._renderAdminHealth();assert.match(html,/本地配置检查/);assert.match(html,/业务验收：待验证/);assert(!html.includes('<span>总览</span>'));
}
async function industryGraphSelection() {
  const {app}=env(),queue=delayed(app);let rendered;app._industryShell=(tab,html)=>html;app._disposeIndustryCharts=()=>{};app._renderIndustryChainDetail=category=>{rendered=category;};
  const a=app._renderIndustryChain('A'),b=app._renderIndustryChain('B');queue[1].resolve(response({nodes:[]}));await b;queue[0].resolve(response({nodes:[]}));await a;assert.equal(rendered,'B');
}
async function researchReverseResponse() {
  const {app,doc}=env(),queue=delayed(app);const a=app._openResearchRun('A'),b=app._openResearchRun('B');queue[1].resolve(response({run:{run_id:'B',question:'B'},claims:[]}));await b;queue[0].resolve(response({run:{run_id:'A',question:'A'},claims:[]}));await a;assert.equal(app._researchBundle.run.run_id,'B');assert.equal(doc.getElementById('researchTitle').textContent,'B');
}

async function vibeSaveVersionAndSelection() {
  const {app}=env(),queue=delayed(app),opened=[],notices=[];app._toast=message=>notices.push(message);app.switchMode=()=>{};app._openSavedReport=async id=>opened.push(id);
  app._activeVibePlanId='A';app._activeVibePlan={id:'A',report_markdown:'original',followups:[{id:'T',status:'complete',answer_markdown:'followup'}]};app._vibeSelectionSeq=1;
  const stale=app._saveVibeVersion('T');assert.deepEqual(JSON.parse(queue[0].options.body),{turn_id:'T'});assert.equal(queue[0].url,'/api/vibe/plans/A/save-report');
  app._activeVibePlanId='B';app._vibeSelectionSeq++;queue[0].resolve(response({ok:true,report:{id:'REPORT-A'}}));await stale;assert.equal(opened.length,0);assert.equal(notices.length,0);
  app._activeVibePlanId='A';app._vibeSelectionSeq++;const saved=app._saveVibeVersion();assert.deepEqual(JSON.parse(queue[1].options.body),{});queue[1].resolve(response({ok:true,report:{id:'ORIGINAL'}}));await saved;assert.deepEqual(opened,['ORIGINAL']);assert.match(notices[0],/原始研究时间保持不变/);
  assert.match(app._vibeFollowupsHtml(app._activeVibePlan.followups),/保存此追问版本/);
}


async function normalApiDeadlineAndCallerCancellation() {
  const {app,ctx,timers,rawApi}=env();app.api=rawApi;app._captureDiagnostic=()=>{};let signal;
  ctx.fetch=(url,options)=>new Promise((resolve,reject)=>{signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});});
  const pending=app.api('/api/market/kline');const scheduled=[...timers.values()].find(item=>item.delay===30000);assert(scheduled);scheduled.fn();await assert.rejects(pending,/执行结果尚未确认/);assert(signal.aborted);assert.equal(timers.size,0);
  const caller=new AbortController();const cancelled=app.api('/api/vibe/plans/A',{signal:caller.signal});caller.abort();await assert.rejects(cancelled,/aborted/);assert.equal(timers.size,0);
}

(async()=>{const cases=[analysisUsesNativeOnce,adminDiagnosticFailuresAreUnknown,adminHealthDoesNotCertifyBusiness,normalApiDeadlineAndCallerCancellation,vibeSaveVersionAndSelection,actualChartOrder,klineIdentityAndReverseResponses,lateIndicatorDoesNotApply,savedReportSelection,vibeSelectionAndAction,stoppedVibeRequestCannotReturn,scanSelectionAndReconnect,capabilitySnapshotDoesNotReexecute,accountWatchlistAndFailure,confirmedWatchlistActivityIsPrivate,analysisShowsOnlyObservedStages,researchLedgerAndUntrustedData,researchReverseResponse,configuredModelSelection,analysisStartCannotReplaceHistory,industryGraphSelection];for(const test of cases)await test();console.log('frontend journeys ok: '+cases.length+' scenarios');})().catch(error=>{console.error(error);process.exitCode=1;});
