/* Shipped form handlers against finite HTTP replies; no user content or network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(setImmediate);
const script=fs.readFileSync('static/js/app.hub.js','utf8');
function el(){return {value:'',checked:false,disabled:false,hidden:false,textContent:'',style:{},listeners:{},children:[],
  append(...items){this.children.push(...items);},addEventListener(k,f){this.listeners[k]=f;},replaceChildren(...items){this.children=items;},setAttribute(){}};}
function setup(page='preferences'){
 const fields=Object.fromEntries(['style','horizon','max_position_pct','max_drawdown_pct','allow_leverage'].map(k=>[k,el()]));
 Object.assign(fields.style,{value:'conservative'});fields.horizon.value='long';fields.max_position_pct.value='20';fields.max_drawdown_pct.value='10';
 const submit=el(),form=el();form.elements={namedItem:k=>fields[k]};form.querySelector=()=>submit;
 const els={main:el(),notice:el(),profileForm:form,profileReload:el(),profileVersion:el()},requests=[],listeners={};
 Object.assign(els,{portfolioForm:el(),addPosition:el(),useObservedPriceTime:el(),portfolioReload:el(),portfolioDetails:el()});
 Object.assign(els,{futuresForm:form,futureSymbol:el(),futuresResult:el()});
 const context={console,URL,URLSearchParams,Intl,Date,JSON,Number,String,Promise,AbortController,Object,Error,
  setTimeout,clearTimeout,location:{pathname:'/'+page,search:'',replace(){}},confirm:()=>true,
  addEventListener(k,f){listeners[k]=f;},
  document:{body:{dataset:{page}},getElementById:k=>els[k],createElement:el,
   querySelectorAll:s=>s==='form'?[form]:[],addEventListener(){}},
  fetch(path,options){return new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));}};
 Object.defineProperty(context,'localStorage',{get(){throw Error('private persistent cache forbidden');}});
 context.window=context;vm.createContext(context);vm.runInContext(script,context);return {els,requests,submit,form,fields,listeners};
}
const respond=(req,value,ok=true)=>req.resolve({ok,json:async()=>value});
async function initialFailureBlocksNativeSubmit(){
 const e=setup();assert(e.submit.disabled);respond(e.requests[0],{user:'synthetic-A'});await tick();
 e.requests[1].reject(Error('read failed'));await tick();let prevented=false;
 await e.form.onsubmit({preventDefault(){prevented=true;},currentTarget:e.form});
 assert(prevented);assert(e.submit.disabled);assert.equal(e.requests.length,2);
 e.els.profileReload.onclick();respond(e.requests[2],{profile:{configured:false,version:0}});await tick();assert(!e.submit.disabled);
}
async function accountSwitchBlocksWrite(){
 const e=setup();respond(e.requests[0],{user:'synthetic-A'});await tick();respond(e.requests[1],{profile:{configured:false,version:0}});await tick();
 e.form.listeners.input();let prevented=false;e.listeners.beforeunload({preventDefault(){prevented=true;}});assert(prevented);
 const save=e.form.onsubmit({preventDefault(){},currentTarget:e.form});respond(e.requests[2],{user:'synthetic-B'});await save;
 assert.equal(e.requests.length,3);assert.match(e.els.notice.textContent,/账号已经切换/);
 assert(e.requests.every(r=>r.options.cache==='no-store'));
}
async function failedHandoffPreservesExistingDraft(){
 const draft={value:'existing private draft'},button=el();button.remove=()=>assert.fail('must keep retry');
 const context={URLSearchParams,window:{location:{search:'?futures=RB'}},document:{createElement:()=>button,
  querySelector:()=>({appendChild(){}}),getElementById:()=>draft},App:{convId:'old',setSkill(){},newConv:async()=>false}};
 vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/app.handoff.js','utf8'),context);
 await button.listeners.click();assert.equal(draft.value,'existing private draft');assert(!button.disabled);
}
async function showOfficialWarrants(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();
 e.els.futureSymbol.value='CU';
 const run=e.form.onsubmit({preventDefault(){}}),url='https://www.shfe.com.cn/source';
 assert.equal(e.requests[1].path,'/api/futures/CU/snapshot');
 respond(e.requests[1],{symbol:'CU',name:'铜',latest:{},technical:{},positioning:{},fundamentals:[],gaps:[],source:{},
  warehouse_receipt:{status:'collected',data_as_of:'2026-10-30',table:'铜',row:'总计',quantity:17,change:-2,unit:'吨',source_url:url}});
 await run;
 const rendered=JSON.stringify(e.els.futuresResult);
 assert.match(rendered,/2026-10-30 · 总计 17 吨，较上一期变化 -2 吨/);assert.ok(rendered.includes(url));
}
(async()=>{await initialFailureBlocksNativeSubmit();await accountSwitchBlocksWrite();await failedHandoffPreservesExistingDraft();await showOfficialWarrants();console.log('hub journeys ok: 4 scenarios');})().catch(e=>{console.error(e);process.exitCode=1;});
