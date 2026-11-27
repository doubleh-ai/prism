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
  const doc={cookie:'',hidden:false,visibilityState:'visible',documentElement:element(),body:{dataset:{}},addEventListener(k,fn){(listeners[k]||=[]).push(fn);},querySelectorAll(){return [];},querySelector(){return null;},createElement:element,getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
  const ctx={console,document:doc,marked:marked.marked,TextEncoder,TextDecoder,URL,URLSearchParams,Intl,Date,JSON,Map,Set,Promise,AbortController,Event:class{},
    navigator:{},location:{href:'',pathname:'/prism',search:''},localStorage:{getItem(k){storageReads.push(k);return null;},setItem(){},removeItem(){}},sessionStorage:{getItem(){return null;},setItem(){},removeItem(){}},matchMedia(){return {matches:false};},addEventListener(){},getComputedStyle(){return {getPropertyValue(){return '';}};},requestAnimationFrame(){return 1;},cancelAnimationFrame(){},setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout(id){timers.delete(id);},setInterval(){return 1;},clearInterval(){},fetch(){throw new Error('Network is forbidden in frontend regression');},confirm(){return true;},NodeFilter:{SHOW_TEXT:4},echarts:{init(){const chart={setOption(o){this.option=o;},dispose(){},resize(){},dispatchAction(){}};charts.push(chart);return chart;}}};
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
  assert(scripts.indexOf('app.copilot.js')>scripts.indexOf('app.chat.js'));
  for(const zone of ['UTC','America/Los_Angeles','Asia/Shanghai']){process.env.TZ=zone;const {app,charts}=env();app.renderKlineChart(element(),[bar(1)]);assert.equal(charts[0].option.xAxis[0].data[0],'2026-10-21');assert.match(app._quoteTimeLabel({data_kind:'daily_close',data_timestamp:bar(1).ts}),/2026.*10.*21/);assert(!app._quoteTimeLabel({data_kind:'daily_close',data_timestamp:bar(1).ts}).includes('00:00'));}
}
async function klineIdentityAndReverseResponses() {
  const {app}=env();app._currentTicker='AAA';app._lastKline=[bar(111)];app._klineIdentity={ticker:'AAA',period:'1d'};
  app._showMarketWorkspace=()=>{};app._renderQuoteCard=()=>{};app._renderFundamentals=()=>{};app._renderStockIndustryAnchor=()=>{};
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
  const c=app._openSavedReport('C');app._activeSavedReportId='';app._renderReportLibraryHome();queue[2].resolve(response({id:'C'}));await c;assert.notEqual(rendered,'C');assert.match(doc.getElementById('reportsPanelInner').innerHTML,/还没有保存的研究记录/);
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
async function restoredConversationShowsItsResearchMethod() {
  const {app,doc}=env();app.skills=[{id:'prism-research',name:'综合研究'},{id:'futures-research',name:'期货研究'}];
  app._refreshReportsButton=()=>{};app.loadConvs=()=>{};app._loadConversationCalls=()=>{};app._restoreNativeStream=()=>{};
  app.api=async()=>response({id:'saved-futures',skill:'futures-research',messages:[{role:'user',content:'研究铜'}]});
  app.renderSkillsBar();assert.match(doc.getElementById('composerChips').innerHTML,/综合研究/);
  await app.openConv('saved-futures');
  assert.equal(app.skill,'futures-research');assert.match(doc.getElementById('composerChips').innerHTML,/期货研究/);assert(!doc.getElementById('composerChips').innerHTML.includes('综合研究'));
  app.api=async()=>response({id:'saved-general',skill:'prism-research',messages:[{role:'user',content:'普通问题'}]});
  await app.openConv('saved-general');assert.equal(app.skill,'prism-research');assert.match(doc.getElementById('composerChips').innerHTML,/综合研究/);
}
async function researchReverseResponse() {
  const {app,doc}=env(),queue=delayed(app);const a=app._openResearchRun('A'),b=app._openResearchRun('B');queue[1].resolve(response({run:{run_id:'B',question:'B'},claims:[]}));await b;queue[0].resolve(response({run:{run_id:'A',question:'A'},claims:[]}));await a;assert.equal(app._researchBundle.run.run_id,'B');assert.equal(doc.getElementById('researchTitle').textContent,'B');
}
async function normalApiDeadlineAndCallerCancellation() {
  const {app,ctx,timers,rawApi}=env();app.api=rawApi;app._captureDiagnostic=()=>{};let signal;
  ctx.fetch=(url,options)=>new Promise((resolve,reject)=>{signal=options.signal;signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true});});
  const pending=app.api('/api/market/kline');const scheduled=[...timers.values()].find(item=>item.delay===30000);assert(scheduled);scheduled.fn();await assert.rejects(pending,/执行结果尚未确认/);assert(signal.aborted);assert.equal(timers.size,0);
  const caller=new AbortController();const cancelled=app.api('/api/reports/library/A',{signal:caller.signal});caller.abort();await assert.rejects(cancelled,/aborted/);assert.equal(timers.size,0);
}
async function copilotThreeStatesAndContextPerception() {
  const {app,doc}=env();
  app._trackUiEvent=()=>{};
  assert.equal(app._copilotState,'closed');
  app.openCopilot('docked');
  assert.equal(app._copilotState,'docked');
  assert.equal(doc.getElementById('copilotShell').getAttribute('data-copilot-state'),'docked');
  assert(doc.getElementById('appShell').classList.contains('copilot-split'));
  app.toggleCopilotWide();
  assert.equal(app._copilotState,'expanded');
  assert(doc.getElementById('appShell').classList.contains('copilot-wide'));
  app._mode='market';app._marketWorkspaceTab='quote';app._currentTicker='600519';app._currentName='贵州茅台';app._currentPeriod='1w';
  const ctx=app._currentPageContext();
  assert.equal(ctx.surface,'stock_quote');assert.equal(ctx.ticker,'600519');assert.match(ctx.summary,/600519.*贵州茅台/);
  app.closeCopilot();
  assert.equal(app._copilotState,'closed');assert(!doc.getElementById('appShell').classList.contains('copilot-split'));
}
async function personaProposalHumanDecisionAndEscaping() {
  const {app,doc}=env();const calls=[];
  app.api=async(url,opts)=>{
    calls.push({url,opts});
    if(url==='/api/preferences/persona'){
      return response({
        profile:{configured:true,version:2,archetype:'value',style:'conservative',horizon:'long',max_position_pct:25,max_drawdown_pct:15,allow_leverage:false,negative_rules:['排除 ST']},
        archetypes:[{id:'value',name:'价值基本面派',tagline:'ROE'}],
        traits:[{id:'t1',category:'focus',trait_text:'关注经营现金流',source:'confirmed'}],
        pending_proposals:calls.length===1?[{id:'prop-1',trait_text:'<img src=x onerror=alert(1)>严控商誉占比',category:'avoid'}]:[],
        journal_stats:{total:3,supported:2,contradicted:1,win_rate_pct:66.7,bias_hint:'胜率 66.7%'}
      });
    }
    return response({ok:true,decision:'accepted'});
  };
  await app.loadPersona();
  assert.match(doc.getElementById('copilotPersonaBtn').textContent,/价值基本面派/);
  const banner=doc.getElementById('copilotProposalBar');
  assert.equal(banner.hidden,false);
  assert(!banner.innerHTML.includes('<img'));assert(banner.innerHTML.includes('&lt;img'));
  await app.decidePersonaProposal('prop-1','accept');
  assert.equal(calls[1].url,'/api/preferences/proposals/prop-1/decide');
  assert.equal(JSON.parse(calls[1].opts.body).decision,'accept');
  assert.equal(banner.hidden,true);
}
async function marketOverviewCockpitAndJournalVaultRenderSafely() {
  const {app,doc}=env();
  app._currentTicker=null;
  app._watchlist=[];
  app._showMarketEmpty();
  const emptyHtml=doc.getElementById('marketPanelInner').innerHTML;
  assert.match(emptyHtml,/选择或搜索标的开始看盘/);
  app._watchlist=[{ticker:'300750',name:'<script>bad()</script>宁德时代',price:250.5,pct:2.35}];
  app._showMarketEmpty();
  const marketHtml=doc.getElementById('marketPanelInner').innerHTML;
  assert.match(marketHtml,/我的自选股/);
  assert(!marketHtml.includes('<script>'));
  app.api=async()=>response({
    stats:{total:2,supported:1,contradicted:0,pending:1,equity_total:1,futures_total:1,win_rate_pct:100,bias_hint:'多空平衡'},
    items:[{id:'eq-1',asset_class:'equity',code:'300750',name:'宁德时代',direction:'bullish',headline:'<img src=x>缩量企稳',choice_label:'看多 · 参考价 240.00',status:'pending',saved_at:'2026-11-27T10:00:00+08:00',href:'/prism?mode=market&ticker=300750'}]
  });
  await app._loadDecisionJournal();
  const journalHtml=doc.getElementById('journalVaultPanel').innerHTML;
  assert.match(journalHtml,/累计记录判断/);assert(!journalHtml.includes('<img'));assert(journalHtml.includes('&lt;img'));
}

(async()=>{const cases=[restoredConversationShowsItsResearchMethod,normalApiDeadlineAndCallerCancellation,actualChartOrder,klineIdentityAndReverseResponses,lateIndicatorDoesNotApply,savedReportSelection,scanSelectionAndReconnect,capabilitySnapshotDoesNotReexecute,accountWatchlistAndFailure,confirmedWatchlistActivityIsPrivate,researchLedgerAndUntrustedData,researchReverseResponse,configuredModelSelection,copilotThreeStatesAndContextPerception,personaProposalHumanDecisionAndEscaping,marketOverviewCockpitAndJournalVaultRenderSafely];for(const test of cases)await test();console.log('frontend journeys ok: '+cases.length+' scenarios');})().catch(error=>{console.error(error);process.exitCode=1;});
