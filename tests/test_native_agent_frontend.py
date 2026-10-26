"""Exercise shipped native chat JavaScript without a browser network or model."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

RUNTIME = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto').webcrypto;
function element() {
  const classes = new Set();
  return {innerHTML:'',textContent:'',value:'',style:{},dataset:{},disabled:false,
    classList:{add(...n){n.forEach(x=>classes.add(x));},remove(...n){n.forEach(x=>classes.delete(x));},contains(n){return classes.has(n);},toggle(){}},
    addEventListener(){},setAttribute(){},querySelectorAll(){return [];},querySelector(){return null;},appendChild(){},focus(){},scrollTo(){}};
}
function environment() {
  const elements = new Map(), notices = [];
  const document = {cookie:'csrf_token=synthetic',documentElement:element(),addEventListener(){},querySelectorAll(){return [];},querySelector(){return null;},createElement:element,
    getElementById(id){if(!elements.has(id))elements.set(id,element());return elements.get(id);}};
  const context = {console,document,crypto,TextDecoder,TextEncoder,AbortController,URL,Intl,Date,JSON,Map,Set,Promise,Uint8Array,
    navigator:{},location:{href:''},localStorage:{getItem(){return null;},setItem(){},removeItem(){}},sessionStorage:{getItem(){return null;},setItem(){}},
    addEventListener(){},matchMedia(){return {matches:false};},requestAnimationFrame(){return 1;},cancelAnimationFrame(){},setInterval(){return 1;},clearInterval(){},setTimeout,clearTimeout,
    fetch(){throw Error('Unexpected network attempt');},NodeFilter:{SHOW_TEXT:4}};
  context.window=context;vm.createContext(context);
  for (const name of ['app.util.js','app.chat-render.js','app.core.js','app.chat.js']) vm.runInContext(fs.readFileSync('static/js/'+name,'utf8'),context,{filename:name});
  const app=context.App;
  Object.assign(app,{user:'owner',convId:'conv',msgs:[],skills:[{id:'prism-research',name:'综合研究'},{id:'bottleneck-hunter',name:'产业链研究'},{id:'serenity',name:'公司研究'}],
    renderAll(){},renderOne(){},loadConvs:async()=>{},_refreshReportsButton(){},_closeMobileSidebar(){},_buildTOC(){},_scrollToBottom(){},_startTypewriter(){},_stopTypewriter(){},
    _toast(message){notices.push(message);},selectAC(){return false;}});
  return {app,context,document,notices};
}
function frame(seq, fields={}, status='running', run='native-run') {
  return {...fields,agent:{run_id:run,seq,status,engine:'prism-native',workflow:'prism-research'}};
}
function response(frames, {readError=false, split=false, status=200, done=false}={}) {
  let text=frames.map(f=>'data: '+JSON.stringify(f)+'\r\n\r\n').join('')+(done?'data: [DONE]\n\n':'');
  const bytes=new TextEncoder().encode(text);
  const chunks=split?Array.from(bytes,b=>Uint8Array.of(b)):[bytes];let at=0,cancelled=false;
  return {ok:status===200,status,body:{getReader(){return {
    async read(){if(cancelled)return {done:true};if(at<chunks.length)return {done:false,value:chunks[at++]};if(readError)throw new TypeError('synthetic broken connection');return {done:true};},
    async cancel(){cancelled=true;},releaseLock(){}};}}};
}
function stream() {return {convId:'conv',msgs:[{}],msgIdx:0,lastSeq:0};}
async function onePostReplayAndDedupe() {
  const {app,context,document}=environment(), requests=[];
  document.getElementById('input').value='研究这个产业';
  const call={call_id:'call-1',status:'completed',label:'指标',target:{view:'indicators'}};
  context.fetch=async(url,options)=>{
    requests.push({url,options});
    if(requests.length===1)return response([frame(1,{text:'你'},'queued'),frame(2,{platform_call:call})]);
    assert.equal(options.method,'GET');assert.equal(url,'/api/agent/runs/native-run/stream?after_seq=2');
    return response([frame(1,{text:'你'},'queued'),frame(2,{platform_call:call}),frame(3,{text:'好'}),frame(4,{report_status:'unreviewed'},'completed')],{split:true,done:true});
  };
  await app.send();
  assert.equal(requests.length,2);assert.equal(requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(requests[0].url,'/api/agent/chat/stream');
  const body=JSON.parse(requests[0].options.body);assert.match(body.request_id,/^[a-zA-Z0-9-]{20,64}$/);assert.equal(body.skill,'prism-research');assert(!('extra_skills' in body));
  assert.equal(app.msgs[1].content,'你好');assert.equal(app.msgs[1]._chunks.filter(c=>c.t==='platform_call').length,1);
  assert.equal(app.msgs[1].agent.seq,4);assert.equal(app.msgs[1].agent.status,'completed');
}
async function readerAndGetFailuresOnlyReplayOriginalRun() {
  const {app,context}=environment(), s=stream(), seen=[], urls=[];
  context.fetch=async(url,options)=>{urls.push(url);assert.equal(options.method,'GET');if(urls.length===1)throw new TypeError('offline');return response([frame(1,{text:'旧'}),frame(2,{text:'新'},'partial')],{split:true});};
  await app._readNativeStream(s,response([frame(1,{text:'旧'})],{readError:true}),p=>{if(p.text)seen.push(p.text);});
  assert.deepEqual(seen,['旧','新']);assert.equal(urls.length,2);assert(urls.every(u=>u.endsWith('after_seq=1')));assert.equal(s.agentStatus,'partial');
}
async function exhaustedReplayIsBounded() {
  const {app,context}=environment(),s=stream();let gets=0;
  context.fetch=async(url,options)=>{assert.equal(options.method,'GET');gets++;return response([]);};
  await assert.rejects(app._readNativeStream(s,response([frame(1,{text:'保留'})]),()=>{}),/不会重复提交/);
  assert.equal(gets,3);assert.equal(s.lastSeq,1);
}
async function unknownRunNeverRetries() {
  const {app,context}=environment();let requests=0;context.fetch=async()=>{requests++;throw Error('forbidden');};
  await assert.rejects(app._readNativeStream(stream(),response([]),()=>{}),/不会重复提交/);assert.equal(requests,0);
}
async function sequenceGapRecoversWithoutSkipping() {
  const {app,context}=environment(),s=stream(),seen=[];let gets=0;
  context.fetch=async(url)=>{gets++;assert(url.endsWith('after_seq=1'));return response([frame(2,{text:'二'}),frame(3,{text:'三'},'completed')]);};
  await app._readNativeStream(s,response([frame(1,{text:'一'}),frame(3,{text:'三'})]),p=>seen.push(p.text));
  assert.deepEqual(seen,['一','二','三']);assert.equal(gets,1);
}
async function terminalAndDoneDoNotReconnect() {
  for(const status of ['completed','partial','failed','cancelled','done']) {
    const {app,context}=environment(),s=stream();context.fetch=()=>{throw Error('terminal must not reconnect');};
    await app._readNativeStream(s,response([frame(1,{error:status==='failed'?'模型未完成':undefined},status)]),()=>{});assert.equal(s.terminal,true);
  }
  const {app,context}=environment(),s=stream();context.fetch=()=>{throw Error('DONE must not reconnect');};
  await app._readNativeStream(s,response([frame(1,{text:'内容'})],{done:true}),()=>{});assert.equal(s.doneReceived,true);
}
async function ownershipErrorsAndMalformedFrames() {
  const {app,context}=environment(),s=stream();let gets=0;
  context.fetch=async()=>{gets++;return response([],{status:404});};
  await assert.rejects(app._readNativeStream(s,response([frame(1,{text:'内容'})]),()=>{}),/404/);assert.equal(gets,1);
  const other=environment();let net=0;other.context.fetch=()=>{net++;throw Error('forbidden');};
  await assert.rejects(other.app._readNativeStream(stream(),response([frame(1),frame(2,{},'running','another-run')]),()=>{}),/不匹配/);assert.equal(net,0);
  const bad=environment(),b=stream();let reads=0,replays=0;
  const corrupt={ok:true,status:200,body:{getReader(){return {async read(){return reads++?{done:true}:{done:false,value:new TextEncoder().encode('data: '+JSON.stringify(frame(1))+'\n\ndata: {RAW_PRIVATE_FRAGMENT\n\n')};},async cancel(){}};}}};
  bad.context.fetch=async()=>{replays++;return response([]);};
  await assert.rejects(bad.app._readNativeStream(b,corrupt,()=>{}),error=>!error.message.includes('RAW_PRIVATE_FRAGMENT'));
  assert.equal(replays,3);assert.equal(b.lastSeq,1);
}
async function cancelUsesNativeOwnerAndRun() {
  const {app,context,notices}=environment(),s=stream();s.runId='native-run';s.active=true;let aborted=false,readCancelled=false,request,confirm,historyReads=0;
  s.transportController={abort(){aborted=true;}};s.reader={async cancel(){readCancelled=true;}};context.Streams.conv=s;
  app.openConv=async id=>{assert.equal(id,'conv');historyReads++;};
  app.api=async(url,options)=>{
    if(url.endsWith('/cancel')){request={url,body:JSON.parse(options.body)};return {ok:true,json:async()=>({ok:true,run_id:'native-run',status:'running'})};}
    assert.equal(url,'/api/agent/runs/native-run');return new Promise(resolve=>{confirm=resolve;});
  };
  const pending=app.stopStream();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(aborted,false);assert.equal(readCancelled,false);assert.equal(context.Streams.conv,s);assert.equal(s.stopping,true);assert.equal(historyReads,0);
  confirm({ok:true,json:async()=>({run:{run_id:'native-run',status:'cancelled'}})});await pending;
  assert(aborted&&readCancelled);assert.equal(request.url,'/api/agent/chat/cancel');assert.deepEqual(request.body,{conv_id:'conv',run_id:'native-run'});assert.equal(notices.length,0);assert.equal(historyReads,1);assert.equal(context.Streams.conv,undefined);
  const unknown=stream();unknown.active=true;context.Streams.conv=unknown;let unknownAborted=false;unknown.transportController={abort(){unknownAborted=true;}};
  app.api=async()=>({ok:true,json:async()=>({ok:false})});await app.stopStream();assert.match(notices.at(-1),/未确认后台取消成功/);assert.equal(context.Streams.conv,unknown);assert.equal(unknownAborted,false);assert.equal(historyReads,1);
  app.api=async()=>({ok:true,json:async()=>({ok:true,run_id:'discovered-run',status:'cancelled'})});await app.stopStream();assert.equal(unknown.runId,'discovered-run');assert.equal(context.Streams.conv,undefined);assert.equal(historyReads,2);
}
function historyPayload(status='running') {
  const call={call_id:'history-call',status:'completed',label:'指标',conv_id:'conv',target:{view:'indicators'}};
  const ref={url:'https://www.sse.com.cn/qa',title:'合成公开来源'};
  return {call,ref,payload:{text:'保存的进度',chunks:[{t:'text',c:'保存的进度'},{t:'platform_call',call},{t:'ref',...ref}],refs:[ref],research:{as_of:'2026-10-25'},native_agent:{run_id:'history-run',status,engine:'prism-native'}}};
}
async function refreshRestoresGetOnlyAndRebuildsCardsOnce() {
  const {app,context,document}=environment(),{call,ref,payload}=historyPayload();let deliver;const urls=[];
  app.api=async url=>{assert.equal(url,'/api/convs/conv');return {ok:true,json:async()=>({messages:[{role:'user',content:'原问题'},{role:'assistant',content:JSON.stringify(payload)}]})};};
  context.fetch=(url,options)=>{urls.push({url,method:options.method});return new Promise(resolve=>{deliver=resolve;});};
  await app.openConv('conv');const restored=context.Streams.conv;
  assert(restored&&restored.active);assert.equal(restored.runId,'history-run');assert.equal(document.getElementById('sendBtn').title,'停止生成');
  document.getElementById('input').value='不要重复支付';await app.send();assert.equal(urls.length,1);
  deliver(response([frame(1,{},'queued','history-run'),frame(2,{platform_call:call},'running','history-run'),frame(3,{text:'保存的进度'},'running','history-run'),frame(4,{ref},'running','history-run'),frame(5,{report_status:'unreviewed',report_files:['qa.html']},'completed','history-run')]));
  await restored.promise;
  assert.deepEqual(urls,[{url:'/api/agent/runs/history-run/stream?after_seq=0',method:'GET'}]);
  assert.equal(app.msgs[1].content,'保存的进度');assert.equal(app.msgs[1]._chunks.filter(c=>c.t==='platform_call').length,1);assert.equal(app.msgs[1]._refs.length,1);assert.deepEqual(Array.from(app.msgs[1]._reportFiles),['qa.html']);assert.equal(context.Streams.conv,undefined);assert.equal(document.getElementById('sendBtn').title,'发送 (Enter)');
}
async function restoredOfflineRetainsProgressAndReopensSameCursor() {
  const {app,context,document}=environment(),{payload}=historyPayload();app.msgs=[{role:'assistant',content:JSON.stringify(payload)}];let requests=[];
  context.fetch=async(url,options)=>{requests.push({url,method:options.method});return response([frame(1,{},'queued','history-run')]);};
  assert.equal(app._restoreNativeStream('conv'),true);const restored=context.Streams.conv;await restored.promise;
  assert.equal(requests.length,4);assert.equal(restored.lastSeq,1);assert.equal(restored.active,true);assert.equal(restored.offline,true);assert.equal(app.msgs[0].content,'保存的进度');assert.equal(document.getElementById('sendBtn').title,'停止生成');
  // Reopening the same conversation attaches only to the original run/cursor.
  context.fetch=async(url,options)=>{requests.push({url,method:options.method});return response([frame(2,{text:'保存的进度'},'running','history-run'),frame(3,{text:'后续'},'completed','history-run')]);};
  await app.openConv('conv');await restored.promise;
  assert.equal(requests.at(-1).url,'/api/agent/runs/history-run/stream?after_seq=1');assert(requests.every(r=>r.method==='GET'));assert.equal(app.msgs[0].content,'保存的进度后续');assert.equal(context.Streams.conv,undefined);
}
async function restoredRunDoesNotRenderIntoAnotherConversation() {
  const {app,context}=environment(),{payload}=historyPayload();app.msgs=[{role:'assistant',content:JSON.stringify(payload)}];let finish,renders=0;
  context.fetch=()=>new Promise(resolve=>{finish=resolve;});app._restoreNativeStream('conv');const restored=context.Streams.conv;
  const other=[{role:'assistant',content:'另一个会话'}];app.convId='other';app.msgs=other;app.renderOne=()=>{renders++;};
  finish(response([frame(1,{text:'原会话完成'},'completed','history-run')]));await restored.promise;
  assert.equal(other[0].content,'另一个会话');assert.equal(renders,0);assert.equal(restored.msgs[0].content,'原会话完成');assert.equal(context.Streams.conv,undefined);
  app.convId='conv';app.msgs=[{role:'assistant',content:JSON.stringify(historyPayload('completed').payload)}];assert.equal(app._restoreNativeStream('conv'),false);
}
async function disconnectedSendStillOwnsStopControl() {
  const {app,context,document}=environment();let gets=0;
  context.fetch=async(url,options)=>{if(options.method==='POST')return response([frame(1,{text:'已有进度'})]);gets++;return response([]);};
  document.getElementById('input').value='保留真实后台状态';await app.send();
  assert.equal(gets,3);assert.equal(context.Streams.conv.active,true);assert.equal(document.getElementById('sendBtn').title,'停止生成');
}
async function workflowsAreSingleAndNative() {
  const {app,document}=environment();let url;app.api=async(u)=>{url=u;return {ok:true,json:async()=>app.skills};};
  await app.loadSkills();assert.equal(url,'/api/agent/workflows');app._toggleSkill('serenity');assert.equal(app.skill,'serenity');assert.deepEqual(Array.from(app._activeSkills),['serenity']);
  app._toggleSkill('bottleneck-hunter');assert.deepEqual(Array.from(app._activeSkills),['bottleneck-hunter']);app.clearSkill();assert.equal(app.skill,'prism-research');
  app._renderSkillDrawerItems(document.getElementById('skillDrawer'));assert.match(document.getElementById('skillDrawer').innerHTML,/研究方式 · 单选/);assert(!document.getElementById('skillDrawer').innerHTML.includes('cn'));
  app.skill='legacy-method';app.renderSkillsBar();assert.match(document.getElementById('composerChips').innerHTML,/综合研究/);
  app.api=async()=>{throw Error('offline');};await app.loadSkills();assert.equal(app._workflowsUnavailable,true);assert.equal(app.skills.length,0);
}
async function nextQuestionHasDifferentRequestId() {
  const {app,context,document}=environment(),ids=[];
  context.fetch=async(url,options)=>{assert.equal(url,'/api/agent/chat/stream');ids.push(JSON.parse(options.body).request_id);return response([frame(1,{text:'完成'},'completed','run-'+ids.length)]);};
  document.getElementById('input').value='第一条';await app.send();document.getElementById('input').value='第二条';await app.send();assert.equal(ids.length,2);assert.notEqual(ids[0],ids[1]);
}
async function doubleSendAndWorkflowShortcutDoNotCreateTwoRuns() {
  const {app,context,document}=environment();let finish,calls=0;
  app.api=()=>{throw Error('A workflow shortcut must not wait on another mutation before owning its stream');};
  context.fetch=(url,options)=>{calls++;assert.equal(JSON.parse(options.body).skill,'serenity');return new Promise(resolve=>{finish=resolve;});};
  document.getElementById('input').value='/serenity 公司研究';
  const first=app.send();await app.send();assert.equal(calls,1);
  finish(response([frame(1,{text:'完成'},'completed')]));await first;assert.equal(app.msgs.length,2);
}
async function cancellationDiscardsLateTransportFrames() {
  const {app,context}=environment(),s=stream();let finish,seen=0;
  context.fetch=()=>{throw Error('Cancellation must never reconnect');};
  const initial={ok:true,status:200,body:{getReader(){return {read:()=>new Promise(resolve=>{finish=resolve;}),async cancel(){}};}}};
  const pending=app._readNativeStream(s,initial,()=>{seen++;});
  s.cancelled=true;finish({done:false,value:new TextEncoder().encode('data: '+JSON.stringify(frame(1,{text:'迟到'}))+'\n\n')});
  await pending;assert.equal(seen,0);assert.equal(s.lastSeq,0);
}
async function unknownWorkflowCommandRemainsUserText() {
  const {app,context,document}=environment();let question;
  context.fetch=async(url,options)=>{question=JSON.parse(options.body).question;return response([frame(1,{text:'完成'},'completed')]);};
  document.getElementById('input').value='/unknown 保留完整问题';await app.send();assert.equal(question,'/unknown 保留完整问题');
}
(async()=>{
  await onePostReplayAndDedupe();await readerAndGetFailuresOnlyReplayOriginalRun();await exhaustedReplayIsBounded();await unknownRunNeverRetries();
  await sequenceGapRecoversWithoutSkipping();await terminalAndDoneDoNotReconnect();await ownershipErrorsAndMalformedFrames();await cancelUsesNativeOwnerAndRun();await workflowsAreSingleAndNative();await nextQuestionHasDifferentRequestId();await doubleSendAndWorkflowShortcutDoNotCreateTwoRuns();await cancellationDiscardsLateTransportFrames();await unknownWorkflowCommandRemainsUserText();
  await refreshRestoresGetOnlyAndRebuildsCardsOnce();await restoredOfflineRetainsProgressAndReopensSameCursor();await restoredRunDoesNotRenderIntoAnotherConversation();await disconnectedSendStillOwnsStopControl();
  console.log('native frontend runtime ok: 17 scenarios');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def test_native_frontend_replay_cancel_and_workflows():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, "-e", RUNTIME],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "native frontend runtime ok: 17 scenarios" in result.stdout


def test_native_entry_and_catalog_are_explicit():
    chat = (ROOT / "static/js/app.chat.js").read_text()
    template = (ROOT / "templates/app.html").read_text()
    workbench = (ROOT / "static/js/app.workbench.js").read_text()
    assert "fetch('/api/agent/chat/stream'" in chat
    assert "'/api/agent/chat/cancel'" in chat
    assert "'/api/agent/workflows'" in chat
    assert "'/api/chat/stream'" not in chat
    assert "'/api/chat/respond'" not in chat
    assert "'/api/chat/cancel'" not in chat
    assert "研究技能" not in template
    assert "'/api/agent/tools'" in workbench
