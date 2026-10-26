/* Feedback text is data. It never grants execution or release authority. */
Object.assign(App, {
  _feedbackBusy: false,
  _feedbackReadSeq: 0,
  _feedbackStatusLabels: {
    triaging:'排查中', pending_review:'待评审', accepted:'已采纳', in_progress:'处理中',
    needs_info:'待补充', fixed:'已完成', deferred:'暂缓', declined:'未采纳'
  },

  _feedbackProblemDetected: function() {
    var hint = document.getElementById('feedbackErrorHint');
    if (hint) hint.hidden = false;
  },

  _openFeedback: function(category) {
    var dialog = document.getElementById('feedbackDialog');
    document.getElementById('feedbackReviewEntry').hidden = !this.isAdmin;
    if (category === 'bug' || category === 'suggestion') document.getElementById('feedbackCategory').value = category;
    document.getElementById('feedbackPage').textContent = '当前页面：' + (this._mode || 'chat');
    if (!dialog.open) dialog.showModal();
    this._feedbackView('new');
    document.getElementById('feedbackSummary').focus();
  },

  _feedbackView: function(view) {
    this._feedbackAdmin = view === 'admin' && !!this.isAdmin;
    this._feedbackReadSeq++;
    document.getElementById('feedbackForm').hidden = view !== 'new';
    document.getElementById('feedbackContent').hidden = view === 'new';
    document.getElementById('feedbackMessage').textContent = '';
    if (view !== 'new') this._loadFeedback();
  },

  _feedbackDate: function(value) {
    var date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false});
  },

  _feedbackPath: function(id) {
    return '/api/' + (this._feedbackAdmin && this.isAdmin ? 'admin/' : '') + 'feedback' + (id ? '/' + encodeURIComponent(id) : '');
  },

  _feedbackQuery: function() {
    return '?limit=50' + (this._feedbackAdmin && this._feedbackCategoryFilter ? '&category=' + encodeURIComponent(this._feedbackCategoryFilter) : '');
  },

  _feedbackPayload: function() {
    return {
      category:document.getElementById('feedbackCategory').value,
      title:document.getElementById('feedbackSummary').value.trim(),
      description:document.getElementById('feedbackDescription').value.trim(),
      page:location.pathname,
      feature:this._mode || 'chat'
    };
  },

  _feedbackUUID: function() {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    var bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.from(bytes, function(value) { return value.toString(16).padStart(2, '0'); }).join('');
    return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20);
  },

  _feedbackNotice: function(seq, text) {
    if (seq === this._feedbackReadSeq) document.getElementById('feedbackMessage').textContent = text;
    else if (this._toast) this._toast(text);
  },

  _submitFeedback: async function(event) {
    if (event) event.preventDefault();
    if (this._feedbackBusy) return;
    var payload = this._feedbackPayload(), seq = this._feedbackReadSeq;
    if (!payload.title || !payload.description) return;
    var signature = JSON.stringify(payload);
    if (!this._feedbackDraft || this._feedbackDraft.signature !== signature) {
      this._feedbackDraft = {signature:signature,id:this._feedbackUUID()};
    }
    payload.request_id = this._feedbackDraft.id;
    this._feedbackBusy = true;
    var button = document.getElementById('feedbackSubmit'), message = document.getElementById('feedbackMessage');
    button.disabled = true; message.textContent = '正在提交…';
    try {
      var response = await this.api('/api/feedback', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if (!response.ok) {
        this._feedbackNotice(seq, response.status === 429 ? '提交较频繁，请稍后再试。' : '提交未成功，请检查内容或稍后重试。');
        return;
      }
      var data = await response.json();
      var sameDraft = this._feedbackDraft && this._feedbackDraft.id === payload.request_id && signature === JSON.stringify(this._feedbackPayload());
      if (sameDraft) {
        document.getElementById('feedbackSummary').value = '';
        document.getElementById('feedbackDescription').value = '';
        this._feedbackDraft = null;
      }
      this._feedbackNotice(seq, '已收到反馈 #' + data.feedback.id + '，可在“我的反馈与进度”查看后续答复。');
      if (sameDraft && seq === this._feedbackReadSeq && !document.getElementById('feedbackForm').hidden) {
        this._feedbackAdmin = false;
        document.getElementById('feedbackForm').hidden = true;
        document.getElementById('feedbackContent').hidden = false;
        this._renderFeedbackDetail(data.feedback);
      }
      document.getElementById('feedbackErrorHint').hidden = true;
    } catch (error) {
      // Keep the same request id: a lost response must not create another ticket.
      this._feedbackNotice(seq, '提交结果尚未确认，请先查看“我的反馈与进度”。原内容已保留，重试同一内容会核对原提交。');
    } finally {
      this._feedbackBusy = false; button.disabled = false;
    }
  },

  _loadFeedback: async function() {
    var seq = ++this._feedbackReadSeq, admin = !!this._feedbackAdmin;
    var box = document.getElementById('feedbackContent');
    box.innerHTML = '<p>正在读取反馈进度…</p>';
    try {
      var response = await this.api(this._feedbackPath() + this._feedbackQuery());
      if (seq !== this._feedbackReadSeq) return;
      if (!response.ok) { box.textContent = '反馈暂时无法读取，请稍后重新打开。'; return; }
      var data = await response.json();
      if (seq !== this._feedbackReadSeq) return;
      this._feedbackItems = data.items || [];
      this._feedbackNext = data.next_before_id;
      this._renderFeedbackList(admin);
    } catch (error) {
      if (seq === this._feedbackReadSeq) box.textContent = '反馈暂时无法读取，已提交的记录不会因此丢失。';
    }
  },

  _renderFeedbackList: function(admin) {
    var items = this._feedbackItems || [], self = this;
    var category = admin ? this._feedbackCategoryFilter || '' : '';
    var shown = category ? items.filter(function(item) { return item.category === category; }) : items;
    var heading = admin ? '<div class="feedback-queue-head"><p>异常优先排查；建议按北京时间每日评审。所有决定和验证结论均保留记录。</p><label>筛选 <select id="feedbackQueueFilter" onchange="App._feedbackCategoryFilter=this.value;App._loadFeedback()"><option value="">全部反馈</option><option value="bug" ' + (category === 'bug' ? 'selected' : '') + '>功能异常</option><option value="suggestion" ' + (category === 'suggestion' ? 'selected' : '') + '>改进建议</option></select></label></div>' : '<p class="feedback-privacy">以下记录仅你、管理员及获授权的自动处理流程可见。采纳表示进入计划，完成需有验证说明。</p>';
    document.getElementById('feedbackContent').innerHTML = heading + (shown.length ? '<div class="feedback-list">' + shown.map(function(item) {
      return '<button type="button" onclick="App._openFeedbackItem(' + Number(item.id) + ')"><div><span class="feedback-status">' + esc(self._feedbackStatusLabels[item.status] || item.status) + '</span><small>#' + Number(item.id) + ' · ' + esc(item.category === 'bug' ? '功能异常' : '改进建议') + (admin ? ' · ' + esc(item.username || '') : '') + '</small></div><strong>' + esc(item.title) + '</strong><small>更新于 ' + esc(self._feedbackDate(item.updated_at)) + '</small>' + (item.public_reply ? '<p>' + esc(item.public_reply) + '</p>' : '') + '</button>';
    }).join('') + '</div>' : '<p class="feedback-empty">暂无' + (category ? '此类' : '') + '反馈。</p>') + (this._feedbackNext ? '<button type="button" class="feedback-more" onclick="App._feedbackLoadMore()">加载更多记录</button>' : '');
  },

  _feedbackLoadMore: async function() {
    if (!this._feedbackNext) return;
    var seq = ++this._feedbackReadSeq;
    try {
      var response = await this.api(this._feedbackPath() + this._feedbackQuery() + '&before_id=' + Number(this._feedbackNext));
      if (!response.ok || seq !== this._feedbackReadSeq) return;
      var data = await response.json();
      if (seq !== this._feedbackReadSeq) return;
      this._feedbackItems = (this._feedbackItems || []).concat(data.items || []);
      this._feedbackNext = data.next_before_id;
      this._renderFeedbackList(!!this._feedbackAdmin);
    } catch (error) {
      if (seq === this._feedbackReadSeq) document.getElementById('feedbackMessage').textContent = '更多记录暂时无法读取，请稍后再试。';
    }
  },

  _openFeedbackItem: async function(id) {
    var seq = ++this._feedbackReadSeq;
    try {
      var response = await this.api(this._feedbackPath(id));
      if (seq !== this._feedbackReadSeq) return;
      if (!response.ok) { document.getElementById('feedbackMessage').textContent = '此反馈暂时无法读取。'; return; }
      var data = await response.json();
      if (seq === this._feedbackReadSeq) this._renderFeedbackDetail(data.feedback);
    } catch (error) {
      if (seq === this._feedbackReadSeq) document.getElementById('feedbackMessage').textContent = '反馈详情暂时无法读取，请稍后再试。';
    }
  },

  _renderFeedbackDetail: function(item) {
    this._feedbackSelected = item;
    var self = this, release = item.release || {}, triage = item.triage || {};
    var triageText = triage.mode === 'daily_review' ? '已进入每日建议评审队列，评审后说明是否采纳。' : '已进入异常排查队列；正在核对版本与复现条件，尚未验证修复。';
    if (triage.priority === 'urgent') triageText = '优先排查。' + triageText;
    if (item.status !== 'triaging' && item.status !== 'pending_review') triageText = '最初分诊：' + (item.category === 'bug' ? '异常进入排查队列。' : '建议进入每日评审队列。') + '后续决定与验证见处理记录。';
    var updates = (item.updates || []).map(function(update) {
      return '<li><strong>' + esc(self._feedbackStatusLabels[update.status] || update.status) + '</strong><small>' + esc(self._feedbackDate(update.created_at)) + '</small><p>' + esc(update.public_reply || '') + '</p>' + (update.verification ? '<p>验证：' + esc(update.verification) + ' · ' + esc(update.target_version || '') + '</p>' : '') + '</li>';
    }).join('');
    var adminForm = '';
    if (this._feedbackAdmin && this.isAdmin) {
      var states = item.category === 'bug' ? ['triaging','in_progress','needs_info','fixed','deferred','declined'] : ['pending_review','accepted','in_progress','needs_info','fixed','deferred','declined'];
      adminForm = '<form class="feedback-form feedback-review-form" onsubmit="App._updateFeedback(event)"><h3>处理与评审</h3><label for="feedbackDecision">处理状态</label><select id="feedbackDecision">' + states.map(function(status) { return '<option value="' + status + '" ' + (status === item.status ? 'selected' : '') + '>' + esc(self._feedbackStatusLabels[status]) + '</option>'; }).join('') + '</select><label for="feedbackReply">答复或决定理由（提交者可见）</label><textarea id="feedbackReply" required maxlength="3000" rows="3"></textarea><label for="feedbackTarget">目标版本（完成时必填）</label><input id="feedbackTarget" maxlength="64"><label for="feedbackVerification">验证说明（完成时必填）</label><textarea id="feedbackVerification" maxlength="3000" rows="3" placeholder="复现、测试及上线核验的结果；无法核验时保持处理中。"></textarea><button type="submit" id="feedbackUpdateSubmit">保存处理记录</button></form>';
    }
    document.getElementById('feedbackContent').innerHTML = '<button type="button" class="feedback-back" onclick="App._loadFeedback()">← 返回反馈列表</button><article class="feedback-detail"><div class="feedback-status">' + esc(this._feedbackStatusLabels[item.status] || item.status) + ' · #' + Number(item.id) + '</div><h3>' + esc(item.title) + '</h3><p class="feedback-description">' + esc(item.description) + '</p><p class="feedback-privacy">页面 ' + esc(item.feature || '') + ' · ' + esc(item.page || '/') + ' · ' + esc(release.version || '') + ' · ' + esc(String(release.revision || '').slice(0,12)) + '</p><p class="feedback-privacy">' + esc(triageText) + '</p>' + (item.public_reply ? '<blockquote>' + esc(item.public_reply) + '</blockquote>' : '<p>已进入处理队列，后续答复会在此保留。</p>') + (item.verification ? '<p>验证：' + esc(item.verification) + ' · ' + esc(item.target_version || '') + '</p>' : '') + (updates ? '<h4>处理记录</h4><ol class="feedback-timeline">' + updates + '</ol>' : '') + '</article>' + adminForm;
  },

  _updateFeedback: async function(event) {
    if (event) event.preventDefault();
    if (!this.isAdmin || !this._feedbackAdmin || this._feedbackBusy || !this._feedbackSelected) return;
    var payload = {
      status:document.getElementById('feedbackDecision').value,
      public_reply:document.getElementById('feedbackReply').value.trim(),
      target_version:document.getElementById('feedbackTarget').value.trim(),
      verification:document.getElementById('feedbackVerification').value.trim(),
      expected_revision:this._feedbackSelected.revision_number
    };
    var message = document.getElementById('feedbackMessage');
    if (!payload.public_reply) { message.textContent = '请填写答复或决定理由。'; return; }
    if (payload.status === 'fixed' && (!payload.verification || !payload.target_version)) {
      message.textContent = '完成需填写验证说明和目标版本。'; return;
    }
    this._feedbackBusy = true;
    var seq = this._feedbackReadSeq, selectedId = this._feedbackSelected.id;
    var button = document.getElementById('feedbackUpdateSubmit'); button.disabled = true;
    try {
      var response = await this.api(this._feedbackPath(selectedId) + '/update', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if (!response.ok) { this._feedbackNotice(seq, response.status === 409 ? '记录已被更新，请重新打开详情后评审。' : '处理记录未保存，请检查内容。'); return; }
      var data = await response.json();
      if (seq === this._feedbackReadSeq && this._feedbackAdmin && this._feedbackSelected && this._feedbackSelected.id === selectedId) this._renderFeedbackDetail(data.feedback);
      this._feedbackNotice(seq, '反馈 #' + selectedId + ' 已保存，提交者可以查看本次答复。');
    } catch (error) {
      this._feedbackNotice(seq, '保存结果尚未确认，请重新打开详情核对后再操作。');
    } finally {
      this._feedbackBusy = false; button.disabled = false;
    }
  }
});
