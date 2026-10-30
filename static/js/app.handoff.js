/* Fixed public product handoff. Never submits a model request automatically. */
(function () {
  'use strict';
  var names = {RB:'螺纹钢',CU:'铜',AU:'黄金',M:'豆粕',SC:'原油'};
  var symbol = new URLSearchParams(window.location.search).get('futures');
  if (!Object.prototype.hasOwnProperty.call(names, symbol)) return;
  var button = document.createElement('button');
  button.className = 'workbench-tools-btn';
  button.textContent = '准备' + names[symbol] + '期货研究问题';
  button.addEventListener('click', async function () {
    button.disabled = true;
    try {
      App.setSkill('futures-research');
      var created = await App.newConv(true);
      if (!created || !App.convId) throw new Error('conversation_unavailable');
      if (App.renderSkillsBar) App.renderSkillsBar();
      var input = document.getElementById('input');
      input.value = '请研究' + names[symbol] + '（' + symbol + '），先用 futures_snapshot 读取实际数据，再分别核查基本面、技术面及成交持仓。追溯官方来源和数据日期，列明缺口、不同情景和失效条件。按我的研究偏好分析，不把持仓量变化说成资金净流入。';
      input.dispatchEvent(new Event('input'));
      input.focus();
      button.remove();
      history.replaceState(null, '', '/');
    } catch (_) { button.disabled = false; button.textContent = '准备未完成，请检查会话后重试'; }
  });
  document.querySelector('.workbench-page-title').appendChild(button);
}());
