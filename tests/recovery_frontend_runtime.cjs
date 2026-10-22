'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
(async()=>{
  let writes=0,registrations=0,unregistered=0,requests=[];
  const button={classList:{add(){}},setAttribute(){}};
  const app={newConv(){writes++;},send(){writes++;},startAnalysis(){writes++;},_runAnalysis(){writes++;},_startMarketScan(){writes++;},_submitVibeFollowup(){writes++;},_runVibePlan(){writes++;},_addToWatchlist(){writes++;},_loadWatchlist(){throw new Error('legacy unowned cache');},_renderWatchlist(){},_toast(){},_openAnalysisRecord(id){this.restored=id;},api:async url=>{requests.push(url);return{ok:true,json:async()=>({items:[{ticker:'600000',name:'fixture'}]})};}};
  const navigator={serviceWorker:{register(){registrations++;},getRegistrations:async()=>[{unregister:async()=>{unregistered++;}}]}};
  const context={App:app,navigator,Response,sessionStorage:{getItem:()=>JSON.stringify({id:'owned-record',streaming:true})},document:{querySelectorAll:()=>[button],getElementById:()=>null}};context.window=context;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../static/js/app.recovery.js'),'utf8'),context);
  app.newConv();app.send();app.startAnalysis();app._runAnalysis();app._startMarketScan();app._submitVibeFollowup();app._runVibePlan();app._addToWatchlist();
  assert.equal(writes,0);assert.equal(button.disabled,true);
  await app._loadWatchlist();assert.deepEqual(requests,['/api/watchlist']);assert.equal(app._watchlist[0].ticker,'600000');
  assert.equal((await app.api('/api/unknown',{method:'POST'})).status,503);assert.deepEqual(requests,['/api/watchlist']);
  app._analysisStreaming=true;app._connectAnalysisStream();assert.equal(app._analysisStreaming,false);
  app._restoreAnalysisState();assert.equal(app.restored,'owned-record');
  await navigator.serviceWorker.register('/static/sw.js');await Promise.resolve();
  assert.equal(registrations,0);assert.equal(unregistered,1);assert.equal(context.PRISM_RECOVERY_MODE,'limited_read_only_recovery');
  console.log('recovery frontend ok');
})();
