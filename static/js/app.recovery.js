/* Fixed recovery UI: the server is the authoritative write boundary. */
(function(){
  'use strict';
  const message='恢复模式，暂不支持新研究';
  const show=()=>{if(window.App && App._toast)App._toast(message);};
  if(!window.App)return;
  ['newConv','send','sendMessage','startAnalysis','_runAnalysis','_runVibePlan','_submitVibeFollowup','_cancelVibePlan','_deleteVibePlan','_startMarketScan','_startScanJob','_runScreener','_addToWatchlist','_removeFromWatchlist','_saveWatchlist'].forEach(name=>{if(typeof App[name]==='function')App[name]=show;});
  ['_initDiagnostics','_captureDiagnostic','_trackUiEvent','_loadIndicators','_updateTrashLink'].forEach(name=>{App[name]=()=>{};});
  const api=App.api;
  App.api=async function(url,options){
    const method=String(options&&options.method||'GET').toUpperCase();
    if(!['GET','HEAD'].includes(method)&&!(method==='POST'&&['/api/login','/api/logout'].includes(String(url).split('?')[0]))){
      show();
      return new Response(JSON.stringify({error:message,detail:message}),{status:503,headers:{'Content-Type':'application/json'}});
    }
    return api.call(this,url,options);
  };
  App._connectAnalysisStream=function(){this._analysisStreaming=false;show();};
  App._restoreAnalysisState=function(){
    if(this._analysisAttachAttempted)return;
    this._analysisAttachAttempted=true;
    try{const state=JSON.parse(sessionStorage.getItem('prism_analysis')||'null');if(state&&state.id&&this._openAnalysisRecord)return this._openAnalysisRecord(state.id,state.ticker);}catch(_error){}
  };
  App.showWelcome=function(){
    const welcome=document.getElementById('msgList');
    if(welcome)welcome.innerHTML='<div class="welcome"><h2>Prism 有限恢复模式</h2><p>从左侧打开本人已保存对话，或查看报告和行情。新研究与修改暂不可用。</p></div>';
  };
  App._loadWatchlist=async function(){
    this._watchlist=[];
    try{
      const response=await this.api('/api/watchlist');
      if(response.ok)this._watchlist=(await response.json()).items||[];
    }catch(_error){}
    if(this._renderWatchlist)this._renderWatchlist();
  };
  const selectors='.new-chat-btn, #sendBtn, #input, #modelSelect, #skillSelect';
  document.querySelectorAll(selectors).forEach(element=>{element.disabled=true;element.setAttribute('aria-disabled','true');element.title=message;element.classList.add('recovery-disabled');});
  // Do not let legacy init re-register a replacement worker on every reload.
  // Existing registrations are removed; versioned assets remain network-only.
  const worker=window.navigator && navigator.serviceWorker;
  if(worker){
    worker.register=async()=>({update(){}});
    worker.getRegistrations().then(registrations=>Promise.all(registrations.map(reg=>reg.unregister()))).catch(()=>{});
  }
  window.PRISM_RECOVERY_MODE='limited_read_only_recovery';
})();
