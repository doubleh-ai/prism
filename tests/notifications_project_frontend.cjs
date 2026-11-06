/* Unified Work notifications with synthetic project and approval records. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const script=fs.readFileSync('static/js/notifications.js','utf8'),template=fs.readFileSync('templates/partials/work-notifications.html','utf8'),ids=[...template.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
class Element{constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this._text='';this.hidden=false;this.disabled=false;this.listeners={};this.isConnected=true;}set textContent(v){this._text=String(v);this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}append(...items){this.children.push(...items);}appendChild(item){this.append(item);}replaceChildren(...items){this.children=items;this._text='';}addEventListener(name,fn){this.listeners[name]=fn;}showModal(){this.open=true;}close(){this.open=false;this.listeners.close();}focus(){this.focused=true;}}
const entries=[{id:'1',kind:'approval',title:'待处理审批',due_at:null,href:'/projects',read_at:null},{id:'2',kind:'approval_result',title:'审批已通过',due_at:null,href:'/projects',read_at:1},{id:'3',kind:'project_task',title:'项目任务',due_at:'2026-11-05T10:00:00Z',href:'/projects',read_at:null},{id:'4',kind:'approval',title:'不可信地址',due_at:null,href:'https://bad.example',read_at:null},{id:'5',kind:'task',title:'缺少时间',due_at:null,href:'/work',read_at:null}];
const tick=()=>new Promise(setImmediate);
function setup(search=''){
 const nodes=new Map(ids.map(id=>[id,new Element()])),trigger=new Element('button'),requests=[],events={};
 const doc={hidden:false,getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),querySelectorAll:()=>[trigger],querySelector:()=>trigger,addEventListener(k,fn){events[k]=fn;}};
 const context={document:doc,window:null,location:{search,href:'https://local.invalid/work'+search},history:{replaceState(...args){context.replaced=args[2];}},fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),URL,URLSearchParams,Intl,Date,Number,String,Array,Object,JSON,Promise,Error,AbortController,setTimeout,clearTimeout,encodeURIComponent};
 context.window=context;vm.createContext(context);vm.runInContext(script,context);
 return {nodes,trigger,requests,events,context};
}
function reply(req,data,status=200){req.resolve({ok:status===200,status,json:async()=>data});}
async function fill(e,items=entries){
 const start=e.requests.length-1;reply(e.requests[start],{user:'synthetic'});await tick();reply(e.requests[start+1],{notifications:items});await tick();reply(e.requests[start+2],{user:'synthetic'});await tick();
}
async function run(){
 const e=setup();assert.equal(e.requests.length,0,'opening Work must not fetch reminders');
 const underlyingDraft={value:'现有未保存的安排'};
 e.trigger.listeners.click();assert(e.nodes.get('workNotifications').open);await fill(e);
 const rows=e.nodes.get('notificationList').children;assert.equal(rows.length,5);assert.match(rows[0].textContent,/待你审批 · 未读/);assert.match(rows[1].textContent,/审批结果 · 已读/);assert.doesNotMatch(rows[0].textContent+rows[1].textContent,/时间不可用|1970/);assert.match(rows[2].textContent,/项目任务截止/);for(let i=0;i<3;i++)assert.equal(rows[i].children[0].children.find(n=>n.tagName==='A').href,'/projects');assert(!rows[3].children[0].children.some(n=>n.tagName==='A'));assert.match(rows[4].textContent,/时间不可用/);
 const mark=rows[0].children[1];mark.listeners.click();reply(e.requests[3],{user:'synthetic'});await tick();
 assert.equal(e.requests[4].options.method,'POST');assert.equal(e.requests[4].options.headers['X-Prism-Account'],'synthetic');
 e.requests[4].reject(new Error('response lost'));await tick();assert.match(e.nodes.get('notificationNotice').textContent,/结果未知/);
 mark.listeners.click();assert.equal(e.requests.length,5,'unknown write must await reconciliation');
 e.nodes.get('notificationClose').listeners.click();assert(e.trigger.focused);assert.equal(underlyingDraft.value,'现有未保存的安排');
 e.trigger.listeners.click();await fill(e,[]);assert(!e.nodes.get('notificationEmpty').hidden);
 e.nodes.get('notificationRefresh').listeners.click();reply(e.requests[8],{user:'different'});await tick();assert(e.nodes.get('notificationContent').hidden);assert.equal(e.nodes.get('notificationList').children.length,0);assert.match(e.nodes.get('notificationNotice').textContent,/账号已切换/);
 assert(e.requests.every(r=>r.options.cache==='no-store'&&r.options.redirect==='error'));
 const old=setup('?panel=notifications');assert(old.nodes.get('workNotifications').open);await fill(old);old.nodes.get('notificationClose').listeners.click();assert.equal(old.context.replaced,'/work');
 console.log('project notifications frontend ok: contextual drawer, identity, fixed links and unknown writes');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
