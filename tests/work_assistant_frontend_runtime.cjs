/* Shipped assistant UI against synthetic, isolated proposal responses. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),{randomUUID}=require('node:crypto');
const source=fs.readFileSync('static/js/work.assistant.js','utf8');
const template=fs.readFileSync('templates/work.html','utf8');
const ids=[...template.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
class Element{constructor(tag='div'){this.tagName=tag.toUpperCase();this.open=false;this.children=[];this.value='';this.textContent='';this.hidden=false;this.disabled=false;this.listeners={};this.style={};this.parentElement={style:{}};}append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this.children=nodes;this.textContent='';}addEventListener(name,fn){(this.listeners[name]||=[]).push(fn);}fire(name,event={}){for(const fn of this.listeners[name]||[])fn({preventDefault(){},...event});}click(){this.fire('click');}}
function text(node){return node.textContent+node.children.map(text).join('');}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function main(){
 let user='alice',mode='ready',requests=[],proposal;
 const nodes=new Map(ids.map(id=>[id,new Element()])),docListeners={};
 const document={hidden:false,getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),addEventListener:(name,fn)=>{docListeners[name]=fn;}};
 const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
 const fetch=async(path,options)=>{if(path==='/api/me')return response({user});requests.push({path,options});if(path.endsWith('/confirm')){if(mode==='unknown')throw new Error('offline');return response({proposal:{id:'p',state:'completed',digest:'digest',results:[{kind:'update_task',result:{}},{kind:'update_project',result:{}},{kind:'update_occurrence',result:{conflicts:[{title:'其他日程'}]}}]}});}if(path.endsWith('/undo'))return response({proposal:{id:'p',state:'undone'}});return response({proposal});};
 const context={document,window:null,fetch,crypto:{randomUUID},Intl,Date,Number,String,Array,Object,JSON,Promise,Error,AbortController,encodeURIComponent,setTimeout,clearTimeout};context.window=context;vm.createContext(context);vm.runInContext(source,context);await tick();
 const e=id=>nodes.get(id);e('assistantText').value='合成安排';
 const actions=[
  {kind:'update_task',operation_id:'private-op',target:{id:'secret-task',title:'整理材料'},before:{status:'todo',version:3},fields:{status:'done',expected_version:3}},
  {kind:'update_project',target:{id:'secret-project',title:'发布'},before:{name:'发布',status:'active',description:''},fields:{name:'发布',description:'',status:'archived',request_id:'secret-id',expected_version:1}},
  {kind:'update_occurrence',target:{id:'secret-occ',title:'周会',timezone:'Asia/Shanghai'},before:{start_at:'2026-11-04T02:00:00Z',end_at:'2026-11-04T03:00:00Z',cancelled:0},fields:{start_at:'2026-11-04T07:00:00Z',end_at:'2026-11-04T08:00:00Z',expected_series_version:2}},
  {kind:'update_event',target:{title:'例会'},before:{start_at:'2026-11-04T02:00:00Z',recurrence:{frequency:'weekly',interval:1,count:12},timezone:'Asia/Shanghai'},fields:{start_at:'2026-11-04T03:00:00Z'}},
  {kind:'plan_project_task',target:{project_name:'合成项目',title:'开发',id:'secret-task'},before:{duration_minutes:null,assignee:null,milestone:false,not_before:null},fields:{request_id:'secret-id',expected_version:0,duration_minutes:120,assignee:'editor'}},
  {kind:'set_project_dependencies',target:{project_name:'合成项目',title:'合成项目',dependency_changes:[{before_title:'设计',after_title:'开发',lag_minutes:30}]},before:{links:[]},fields:{request_id:'secret-id',expected_version:1,links:[{predecessor_id:'secret-before',successor_id:'secret-after'}]}},
  {kind:'set_project_calendar',target:{project_name:'合成项目',title:'合成项目'},before:{configured:false,config:{}},fields:{request_id:'secret-id',expected_version:1,start_at:'2026-11-04T02:00:00Z',timezone:'Asia/Shanghai',work_start:540,work_end:1080,weekdays:[0,1,2,3,4],holidays:['2026-11-09']}},
  {kind:'update_project_task',target:{project_name:'合成项目',title:'设计'},before:{status:'todo'},fields:{status:'doing',expected_version:1}},
  {kind:'create_project_approval',target:{project_name:'合成项目',title:'合成审批',approval_snapshot:{task:{id:'secret-task',title:'开发'},attachments:[{id:'secret-file',filename:'设计.pdf',sha256:'a'.repeat(64),version:2}]}},fields:{title:'合成审批',description:'合成说明',reviewer:'bob',task_id:'secret-task',attachment_ids:['secret-file']}}
 ];proposal={id:'p',state:'ready',digest:'digest',actions};
 e('assistantText').fire('compositionstart');e('assistantForm').fire('submit');await tick();assert.equal(requests.length,0);e('assistantText').fire('compositionend');e('assistantForm').fire('submit');await tick();await tick();
 let preview=text(e('assistantDetails'));for(const expected of ['修改任务：整理材料','状态：待开始 → 已完成','修改项目：发布','状态：进行中 → 已归档','只调整这一次日程：周会','修改整组重复日程：例会','GMT+8','默认估算 60 分钟 → 120 分钟','负责人：未设置 → editor','先后顺序：设计 → 开发，间隔 30 工作分钟','计划开始：未启用','额外休息日：2026-11-09','修改项目任务：合成项目 · 设计','发起审批：合成项目 · 合成审批','审批人：bob','关联任务：开发','附件：设计.pdf · 指纹 aaaaaaaaaaaa','内容变化后需重新发起'])assert(preview.includes(expected),expected+' '+preview);assert(!/secret|private-op|expected_version/.test(preview));assert.equal(e('assistantConfirm').hidden,false);
 e('assistantConfirm').click();await tick();await tick();let results=text(e('assistantDetails'));for(const label of ['查看任务','查看项目','查看日历','1 条日程时间重叠'])assert(results.includes(label),results);assert.equal(e('assistantUndo').hidden,false);e('assistantUndo').click();await tick();await tick();assert.equal(e('assistantPreviewTitle').textContent,'已撤销');
 proposal={id:'read',state:'completed',digest:'digest',can_undo:false,results:[{kind:'read_project',result:{project:{name:'合成项目',status:'active'},tasks:[{title:'开发',status:'todo'}],tasks_truncated:true,attachments:[{filename:'设计.pdf'}],approvals:[{title:'审批方案',status:'pending'}],approvals_truncated:false}}]};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantPreviewTitle').textContent,'项目概览');assert.equal(e('assistantUndo').hidden,true);assert.equal(e('assistantConfirm').hidden,true);for(const expected of ['合成项目','开发','设计.pdf','审批方案','还有未显示的记录'])assert(text(e('assistantDetails')).includes(expected));
 function sections(){return e('assistantDetails').children[0].children.filter(node=>node.tagName==='DETAILS');}
 assert.deepEqual(sections().map(node=>node.open),[false,false,false]);
 for(const [key,index] of [['tasks',0],['attachments',1],['approvals',2]]){proposal.results[0].result.section=key;e('assistantForm').fire('submit');await tick();await tick();assert.deepEqual(sections().map(node=>node.open),[0,1,2].map(i=>i===index));}
 for(const [key,index,label] of [['tasks',0,'暂无任务。'],['attachments',1,'暂无附件。'],['approvals',2,'暂无审批。']]){proposal.results[0].result[key]=[];proposal.results[0].result.section=key;e('assistantForm').fire('submit');await tick();await tick();assert.equal(sections()[index].open,true);assert(text(sections()[index]).includes(label));assert.equal(e('assistantConfirm').hidden,true);assert.equal(e('assistantUndo').hidden,true);}
 proposal={id:'done',state:'completed',digest:'digest',can_undo:false,results:[{kind:'create_project_approval',result:{}}]};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantUndo').hidden,true);assert(text(e('assistantDetails')).includes('查看项目'));
 proposal={id:'approval',state:'ready',digest:'digest',actions:[{kind:'create_project_approval',target:{project_name:'合成项目',approval_snapshot:{task:null,attachments:[]}},fields:{title:'审批',reviewer:'alice',task_id:null,attachment_ids:[]}}]};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantConfirm').hidden,true);
 proposal={id:'p',state:'ready',digest:'digest',actions:[{kind:'update_task',target:{title:'任务'},before:{status:'todo'},fields:{unknown_field:'value'}}]};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantConfirm').hidden,true);assert.match(e('assistantNotice').textContent,/预览信息不完整/);
 proposal={id:'p',state:'ready',digest:'digest',actions:[actions[0]]};e('assistantForm').fire('submit');await tick();await tick();mode='unknown';e('assistantConfirm').click();await tick();await tick();assert.match(e('assistantNotice').textContent,/操作结果未知/);let count=requests.filter(r=>r.path.endsWith('/confirm')).length;e('assistantConfirm').click();await tick();assert.equal(requests.filter(r=>r.path.endsWith('/confirm')).length,count);assert.equal(e('assistantCheck').hidden,false);
 mode='ready';e('assistantCheck').click();await tick();await tick();proposal={id:'p2',state:'clarification',clarification:'找到多个同名任务，请区分名称。',can_use_model:false};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantModel').hidden,true);assert.equal(e('assistantModelHelp').hidden,true);assert.match(text(e('assistantDetails')),/多个同名/);proposal={id:'p3',state:'clarification',clarification:'已经是要求的状态。',can_use_model:false};e('assistantForm').fire('submit');await tick();await tick();assert.equal(e('assistantModel').hidden,true);assert.match(text(e('assistantDetails')),/已经是要求的状态/);
 for(const [reason,message] of [['billing','计费'],['access','访问'],['rate_limit','频率']]){
  proposal={id:'p4',state:'model_rejected',reason,actions:[],digest:null};
  const before=requests.length;
  e('assistantForm').fire('submit');await tick();await tick();
  assert.equal(requests.length,before+1);
  assert.match(e('assistantPreviewTitle').textContent,/未生成草稿/);
  assert.ok(text(e('assistantDetails')).includes(message));
  for(const id of ['assistantCheck','assistantModel','assistantConfirm','assistantUndo'])assert.equal(e(id).hidden,true);
  assert.equal(e('assistantText').value,'合成安排');
 }
 user='bob';document.hidden=false;await docListeners.visibilitychange();assert.equal(e('assistantText').value,'');assert.equal(e('assistantPreview').hidden,true);
 console.log('work assistant frontend ok: update preview, results, IME, unknown and account');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
