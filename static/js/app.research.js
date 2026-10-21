/* Owner-scoped research ledger. Retrieval and citation checks never imply semantic verification. */
(function() {
  'use strict';
  var kinds = {fact:'事实候选',calculation:'计算',inference:'推断',hypothesis:'假设'};
  var states = {queued:'等待开始',discovering:'发现线索',collecting:'采集资料',extracting:'提取内容',checking:'检查引用',synthesizing:'整理报告',completed:'已完成',partial:'部分完成',failed:'未完成',cancelled:'已取消',interrupted:'研究已中断'};
  function text(value) { return esc(ChatRender.stringify(value)); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function publication(doc) { if (!doc.published_at || doc.publication_precision === 'unknown') return '未知'; if (doc.publication_precision === 'day' || /^\d{4}-\d{2}-\d{2}$/.test(String(doc.published_at))) return String(doc.published_at).slice(0,10)+'（日精度）'; return ChatRender.formatTime(doc.published_at); }
  function empty(message) { return '<div class="research-empty">'+text(message)+'</div>'; }
  function note(items) { var labels={published_after_cutoff:'发布日期晚于信息截止',historical_availability_unknown:'尚不能确认截止前已公开可用',publication_precision_insufficient:'发布时间精度不足，无法确认截止前可用',semantic_review_pending:'尚未完成语义核验，需要核对原文是否支持判断',no_claims:'尚未形成可追溯的候选判断',no_sources:'尚未取得来源资料',structured_claims_missing:'未登记逐项结论',citation_checks_incomplete:'引用校验未完成',search_snippet_only:'仅取得搜索摘要',publication_unknown:'发布日期未知',original_not_verified:'原始全文尚未核验',published_after_as_of:'资料晚于观察时点',future_publication:'资料晚于观察时点',value_not_in_excerpt:'数值未在摘录中定位',unit_not_in_excerpt:'单位未在摘录中定位',period_not_in_excerpt:'期间未在摘录中定位',invalidation_missing:'缺失失效条件',support_missing:'缺少支持材料',interrupted:'研究已中断',excerpt_truncated:'仅保留部分摘录',interrupted_by_restart:'服务重启，研究保留为部分完成，未自动重跑'}; return list(items).length ? '<ul class="research-gaps">'+items.map(function(item){return '<li>'+text(labels[item] || item)+'</li>';}).join('')+'</ul>' : ''; }
  Object.assign(App, {
    _researchTab:'overview',
    _researchOnlyUnverified:false,
    _openResearchRun:async function(id) {
      var sequence = this._researchSeq = (this._researchSeq || 0)+1;
      this._researchRunId = id || ''; this._researchBundle = null; this._researchTab = 'overview'; this._researchSelectedRevision = '';
      var dialog = document.getElementById('researchDialog'); if (!dialog) return;
      if (!dialog.open) dialog.showModal();
      document.getElementById('researchTitle').textContent = id ? '读取研究记录…' : '我的研究记录';
      document.getElementById('researchBody').innerHTML = empty('正在读取已保存的研究…');
      document.getElementById('researchEvidence').hidden = true;
      try {
        var response = await this.api(id ? '/api/research/runs/'+encodeURIComponent(id) : '/api/research/runs');
        if (!response.ok) throw new Error('unavailable');
        var data = await response.json();
        if (sequence !== this._researchSeq || !dialog.open) return;
        if (!id) {
          document.getElementById('researchTabs').hidden = true;
          document.getElementById('researchBody').innerHTML = list(data.runs).length ? '<div class="research-run-list">'+data.runs.map(function(run){return '<button data-research-run="'+escAttr(run.run_id)+'"><strong>'+text(run.question || '研究记录')+'</strong><span>'+text(states[run.status] || run.status || '状态未记录')+' · '+text(ChatRender.formatTime(run.as_of))+'</span></button>';}).join('')+'</div>' : empty('还没有研究账本。新对话的真实采集与研究内容会在这里保留，旧报告仍可从报告库阅读。');
          return;
        }
        this._researchBundle = data;
        document.getElementById('researchTitle').textContent = (data.run || {}).question || '研究记录';
        this._renderResearch();
      } catch(error) {
        if (sequence === this._researchSeq && dialog.open) document.getElementById('researchBody').innerHTML = empty('研究记录暂时无法读取或没有访问权限。旧历史可能未建立账本；不会为它补造证据。');
      }
    },
    _renderResearch:function() {
      var data = this._researchBundle; if (!data) return;
      var run = data.run || {}, tab = this._researchTab, self = this;
      document.getElementById('researchTabs').hidden = false;
      document.querySelectorAll('[data-research-tab]').forEach(function(button){button.setAttribute('aria-selected',String(button.dataset.researchTab === tab));});
      var header = '<div class="research-boundary"><span>'+text(states[run.status] || run.status || '状态未知')+'</span><span>研究截至 '+text(ChatRender.formatTime(run.as_of))+'</span><span>数据截至：以各来源为准，缺失则未知</span></div>';
      if (run.information_cutoff) header += '<p class="research-note">信息截止：'+text(run.cutoff_precision === 'day' ? String(run.information_cutoff).slice(0,10)+'（日精度）' : ChatRender.formatTime(run.information_cutoff))+'；观察时间保持本轮实际时间。'+(run.requested_period?' 研究期间：'+text(run.requested_period):'')+'</p>';
      header += '<p class="research-notice">这里保留研究过程、原始线索与候选判断。引用结构匹配不等于语义核验；事实候选仍需核对原文。</p>';
      var body = '';
      if (tab === 'overview') {
        body = '<section class="research-section"><h3>核心判断与边界</h3>'+this._researchClaimsHTML(list(data.claims))+'</section>';
        body += '<section class="research-section"><h3>缺口与后续核查</h3>'+(note(data.gaps || run.gaps) || empty('尚未记录缺口；这不代表所有问题均已查清。'))+'</section>';
        if (run.conv_id) body += '<button class="research-action" data-research-conv="'+escAttr(run.conv_id)+'">返回来源会话</button>';
      } else if (tab === 'report') {
        var revisions = list(data.report_revisions), revision = revisions.find(function(item){return item.revision_id === self._researchSelectedRevision;}) || revisions[revisions.length-1];
        body = '<div class="research-version-bar"><span>已保存的固定版本</span><select id="researchRevisionSelect" aria-label="选择报告版本">'+revisions.map(function(item){return '<option value="'+escAttr(item.revision_id)+'"'+(revision && revision.revision_id === item.revision_id ? ' selected' : '')+'>版本 '+text(item.revision)+' · '+text(ChatRender.formatTime(item.created_at))+'</option>';}).join('')+'</select></div>';
        if (!revision) body += empty('本轮尚未保存结构化报告。可返回原会话阅读已生成内容，不会伪造完整报告。');
        else {
          body += '<p class="research-note">验收记录：'+text(states[revision.acceptance_status] || revision.acceptance_status || '未记录')+' · 原版本保持不变，追问形成新的研究记录。</p>';
          if (revision.narrative) body += '<section class="research-section"><h3>报告正文</h3><p class="research-note">模型输出，语义待核验</p><div class="research-prose">'+ChatRender.renderContent(revision.narrative)+'</div></section>';
          body += list(revision.blocks).map(function(block){
            var claims = list(data.claims).filter(function(claim){return list(block.claim_ids).indexOf(claim.claim_id)>=0;});
            if (block.kind==='gaps') return '<section class="research-section"><h3>未解决缺口</h3>'+note(revision.gaps)+'</section>';
            if (!claims.length && !block.text) return '';
            return '<section class="research-section"><h3>'+text(block.title || block.kind || '研究内容')+'</h3><div class="research-prose">'+ChatRender.renderContent(block.text || '')+'</div>'+(claims.length?self._researchClaimsHTML(claims):'')+'</section>';
          }).join('');
        }
      } else if (tab === 'evidence') {
        body = '<div class="research-version-bar"><h3>资料与原文定位</h3><label><input id="researchUnverified" type="checkbox"'+(this._researchOnlyUnverified?' checked':'')+'> 仅看缺少原文定位</label></div>';
        var documents = list(data.documents).filter(function(doc){return !self._researchOnlyUnverified || doc.original_obtained !== true || !list(data.passages).some(function(p){return p.document_id === doc.document_id && p.locator && ['original_page','original_section','original_table'].indexOf(p.locator.kind)>=0;});});
        body += documents.length ? documents.map(function(doc){var url=safeUrl(doc.url);return '<article class="research-source"><div><span class="research-tag">'+text(doc.content_scope==='search_snippet'?'搜索线索 · 非原文':doc.content_scope==='tool_returned_text'?'工具返回内容 · 非全文保证':'资料范围未知')+'</span><h3>'+text(doc.title || '未命名资料')+'</h3></div><p>发布 '+text(publication(doc))+' · 获取 '+text(ChatRender.formatTime(doc.retrieved_at))+'</p><p>来源类别：'+text(doc.source_kind || '未知')+' · 日期依据：'+text(doc.publication_basis || '未提供')+'</p><footer><button data-research-document="'+escAttr(doc.document_id)+'">查看摘录与定位</button>'+(url?'<a href="'+escAttr(url)+'" target="_blank" rel="noopener noreferrer">打开原链接 ↗</a>':'')+'</footer></article>';}).join('') : empty('没有符合条件的资料；没有资料不等于事实不存在。');
      } else body = this._researchRelationsHTML(data);
      document.getElementById('researchBody').innerHTML = header + body;
    },
    _researchFormulaText:function(formula) {
      if (!formula || typeof formula!=='object') return text('计算式未完整记录');
      var symbols={add:'＋',subtract:'－',multiply:'×',divide:'÷'}, all=list((this._researchBundle || {}).claims);
      var inputs=list(formula.input_claim_ids).map(function(id){var claim=all.find(function(item){return item.claim_id===id;});return claim && claim.value!=null ? String(claim.value)+(claim.unit?' '+claim.unit:'') : '输入待核对';});
      return text(inputs.join(' '+(symbols[formula.operator] || '计算')+' ')+' ＝ '+(formula.computed_value==null?'待核对':formula.computed_value));
    },
    _researchClaimsHTML:function(claims) {
      if (!claims.length) return empty('尚未记录候选判断。原始回复不会被自动追认为已核验事实。');
      var self=this;
      return '<div class="research-claims">'+claims.map(function(claim){return '<article class="research-claim"><div><span class="research-tag">'+text(kinds[claim.kind] || '判断类型未标注')+'</span><span class="research-note">语义待核验</span></div><p>'+text(claim.statement)+'</p>'+(claim.period?'<p class="research-note">所属期间：'+text(claim.period)+'</p>':'')+(claim.value!=null?'<p>数值 '+text(claim.value)+' '+text(claim.unit || '单位未提供')+' '+text(claim.currency || '')+'</p>':'')+(claim.formula?'<p class="research-note">计算式：'+self._researchFormulaText(claim.formula)+'</p>':'')+'<button data-research-claim="'+escAttr(claim.claim_id)+'">查看支持、反方与限制 ↗</button></article>';}).join('')+'</div>';
    },
    _researchRelationsHTML:function(data) {
      var dimensions = [['supply','上游供给'],['certification','认证周期'],['substitution','替代路线'],['cost','成本传导'],['demand','下游需求']];
      var relations = list(data.relations);
      var body = '<section class="research-section"><h3>产业关系与传导机制</h3><p class="research-note">产品用途不等于具名供货，送样不等于量产，连线数量不证明瓶颈。以下仅呈现本轮已记录的候选关系。</p>';
      body += relations.length ? '<div class="research-relations">'+relations.map(function(relation){return '<button data-research-relation="'+escAttr(relation.relation_id)+'"><span>'+text(relation.from_entity)+' → '+text(relation.to_entity)+'</span><strong>'+text(relation.product || relation.relation_type || '关系待明确')+'</strong><small>'+text(relation.business_stage || '业务阶段未知')+' · 语义待核验</small></button>';}).join('')+'</div>' : empty('本轮尚无可追溯的产业关系。背景图谱仍可浏览，但不补画已证实供货链。');
      body += '</section><section class="research-section"><h3>关系核查矩阵</h3><div class="research-matrix">'+dimensions.map(function(dimension){var rows=relations.filter(function(r){return r.dimension===dimension[0];});return '<article><h4>'+dimension[1]+'</h4>'+(rows.length?rows.map(function(r){return '<button data-research-relation="'+escAttr(r.relation_id)+'">'+text(r.mechanism || r.product || '查看候选关系')+'</button>';}).join(''):'<p>尚未取得本维度的关系证据</p>')+'<button class="research-followup" data-research-question="'+escAttr('请基于本轮研究，只补查'+dimension[1]+'；区分原始披露、推断、反证与仍然缺失的资料。')+'">在原会话拟定局部深挖 ↗</button></article>';}).join('')+'</div></section>';
      return body;
    },
    _showResearchEvidence:function(kind,id) {
      var data=this._researchBundle; if(!data)return;
      var drawer=document.getElementById('researchEvidence'); if(!drawer)return;
      var title='',body='',self=this;
      function passages(ids,label){return '<h4>'+label+'</h4>'+ (list(ids).length?list(ids).map(function(entry){var pid=typeof entry==='string'?entry:entry.passage_id || entry.evidence_id;var passage=list(data.passages).find(function(p){return p.passage_id===pid;});if(!passage)return '<p>引用定位不可用，待补查</p>';var doc=list(data.documents).find(function(d){return d.document_id===passage.document_id;})||{};return '<div class="research-passage"><blockquote>'+text(passage.excerpt)+'</blockquote><p>'+text(doc.title || '来源未命名')+' · '+text(passage.locator || '定位未提供')+'</p><button data-research-document="'+escAttr(passage.document_id)+'">查看资料记录</button></div>';}).join(''):'<p>未记录</p>');}
      if(kind==='claim'){
        var claim=list(data.claims).find(function(c){return c.claim_id===id;});if(!claim)return;
        title=kinds[claim.kind] || '候选判断';body='<p>'+text(claim.statement)+'</p><p class="research-notice">引用检查：'+text(claim.citation_status === 'valid' ? '引用结构有效' : claim.citation_status || '未记录')+'；语义仍待核验。</p>'+passages(claim.supports,'支持材料')+passages(claim.opposes,'反方材料')+passages(claim.background,'背景材料')+'<h4>限制与缺口</h4>'+text(claim.limitations || '未记录')+note(claim.gaps);
      } else if(kind==='document'){
        var doc=list(data.documents).find(function(d){return d.document_id===id;});if(!doc)return;
        title=doc.title || '来源资料';var url=safeUrl(doc.url);
        body='<p class="research-notice">'+text(doc.content_scope==='search_snippet'?'这里只取得搜索摘要，不能视为原始全文。':'只展示工具实际返回的内容范围，引用是否支持判断仍需核验。')+'</p><p>发布：'+text(publication(doc))+'<br>采集：'+text(ChatRender.formatTime(doc.retrieved_at))+'</p>'+(url?'<a href="'+escAttr(url)+'" target="_blank" rel="noopener noreferrer">原始链接 ↗</a>':'')+list(data.passages).filter(function(p){return p.document_id===id;}).map(function(p){return '<div class="research-passage"><p>'+text(p.locator || '定位未提供')+'</p><blockquote>'+text(p.excerpt)+'</blockquote></div>';}).join('');
      } else {
        var relation=list(data.relations).find(function(r){return r.relation_id===id;});if(!relation)return;
        title=ChatRender.stringify(relation.from_entity)+' → '+ChatRender.stringify(relation.to_entity);
        body='<p>'+text(relation.product || relation.process || '')+'</p><p>关系类型：'+text(relation.relation_type || '未知')+'<br>业务阶段：'+text(relation.business_stage || '未知')+'<br>有效期间：'+text(relation.valid_period || '未知')+'</p><h4>传导机制</h4><p>'+text(relation.mechanism || '尚未记录')+'</p><h4>失效条件</h4><p>'+text(relation.invalidators || '尚未记录')+'</p>'+self._researchClaimsHTML(list(data.claims).filter(function(c){return list(relation.supporting_claims).concat(list(relation.opposing_claims)).indexOf(c.claim_id)>=0;}))+'<p class="research-notice">以上为候选关系，不能仅凭引用存在认定供货或经济效应已证实。</p>';
      }
      drawer.innerHTML='<header><h3>'+text(title)+'</h3><button data-research-close-evidence aria-label="关闭证据详情">×</button></header>'+body;drawer.hidden=false;
    }
  });
  document.addEventListener('click',function(event){
    var button=event.target.closest('[data-research-run],[data-research-tab],[data-research-claim],[data-research-document],[data-research-relation],[data-research-close-evidence],[data-research-conv],[data-research-question]');if(!button)return;
    var d=button.dataset;
    if(d.researchRun!==undefined)App._openResearchRun(d.researchRun);
    else if(d.researchTab){App._researchTab=d.researchTab;document.getElementById('researchEvidence').hidden=true;App._renderResearch();}
    else if(d.researchClaim)App._showResearchEvidence('claim',d.researchClaim);
    else if(d.researchDocument)App._showResearchEvidence('document',d.researchDocument);
    else if(d.researchRelation)App._showResearchEvidence('relation',d.researchRelation);
    else if(d.researchCloseEvidence!==undefined)document.getElementById('researchEvidence').hidden=true;
    else if(d.researchConv || d.researchQuestion){
      var conv=d.researchConv || ((App._researchBundle || {}).run || {}).conv_id;
      if(!conv){App._toast('本记录未关联会话，请在研究对话中提出问题');return;}
      document.getElementById('researchDialog').close();App.switchMode('chat');
      App.openConv(conv).then(function(){if(d.researchQuestion && App.convId===conv){var input=document.getElementById('input');input.value=d.researchQuestion;input.focus();App._toast('已拟定局部深挖问题，发送后才会开始新研究');}});
    }
  });
  document.addEventListener('change',function(event){
    if(event.target.id==='researchRevisionSelect'){App._researchSelectedRevision=event.target.value;App._renderResearch();}
    if(event.target.id==='researchUnverified'){App._researchOnlyUnverified=event.target.checked;App._renderResearch();}
  });
})();
