/* Execute the shipped operations/admin UI with fake DOM, timers and API only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function environment() {
  const elements=new Map(),listeners={},timers=new Map();let timerId=0;
  const document={visibilityState:'visible',addEventListener(type,fn){listeners[type]=fn;},getElementById(id){if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:''});return elements.get(id);}};
  const app={isAdmin:true,_mode:'admin',user:'fixture-admin',switchMode(mode){this._mode=mode;},api(){throw Error('Unexpected API request');}};
  const context={App:app,document,console,Date,Intl,URL,AbortController,Promise,Map,Set,
    setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);}};
  context.window=context;vm.createContext(context);
  for(const name of ['app.util.js','app.chat-render.js','app.admin.js','app.operations.js'])vm.runInContext(fs.readFileSync('static/js/'+name,'utf8'),context,{filename:name});
  app._adminTab='operations';
  return {app,context,document,elements,listeners,timers};
}
function snapshot(observed=1792555818) {
  const run=(id,kind,status,conclusion)=>({id,kind,name:kind+' fixture',status,conclusion,head_sha:'1234567890abcdef',branch:'fixture-branch',event:'pull_request',created_at:'2026-10-21T03:00:00Z',updated_at:'2026-10-21T03:10:00Z',url:'https://github.com/doubleh-ai/prism/actions/runs/'+id});
  return {observed_at:observed,repository:'doubleh-ai/prism',github:{status:'ok',checked_at:1792555600,stale:false,error:null,runs:[run(1,'ci','completed','success'),run(2,'candidate','in_progress',null),run(3,'deployment','completed','failure')],pull_requests:[{number:8,title:'Fixture PR',draft:true,state:'open',head_sha:'1234567890abcdef',head:'fixture-branch',base:'master',url:'https://github.com/doubleh-ai/prism/pull/8'}]},runtime:{version:'fixture-v1',deployed_revision:'local-fixture-revision',environment:'local',production_verified:false},delivery:{mode:'candidate_only',production_connected:false,can_deploy:false,can_rollback:false,reason:'Production is not connected.'},links:{actions:'https://github.com/doubleh-ai/prism/actions',repository:'https://github.com/doubleh-ai/prism',pull_requests:'https://github.com/doubleh-ai/prism/pulls'}};
}
const response=data=>({ok:true,status:200,json:async()=>data});
const settled=()=>new Promise(setImmediate);

function rendersBoundariesAndSafeLinks() {
  const {app}=environment();app._operationsData=snapshot();
  let html=app._renderOperationsContent();
  for(const text of ['CI 检查','候选构建','生产发布记录','尚未取得可信的生产执行记录','本实例版本，非生产核验','停止剩余步骤不等于回滚','3 分钟','北京时间','打开流程 / 在 GitHub 取消','草稿'])assert(html.includes(text),text);
  assert.equal((html.match(/class="operations-run"/g)||[]).length,2);
  assert(html.includes('当前开放 PR'));assert(!html.includes('草稿与合并状态单独显示'));
  assert(html.includes('href="https://github.com/doubleh-ai/prism/actions/runs/2"'));
  assert(html.includes('<button disabled>部署不可用</button>'));
  assert(!html.includes('onclick='),'the observation page must not contain mutation actions');
  app._operationsData.github.runs[0].conclusion='startup_failure';
  html=app._renderOperationsContent();assert(html.includes('class="operations-state attention">启动失败</span>'),'workflow startup failure must be an explicit failure, not neutral completion');
  app._operationsData.github.pull_requests=[];
  html=app._renderOperationsContent();assert(html.includes('本次快照没有开放 PR'));assert(html.includes('不代表没有已关闭或已合并的记录'));
  app._operationsData=snapshot();
  app._operationsData.github.runs[0].name='<img src=x onerror=alert(1)>';
  app._operationsData.github.runs[0].url='javascript:alert(1)';
  app._operationsData.github.pull_requests[0].title='<script>probe()</script>';
  html=app._renderOperationsContent();assert(!html.includes('<img'));assert(!html.includes('<script>'));assert(!html.includes('href="javascript:'));
  for(const url of ['https://github.com.evil.invalid/doubleh-ai/prism/actions/runs/1','https://github.com/other/repo/actions/runs/1','https://user:password@github.com/doubleh-ai/prism/actions/runs/1','https://github.com/doubleh-ai/prism/settings','java\nscript:void(0)'])assert(!app._operationsLink(url,'open','run').includes('<a '),url);
}
function validationStagesHaveChineseLabelsWithoutProductionClaims() {
  const {app}=environment();
  const labels={finish_stage:'结束隔离验收',diagnose_build:'诊断候选环境',quarantine_failed_prepare:'保留失败现场',integration_astock:'研究引擎联调',integration_cli:'调用协议验证',integration_live:'模型与搜索联调',integration_market:'行情来源验证',validate_legacy_retirement:'退出机制验证',validate_legacy_stop:'停止流程验证',legacy_drain:'等待后台任务保存',switch_stopped:'启动已验收版本'};
  for(const [stage,label] of Object.entries(labels)) {
    const data=snapshot();
    data.github.runs.forEach(run=>{run.status='completed';run.conclusion='success';});
    data.runtime={version:'synthetic-local-build',deployed_revision:'a'.repeat(40),environment:'production',production_verified:false};
    data.production={status:'ok',record:{release_id:'synthetic-validation',commit:'a'.repeat(40),archive_sha256:'b'.repeat(64),stage,status:'completed',updated_at:1792555818,production_verified:false,cutover_occurred:false,steps:[{stage,status:'completed',at:1792555818}]}};
    app._operationsData=data;
    const html=app._renderOperationsContent();
    assert(html.includes(label),stage+' must have a Chinese label');
    assert(!html.includes(' · '+stage+'</p>') && !html.includes('<li>'+stage),stage+' must not appear as an untranslated stage');
    assert(html.includes('生产（尚未核验）') && html.includes('尚无版本已切换的完成证据。'));
    assert(!html.includes('该发布已完成独立验收。') && !html.includes('生产（已验收该版本）'));
    assert(html.includes('通过不代表已经发布。') && html.includes('构建成功不代表生产已更新。'));
  }
}
async function adminPermissionClearsSnapshots() {
  const {app,document,timers}=environment();let calls=0;
  app.isAdmin=false;app._operationsData=snapshot();app.api=async()=>{calls++;return response(snapshot());};
  await app._loadOperations();assert.equal(calls,0);assert(!app._renderOperationsContent().includes('local-fixture-revision'));
  app._renderAdminUsers('fixture-admin');assert.equal(document.getElementById('adminPanelInner').textContent,'无管理权限');
  app.isAdmin=true;app.api=async()=>({ok:false,status:403});await app._loadOperations();
  assert.equal(app._operationsData,null);assert.equal(app._operationsForbidden,true);assert.equal(timers.size,0);
  assert(!document.getElementById('adminOperationsContent').innerHTML.includes('local-fixture-revision'));
  assert(document.getElementById('adminOperationsContent').innerHTML.includes('无权查看'));
}
async function pollsOnlyWhileVisibleWithoutOverlap() {
  const {app,document,listeners,timers}=environment();let resolve,calls=0;
  app.api=()=>{calls++;return new Promise(done=>{resolve=done;});};
  const pending=app._loadOperations();await app._loadOperations();assert.equal(calls,1);assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,20000);
  resolve(response(snapshot()));await pending;assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,30000);
  app.switchMode('chat');assert.equal(timers.size,0);await app._loadOperations();assert.equal(calls,1);
  app._mode='admin';document.visibilityState='hidden';listeners.visibilitychange();await app._loadOperations();assert.equal(calls,1);
  document.visibilityState='visible';listeners.visibilitychange();assert.equal(calls,2);
  const active=app._operationsRequest;document.visibilityState='hidden';listeners.visibilitychange();assert(active.signal.aborted);
  resolve(response(snapshot()));await settled();assert.equal(timers.size,0);
}
async function ignoresLateResponsesAfterLeaving() {
  const {app,document,listeners,timers}=environment();let finishOldJSON;
  app.api=async()=>({ok:true,status:200,json:()=>new Promise(resolve=>{finishOldJSON=resolve;})});
  const first=app._loadOperations();await settled();
  document.visibilityState='hidden';listeners.visibilitychange();
  document.visibilityState='visible';app.api=async()=>response(snapshot(1792555999));listeners.visibilitychange();await settled();
  assert.equal(app._operationsData.observed_at,1792555999);
  finishOldJSON(snapshot(1792555000));await first;
  assert.equal(app._operationsData.observed_at,1792555999,'late JSON must not overwrite the newer snapshot');assert.equal(timers.size,1);
}
async function failuresAndStaleDataStayExplicit() {
  const {app}=environment();const data=snapshot();app.api=async()=>response(data);await app._loadOperations();
  const checked=app._operationsTime(data.github.checked_at);
  app.api=async()=>{throw Error('offline');};await app._loadOperations();
  let html=app._renderOperationsContent();assert(html.includes('本次刷新失败'));assert(html.includes('保留上次观测'));assert(html.includes('local-fixture-revision'));assert(html.includes('GitHub 检查 '+checked));
  const stale=snapshot();stale.github={status:'unavailable',checked_at:null,stale:true,error:'Fixture unavailable',runs:[],pull_requests:[]};app.api=async()=>response(stale);await app._loadOperations();html=app._renderOperationsContent();
  assert(html.includes('GitHub 数据已过期'));assert(html.includes('不能据此判断没有流程'));assert(html.includes('GitHub 检查 未提供'));assert(!html.includes('已取得本次数据'));
}
async function operationsTabUsesItsOwnReadOnlyEndpoint() {
  const {app,document}=environment();const urls=[];
  app.api=async(url,options)=>{urls.push(url);assert(!options.method || options.method==='GET');return response(snapshot());};
  app._adminSetTab('operations');await settled();
  assert.deepEqual(urls,['/api/admin/operations']);assert(document.getElementById('adminPanelInner').innerHTML.includes('发布控制台'));
  assert(document.getElementById('adminPanelInner').innerHTML.includes('App._loadOperations()'));
}
function nativeHealthShowsConfigurationWithoutAnExternalExecutor() {
  const {app}=environment();
  app._adminHealth={ok:true,checks:{native_core:{status:'ok',engine:'prism-native'},tool_catalog:{status:'ok',count:22},provider_configuration:{status:'ok'}},models:[{id:'synthetic',name:'合成模型',configured:true}]};
  const html=app._renderAdminHealth();
  assert(html.includes('Prism Native'));assert(html.includes('22 项'));assert(html.includes('直接 API'));
  assert(html.includes('配置检查不调用模型'));assert(!/Claude|CLI|默认技能|builtin\//i.test(html));
  app._adminHealth.checks.native_core.status='error';
  assert(!/<span>运行器代码<\/span><strong>正常<\/strong>/.test(app._renderAdminHealth()));
}
async function stalledRequestHasABoundedDeadline() {
  const {app,timers}=environment();
  app.api=async(url,options)=>new Promise((resolve,reject)=>{
    assert.equal(options.cache,'no-store');
    options.signal.addEventListener('abort',()=>reject(new Error('Aborted')));
  });
  const pending=app._loadOperations();const deadline=[...timers.values()].find(timer=>timer.delay===20000);assert(deadline);
  deadline.fn();await pending;
  assert.equal(app._operationsLoading,false);assert.equal(app._operationsRequest,null);assert(app._operationsError.includes('刷新失败'));
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,30000);
}
(async()=>{
  rendersBoundariesAndSafeLinks();validationStagesHaveChineseLabelsWithoutProductionClaims();await adminPermissionClearsSnapshots();await pollsOnlyWhileVisibleWithoutOverlap();await ignoresLateResponsesAfterLeaving();await failuresAndStaleDataStayExplicit();await operationsTabUsesItsOwnReadOnlyEndpoint();nativeHealthShowsConfigurationWithoutAnExternalExecutor();await stalledRequestHasABoundedDeadline();
  console.log('operations runtime ok: 9 scenarios');
})().catch(error=>{console.error(error);process.exitCode=1;});
