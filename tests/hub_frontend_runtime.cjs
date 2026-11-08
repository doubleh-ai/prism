/* Shipped form handlers against finite HTTP replies; no user content or network. */
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const tick=()=>new Promise(setImmediate);
const script=fs.readFileSync('static/js/app.hub.js','utf8');
function el(){return {value:'',checked:false,disabled:false,hidden:false,textContent:'',style:{},listeners:{},children:[],
  append(...items){this.children.push(...items);},addEventListener(k,f){this.listeners[k]=f;},replaceChildren(...items){this.children=items;},setAttribute(){}};}
function setup(page='preferences',embedded=false){
 const fields=Object.fromEntries(['style','horizon','max_position_pct','max_drawdown_pct','allow_leverage'].map(k=>[k,el()]));
 Object.assign(fields.style,{value:'conservative'});fields.horizon.value='long';fields.max_position_pct.value='20';fields.max_drawdown_pct.value='10';
 const submit=el(),form=el();form.elements={namedItem:k=>fields[k]};form.querySelector=()=>submit;
 const els={main:el(),notice:el(),profileForm:form,profileReload:el(),profileVersion:el()},requests=[],listeners={};
 Object.assign(els,{portfolioForm:el(),addPosition:el(),useObservedPriceTime:el(),portfolioReload:el(),portfolioDetails:el()});
 Object.assign(els,{futuresForm:form,futureSymbol:el(),futuresResult:el()});
 if(embedded){
  els.futuresWorkspace=el();els.futuresNotice=el();els.futuresWorkspace.querySelectorAll=()=>[els.futuresForm];
  if(page==='preferences'){
   els.preferencesWorkspace=el();els.preferencesNotice=el();
   els.preferencesWorkspace.querySelectorAll=()=>[form,els.portfolioForm];
   els.futuresForm=el();
  }
 }
 const context={console,URL,URLSearchParams,Intl,Date,JSON,Number,String,Promise,AbortController,Object,Error,
  setTimeout,clearTimeout,location:{pathname:'/'+page,search:'',replace(){}},confirm:()=>true,
  addEventListener(k,f){listeners[k]=f;},
  document:{body:{dataset:embedded?{}:{page}},getElementById:k=>els[k],createElement:el,
   querySelectorAll:s=>s==='form'?[form]:[],addEventListener(){}},
  fetch(path,options){return new Promise((resolve,reject)=>requests.push({path,options,resolve,reject}));}};
 Object.defineProperty(context,'localStorage',{get(){throw Error('private persistent cache forbidden');}});
 context.window=context;vm.createContext(context);vm.runInContext(script,context);return {els,requests,submit,form,fields,listeners,context};
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
 const draft={value:'existing private draft'},button=el(),notice=el(),symbol={value:'RB'};
 const app={user:'synthetic-A',model:'synthetic',_modelsLoaded:true,skill:'prism-research',skills:[{id:'futures-research'}],
  api:async()=>({ok:true,json:async()=>({user:'synthetic-A'})}),setSkill(value){this.skill=value;},newConv:async()=>false,_toast(){},send(){assert.fail('failed creation cannot start a model');}};
 const context={App:app,document:{getElementById:id=>({input:draft,futuresStartResearch:button,futuresNotice:notice,futureSymbol:symbol})[id]},confirm:()=>true};context.window=context;
 vm.createContext(context);vm.runInContext(fs.readFileSync('static/js/app.handoff.js','utf8'),context);
 assert.equal(await app._startFuturesResearch(),false);assert.equal(draft.value,'existing private draft');assert(!button.disabled);assert.equal(app.skill,'prism-research');
}
async function embeddedFuturesStayLazyAndDoNotRebindOtherForms(){
 const e=setup('futures',true);assert.equal(e.requests.length,0);assert.equal(e.form.onsubmit,undefined);
 const active=e.context.PrismHub.activateFutures();assert.equal(e.requests.length,1);respond(e.requests[0],{user:'synthetic-A'});await active;
 assert.equal(e.form.onsubmit,e.context.PrismHub.loadFutures);assert.equal(e.els.futuresWorkspace.style.visibility,'');
}
async function preferencesKeepDraftWhileUsingFutures(){
 const e=setup('preferences',true),prefs=e.context.PrismPreferences;
 assert.equal(e.requests.length,0,'opening Prism must not read holdings or preferences');
 assert.equal(e.form.onsubmit,undefined);
 const opening=prefs.activate();respond(e.requests[0],{user:'synthetic-A'});await tick();
 respond(e.requests[1],{profile:{configured:true,version:9,style:'balanced',horizon:'long',max_position_pct:20,max_drawdown_pct:10}});await opening;
 const save=e.form.onsubmit;e.fields.style.value='aggressive';e.form.listeners.input();
 prefs.suspend();const futures=e.context.PrismHub.activateFutures();respond(e.requests[2],{user:'synthetic-A'});await futures;
 assert.equal(e.form.onsubmit,save,'futures must not rebind the preferences form');
 assert.equal(e.els.futuresForm.onsubmit,e.context.PrismHub.loadFutures);
 e.context.PrismHub.suspendFutures();const returning=prefs.activate();respond(e.requests[3],{user:'synthetic-A'});await returning;
 assert.equal(e.fields.style.value,'aggressive');assert.match(e.els.profileVersion.textContent,/版本 9/);
 assert.equal(e.requests.filter(r=>r.path==='/api/preferences/research').length,1,'returning must not overwrite the draft');
 assert(!e.requests.some(r=>r.path.includes('/portfolio')||r.options.method==='PUT'));
 let warned=false;e.listeners.beforeunload({preventDefault(){warned=true;}});assert(warned);
}
async function preferencesRejectChangedAccountOnReturn(){
 const e=setup('preferences',true),prefs=e.context.PrismPreferences;
 let opening=prefs.activate();respond(e.requests[0],{user:'synthetic-A'});await tick();respond(e.requests[1],{profile:{configured:false,version:0}});await opening;
 e.fields.style.value='aggressive';prefs.suspend();opening=prefs.activate();respond(e.requests[2],{user:'synthetic-B'});await opening;
 assert.equal(e.els.preferencesWorkspace.children.length,1,'remove the old account form from the workspace');
 assert.match(JSON.stringify(e.els.preferencesWorkspace.children),/账号已经切换/);
 const saving=e.form.onsubmit({preventDefault(){},currentTarget:e.form});respond(e.requests[3],{user:'synthetic-B'});await saving;
 assert(!e.requests.some(r=>r.options.method==='PUT'),'a detached old form cannot save to the new account');
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
async function showExchangeFuturesData(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='CU';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'CU',name:'铜',latest:{},technical:{},positioning:{},fundamentals:[],gaps:[],source:{},
  exchange_daily:{status:'collected',data_as_of:'2026-10-30',source_url:'https://example.com/daily',active_contract:'cu2612',contracts:[{contract:'cu2612',delivery_month:'2026-12',close:70000,settlement:69900,volume:100,open_interest:80}],curve:{status:'collected',near_contract:'cu2612',far_contract:'cu2701',near_settlement:69900,far_settlement:70100,far_minus_near:200,shape:'contango'}},
  member_positioning:{status:'collected',data_as_of:'2026-10-30',source_url:'https://example.com/members',contract:'cu2612',sides:{long:{ranked_members:20,quantity_sum:123,change_sum:-3,members:[{rank:1,member:'会员甲',quantity:12,change:-1}]}},notice:'多空排名差不代表净资金流。'}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 for(const text of ['远月升水','远月减近月：200','会员甲','多空排名差不代表净资金流','https://example.com/daily','https://example.com/members','空头持仓：未提供'])assert.ok(rendered.includes(text),text);
}
async function showMissingFuturesData(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='CU';
 const run=e.form.onsubmit({preventDefault(){}});respond(e.requests[1],{symbol:'CU',name:'铜',latest:{},technical:{},positioning:{},fundamentals:[],gaps:[],source:{},exchange_daily:{status:'unavailable'},member_positioning:{status:'not_reported'}});await run;
 const rendered=JSON.stringify(e.els.futuresResult);assert.match(rendered,/未取得交易所合约数据/);assert.match(rendered,/交易所未披露该合约的会员持仓/);
}
async function showRetainedOfficialDataWithoutContinuousPrices(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='CU';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'CU',name:'铜',series_status:'unavailable',data_as_of:null,collected_at:'2026-11-03T12:00:00+08:00',latest:{close:null},technical:null,positioning:null,fundamentals:[],gaps:['现货数据尚未取得'],source:{},warehouse_receipt:{status:'collected',data_as_of:'2026-11-02',table:'铜',row:'总计',quantity:7,change:-1,unit:'吨',source_url:'https://www.shfe.com.cn/source'},exchange_daily:{status:'unavailable'}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 assert.match(e.els.notice.textContent,/连续日线暂未取得/);assert.match(rendered,/总计 7 吨/);
 assert(!rendered.includes('最近收盘')&&!rendered.includes('20日均值')&&!rendered.includes('null')&&!rendered.includes('undefined'));
 assert(!e.submit.disabled);
}
async function showDifferentSourceDatesInOneNotice(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='CU';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'CU',name:'铜',series_status:'collected',data_as_of:'2026-11-02',collected_at:'2026-11-03T12:00:00+08:00',latest:{close:70000},technical:{},positioning:{},fundamentals:[],gaps:[],source:{name:'合成公开行情',url:'https://finance.sina.com.cn/fixture'},date_alignment:'mixed',warehouse_receipt:{status:'collected',data_as_of:'2026-10-30',table:'铜',row:'总计',quantity:7,change:-1,unit:'吨',source_url:'https://www.shfe.com.cn/source'}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 assert.match(e.els.notice.textContent,/资料日期不同/);assert(rendered.includes('2026-11-02')&&rendered.includes('2026-10-30')&&rendered.includes('70000'));
}
async function showDatedParametersWithinExistingDetails(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='RB';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'RB',name:'螺纹钢',series_status:'unavailable',collected_at:'2026-11-03T14:00:00+08:00',latest:{},source:{},fundamentals:[],gaps:[],
  contract_dates:{status:'collected',data_as_of:'2026-11-02',reference_url:'https://www.shfe.com.cn/reports/businessdata/prmsummary/',notice:'未含法定节假日调整',contracts:[{contract:'rb2701',listed_on:'2026-01-16',listed_expiry_date:'2027-01-15',delivery_start:'2027-01-18',delivery_end:'2027-01-19'}]},
  exchange_parameters:{status:'collected',data_as_of:'2026-11-03',reference_url:'javascript:alert(1)',notice:'非经纪商保证金',contracts:[{contract:'rb2701',spec_long_margin_pct:7,spec_short_margin_pct:null,hedge_long_margin_pct:6,hedge_short_margin_pct:6.5,upper_limit_pct:5,lower_limit_pct:4,additional_limit_notice_required:true}]}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 for(const s of ['合约日期与交易所参数','2026-11-02','2026-11-03','2027-01-15','一般买保证金（%）','套保卖保证金（%）','未提供','须查原公告','未含法定节假日调整','非经纪商保证金'])assert(rendered.includes(s),s);
 assert(!rendered.includes('javascript:')&&!rendered.includes('设置参数')&&!rendered.includes('undefined'));
 assert.equal(e.requests.length,2);assert(!e.submit.disabled);
}
async function showDceAccessRestrictionWithoutDiscardingAvailablePrices(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='M';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'M',name:'豆粕',series_status:'collected',data_as_of:'2026-11-06',collected_at:'2026-11-07T10:00:00+08:00',latest:{close:3000},technical:{},positioning:{},fundamentals:[],gaps:[],source:{},exchange_daily:{status:'unavailable',error_code:'automated_access_restricted',member_positioning:{status:'not_covered'}},warehouse_receipt:{status:'unavailable',error_code:'automated_access_restricted'}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 for(const text of ['最近收盘','3000','交易所限制自动读取，合约资料暂未取得','交易所限制自动读取，仓单资料暂未取得'])assert(rendered.includes(text),text);
 assert.equal(e.requests.length,2);assert(!e.submit.disabled);
}
async function showDceQueryDateAndKeepUnverifiedSourceExplicit(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();e.els.futureSymbol.value='M';
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'M',name:'豆粕',series_status:'unavailable',latest:{},fundamentals:[],gaps:[],source:{},exchange_daily:{status:'collected',data_as_of:'2026-11-06',date_provenance:'request_parameter',transport_authenticated:false,active_contract:'m2701',contracts:[],curve:{status:'insufficient'},notice:'日期来自查询参数，原始内容尚未独立核验。',source_url:'http://www.dce.com.cn/dcereport/publicweb/dailystat/dayQuotes',member_positioning:{status:'not_covered'}},warehouse_receipt:{status:'collected',data_as_of:'2026-11-06',date_provenance:'request_parameter',table:'豆粕',row:'小计',quantity:30,change:2,unit:'手',source_url:'javascript:alert(1)',notice:'仓单仅为手数，未换算吨数。'}});
 await run;const rendered=JSON.stringify(e.els.futuresResult);
 for(const text of ['查询日期 2026-11-06','公开来源未独立核验','日期来自查询参数','仓单仅为手数'])assert(rendered.includes(text),text);
 assert(!rendered.includes('javascript:')&&!rendered.includes('交易所日期 2026-11-06'));
}
async function showCollectedMonthlySupplyAlongsideUncollectedFundamentalDrivers(){
 const e=setup('futures');respond(e.requests[0],{user:'synthetic-A'});await tick();
 const run=e.form.onsubmit({preventDefault(){}});
 respond(e.requests[1],{symbol:'RB',name:'螺纹钢',series_status:'unavailable',latest:{},source:{},gaps:['社会库存和现货基差仍缺少原始数据'],fundamentals:[
  {topic:'全国钢铁生产供给背景',status:'collected',history_window_complete:false,notice:'不是螺纹钢专属产量、开工率或社会库存',observations:[
   {period_label:'2026年9月',published_date:'2026-10-19',source_url:'https://www.stats.gov.cn/sj/zxfb/202610/t20261019_1965617.html',products:[{label:'粗钢',unit:'万吨',period_amount:7359,period_yoy_pct:0.1,year_to_date_amount:72477,year_to_date_yoy_pct:-2.9}]}
  ]},
  {topic:'社会与钢厂库存',status:'not_collected',official_url:'https://www.shfe.cn/'}
 ]});await run;
 const rendered=JSON.stringify(e.els.futuresResult);
 for(const text of ['全国钢铁生产供给背景','2026年9月','发布 2026-10-19','7359 万吨','官方同比 0.1%','年初至该期 72477 万吨','社会与钢厂库存：尚未采集','不是螺纹钢专属产量','本次历史窗口未收集完整','社会库存和现货基差仍缺少原始数据'])assert(rendered.includes(text),text);
 assert(!rendered.includes('全国钢铁生产供给背景：尚未采集'));assert.equal(e.requests.length,2);
}
(async()=>{await initialFailureBlocksNativeSubmit();await accountSwitchBlocksWrite();await failedHandoffPreservesExistingDraft();await embeddedFuturesStayLazyAndDoNotRebindOtherForms();await preferencesKeepDraftWhileUsingFutures();await preferencesRejectChangedAccountOnReturn();await showOfficialWarrants();await showExchangeFuturesData();await showMissingFuturesData();await showRetainedOfficialDataWithoutContinuousPrices();await showDifferentSourceDatesInOneNotice();await showDatedParametersWithinExistingDetails();await showDceAccessRestrictionWithoutDiscardingAvailablePrices();await showDceQueryDateAndKeepUnverifiedSourceExplicit();await showCollectedMonthlySupplyAlongsideUncollectedFundamentalDrivers();console.log('hub journeys ok: 15 scenarios');})().catch(e=>{console.error(e);process.exitCode=1;});
