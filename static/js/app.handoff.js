/* Futures research uses the existing Native conversation, stop and report flow. */
(function () {
  'use strict';
  var names = {RB:'螺纹钢',CU:'铜',AU:'黄金',M:'豆粕',SC:'原油'};
  Object.assign(App, {
    _futuresStarting: false,
    _selectFuturesSymbol: function(symbol) {
      if (Object.prototype.hasOwnProperty.call(names, symbol)) document.getElementById('futureSymbol').value = symbol;
    },
    _syncFuturesModel: function() {
      var label = document.getElementById('futuresModelLabel');
      var selector = document.getElementById('modelSelect');
      var selected = selector && selector.selectedOptions && selector.selectedOptions[0];
      if (label) label.textContent = selected ? selected.textContent : this.model || '尚未配置的模型';
    },
    _initFutures: function() {
      this._syncFuturesModel();
      if (window.PrismHub) PrismHub.activateFutures();
    },
    _startFuturesResearch: async function() {
      if (this._futuresStarting || this._sendStarting) return false;
      var previous = typeof Streams !== 'undefined' && Streams[this.convId];
      if (previous && previous.active && (previous.offline || previous.saveUnconfirmed)) {
        this.switchMode('chat');
        this._toast('上次研究结果尚未确认，请先核对原对话或停止研究。');
        return false;
      }
      var symbol = document.getElementById('futureSymbol').value;
      if (!Object.prototype.hasOwnProperty.call(names, symbol)) return false;
      var input = document.getElementById('input');
      var initialDraft = input.value;
      if (input.value.trim() && !confirm('开始新的期货研究会替换尚未发送的对话内容，继续吗？')) return false;
      var button = document.getElementById('futuresStartResearch');
      var notice = document.getElementById('futuresNotice');
      var previousSkill = this.skill, created = false;
      this._futuresStarting = true; button.disabled = true; notice.hidden = true;
      try {
        var owner = this.user;
        if (!owner) throw new Error('账号尚未准备好，请刷新后重试。');
        var response = await this.api('/api/me', {cache:'no-store'});
        var me = response.ok ? await response.json() : null;
        if (!me || me.user !== owner) throw new Error('账号已变化或登录已失效，请刷新后重试。');
        if (!this._modelsLoaded) await this.loadModels();
        this._syncFuturesModel();
        if (!this.model) throw new Error('尚无可用模型，请先检查模型配置。');
        if (!this.skills.some(function(skill) { return skill.id === 'futures-research'; })) await this.loadSkills();
        if (this._workflowsUnavailable || !this.skills.some(function(skill) { return skill.id === 'futures-research'; })) throw new Error('期货研究方式暂不可用，请刷新后重试。');
        this.setSkill('futures-research');
        created = await this.newConv(true);
        if (!created || !this.convId) throw new Error('新对话未确认，请先核对会话列表。已有输入仍保留。');
        if (input.value !== initialDraft) throw new Error('对话输入已变化；新会话已创建，未开始研究，当前输入已保留。');
        input.value = '全面研究' + names[symbol] + '（' + symbol + '）：基本面、技术面与成交持仓，并给出有证据的判断、情景和风险。';
        input.dispatchEvent(new Event('input')); input.focus();
        if (this.renderSkillsBar) this.renderSkillsBar();
        await this.send();
        return true;
      } catch (error) {
        if (!created) this.setSkill(previousSkill);
        notice.textContent = error.message || '研究未能发起，请先核对会话记录后再操作。'; notice.hidden = false;
        if (this._toast) this._toast(notice.textContent);
        return false;
      } finally {
        this._futuresStarting = false; button.disabled = false;
      }
    }
  });
}());
