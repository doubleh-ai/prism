/* Shipped research library: context switches preserve drafts and identity fences. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const knowledge=fs.readFileSync('static/js/knowledge.js','utf8'),reports=fs.readFileSync('static/js/app.reports.js','utf8');
const markup=fs.readFileSync('templates/partials/research-knowledge.html','utf8')+fs.readFileSync('templates/app.html','utf8');
const tick=()=>new Promise(setImmediate);
class Element{
 constructor(){this.children=[];this.listeners={};this.style={};this.value='';this.hidden=false;this.attributes={};this.textContent='';this.classList={remove(){}};}
 append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;}addEventListener(k,fn){this.listeners[k]=fn;}setAttribute(k,v){this.attributes[k]=v;}scrollIntoView(){this.scrolled=true;}
}
function setup(){
 const nodes=new Map([...markup.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()])),requests=[],events={},button=new Element();
 nodes.get('knowledgeReviewForm').querySelector=()=>button;
 nodes.get('knowledgeReviewForm').reset=()=>{for(const name of ['knowledgeNote','knowledgeUntil','knowledgeReplacement'])nodes.get(name).value='';};
 const document={hidden:false,getElementById:id=>nodes.get(id),createElement:()=>new Element(),addEventListener(k,fn){events[k]=fn;}};
 const context={document,window:null,confirm:()=>true,AbortController,setTimeout,clearTimeout,URL,URLSearchParams,Intl,Date,Number,String,Array,Object,JSON,Error,Promise,encodeURIComponent,esc:x=>String(x),escAttr:x=>String(x),fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),addEventListener(){},App:{_mode:'chat',_closeMobileSidebar(){},api:async()=>({ok:true,json:async()=>({reports:[]})}),switchMode(mode){this._mode=mode;if(mode==='reports')this._initReports();}}};
 context.window=context;vm.createContext(context);vm.runInContext(knowledge,context);vm.runInContext(reports,context);return{context,nodes,requests,events,button};
}
const reply=(req,data,status=200)=>req.resolve({ok:status===200,status,json:async()=>data});
const me={user:'synthetic'};
const claim={id:'claim-one',statement:'合成研究结论',run_id:'run-one',effective_state:'candidate',citation_status:'matched',version:0};
async function initial(e){reply(e.requests[0],me);await tick();reply(e.requests[1],{items:[claim]});await tick();reply(e.requests[2],me);await tick();}
async function detail(e,start,item=claim){reply(e.requests[start],me);await tick();reply(e.requests[start+1],{item,sources:[{excerpt:'合成证据',url:'https://www.sse.com.cn/fixture'},{excerpt:'不可用地址',url:'javascript:attack()'}]});await tick();reply(e.requests[start+2],me);await tick();}
async function run(){
 const e=setup();assert.equal(e.requests.length,0,'chat must not fetch knowledge');
 e.context.App._researchLibraryTab='knowledge';e.context.App.switchMode('reports');await initial(e);
 assert.equal(e.nodes.get('reportsPanelInner').hidden,true);assert.equal(e.nodes.get('researchKnowledgeTab').attributes['aria-pressed'],'true');
 e.nodes.get('knowledgeResults').children[0].children[0].listeners.click();await detail(e,3);
 assert.equal(e.nodes.get('knowledgeStatement').textContent,'合成研究结论');assert.equal(e.nodes.get('knowledgeSources').children[1].children.length,2,'unsafe sources never become links');
 e.nodes.get('knowledgeNote').value='尚未提交的复核';e.nodes.get('knowledgeReviewForm').listeners.input();
 e.context.App._setResearchLibraryTab('reports');await tick();e.context.App._setResearchLibraryTab('knowledge');reply(e.requests[6],me);await tick();
 assert.equal(e.nodes.get('knowledgeNote').value,'尚未提交的复核');assert(!e.nodes.get('knowledgeDetail').hidden);
 const save=e.nodes.get('knowledgeReviewForm').listeners.submit({preventDefault(){},currentTarget:e.nodes.get('knowledgeReviewForm')});reply(e.requests[7],me);await tick();
 assert.equal(e.requests[8].options.method,'PUT');assert.equal(e.requests[8].options.headers['X-Prism-Account'],'synthetic');assert.equal(JSON.parse(e.requests[8].options.body).expected_version,0);
 reply(e.requests[8],{},409);await save;assert.match(e.nodes.get('knowledgeReviewNotice').textContent,/结论已变化/);assert.equal(e.nodes.get('knowledgeNote').value,'尚未提交的复核');
 await e.nodes.get('knowledgeReviewForm').listeners.submit({preventDefault(){},currentTarget:e.nodes.get('knowledgeReviewForm')});assert.equal(e.requests.length,9);
 const switched=e.events.visibilitychange();reply(e.requests[9],{user:'another-account'});await switched;assert.equal(e.nodes.get('knowledgeResults').children.length,0);assert(e.nodes.get('knowledgeDetail').hidden);assert.equal(e.nodes.get('knowledgeNote').value,'');
 const linked=setup();const opening=linked.context.App._openKnowledgeClaim('claim-one');await initial(linked);await detail(linked,3,{...claim,effective_state:'reviewed'});await opening;assert.match(linked.nodes.get('knowledgeResults').children[0].children[1].textContent,/已复核/);assert.equal(linked.requests.length,6,'deep links share one initial query');assert.equal(linked.nodes.get('knowledgeReport').href,'/prism?run_id=run-one');
 console.log('research library runtime ok: lazy loading, shared context, drafts, ownership and deep links');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
