/* One explicit futures action uses the shipped Native send path, without network. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(setImmediate),script=fs.readFileSync('static/js/app.handoff.js','utf8');
function setup(){
 const input={value:'',dispatchEvent(){},focus(){}},button={disabled:false},notice={hidden:true},symbol={value:'CU'},requests=[],created=[],sent=[];
 const app={user:'owner-A',skill:'prism-research',model:'synthetic-model',_modelsLoaded:true,skills:[{id:'futures-research'}],
  api:(url,opts)=>new Promise(resolve=>requests.push({url,opts,resolve})),setSkill(value){this.skill=value;},
  newConv:async function(){created.push(this.skill);this.convId='synthetic-conversation';this._mode='chat';return true;},
  send:async function(){sent.push({question:input.value,model:this.model,conv:this.convId,skill:this.skill});},_toast(value){this.toast=value;},switchMode(value){this._mode=value;}};
 const context={App:app,Streams:{},confirm:()=>true,Event:class{},document:{getElementById:id=>({input,futuresStartResearch:button,futuresNotice:notice,futureSymbol:symbol,futuresModelLabel:{},modelSelect:{selectedOptions:[{textContent:'合成模型'}]}})[id]}};
 context.window=context;vm.createContext(context);vm.runInContext(script,context);return{context,app,input,button,notice,symbol,requests,created,sent};
}
function identify(e,user='owner-A'){e.requests.at(-1).resolve({ok:true,json:async()=>({user})});}
async function run(){
 const e=setup();assert.equal(e.requests.length,0);assert.equal(e.sent.length,0,'navigation never starts research');
 const first=e.app._startFuturesResearch();assert(e.button.disabled);assert.equal(await e.app._startFuturesResearch(),false);assert.equal(e.requests.length,1);
 identify(e);assert.equal(await first,true);assert.equal(e.created.length,1);assert.equal(e.sent.length,1);assert.equal(e.sent[0].skill,'futures-research');assert.equal(e.sent[0].model,'synthetic-model');assert.equal(e.sent[0].conv,'synthetic-conversation');for(const term of ['铜（CU）','基本面','技术面','成交持仓','证据','情景','风险'])assert(e.sent[0].question.includes(term),term);assert(e.sent[0].question.length<80,'generated question remains readable in the conversation and research library');assert(!e.sent[0].question.includes('futures_snapshot'));assert(!e.button.disabled);
 const changed=setup(),pending=changed.app._startFuturesResearch();identify(changed,'owner-B');assert.equal(await pending,false);assert.equal(changed.created.length,0);assert.equal(changed.sent.length,0);assert.match(changed.notice.textContent,/账号已变化/);
 const unconfigured=setup();unconfigured.app.model='';const none=unconfigured.app._startFuturesResearch();identify(unconfigured);assert.equal(await none,false);assert.equal(unconfigured.created.length,0);
 const draft=setup();draft.input.value='未发送的原问题';draft.context.confirm=()=>false;assert.equal(await draft.app._startFuturesResearch(),false);assert.equal(draft.requests.length,0);assert.equal(draft.input.value,'未发送的原问题');
 const edited=setup();edited.app.newConv=async function(){edited.created.push(1);edited.input.value='等待时新输入的内容';this.convId='new';return true;};const edit=edited.app._startFuturesResearch();identify(edited);assert.equal(await edit,false);assert.equal(edited.sent.length,0);assert.equal(edited.input.value,'等待时新输入的内容');
 const unknown=setup();unknown.app.convId='unconfirmed';unknown.context.Streams.unconfirmed={active:true,offline:true};assert.equal(await unknown.app._startFuturesResearch(),false);assert.equal(unknown.app._mode,'chat');assert.equal(unknown.requests.length,0);
 const invalid=setup();invalid.symbol.value='__proto__';assert.equal(await invalid.app._startFuturesResearch(),false);assert.equal(invalid.requests.length,0);
 console.log('futures Native entry ok: 7 scenarios, one send, account/draft/unknown fences');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
