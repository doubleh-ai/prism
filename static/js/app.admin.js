Object.assign(App, {
  _adminUsers: [],
  _adminStats: null,
  _adminHealth: null,
  _adminAnalytics: null,
  _adminInsights: null,
  _adminActivityUsers: null,
  _adminActivityEvents: null,
  _adminDiagnostics: null,
  _adminAnalyticsRange: '7d',
  _adminEventFilters: {username: '', feature: '', action: '', status: '', query: ''},
  _adminTab: 'overview',
  _adminCurrentUser: '',
  _adminCharts: {},

  _initAdmin: function() {
    if (!this.isAdmin) {
      this._toast && this._toast('无管理权限');
      this.switchMode('chat');
      return;
    }
    if (this._adminTab === 'operations' && this._activateOperations) {
      this._renderAdminUsers(this.user);
      this._activateOperations();
      return;
    }
    this._loadAdminUsers();
  },

  _adminSetStatus: function(msg) {
    var el = document.getElementById('adminSidebarStatus');
    if (el) el.textContent = msg || '';
  },

  _adminFormatDate: function(ts) {
    if (!ts) return '-';
    try { return new Date(ts * 1000).toLocaleString('zh-CN'); }
    catch(e) { return '-'; }
  },

  _adminFmtNum: function(n) {
    n = Number(n || 0);
    if (n >= 10000) return (Math.round(n / 1000) / 10) + '万';
    return String(n);
  },

  _adminPct: function(n, d) {
    n = Number(n || 0); d = Number(d || 0);
    if (!d) return '0%';
    return Math.round(n * 1000 / d) / 10 + '%';
  },

  _adminFeatureLabel: function(feature) {
    var labels = {
      auth: '登录安全',
      admin: '后台管理',
      api: 'API',
      chat: '对话',
      conversation: '会话',
      market: '行情',
      indicator: '指标',
      analysis: 'A股分析',
      vibe: 'Vibe Trading',
      report: '报告',
      graph: '关系图',
      industry: '产业图谱',
      release: '更新日志'
    };
    return labels[feature] || feature || '-';
  },

  _adminActionLabel: function(action) {
    var labels = {
      login: '登录',
      login_rate_limited: '登录限流',
      logout: '退出',
      open_users: '打开账号控制',
      create_user: '创建账号',
      reset_password: '重置密码',
      set_user_status: '启用/禁用账号',
      delete_user: '删除账号',
      list: '列表',
      create: '创建',
      open: '打开',
      delete: '删除',
      batch_delete: '批量删除',
      open_trash: '打开回收站',
      restore: '恢复',
      delete_permanent: '永久删除',
      set_skill: '切换技能',
      stream: '生成内容',
      respond: '回答追问',
      cancel: '停止生成',
      explain_term: '术语解释',
      search: '搜索',
      quote: '查看行情',
      batch_quote: '批量行情',
      kline: '查看K线',
      fundamentals: '查看基本面',
      provider_health: '数据源健康',
      translate: '翻译指标',
      eval: '运行指标',
      scan: '指标扫描',
      start: '启动分析',
      open_report: '打开分析报告',
      save_analysis_report: '保存报告',
      list_library: '打开报告库',
      open_saved: '打开保存报告',
      open_file: '打开报告文件',
      create_plan: '生成计划',
      list_plans: '查看历史计划',
      open_plan: '打开计划',
      delete_plan: '删除计划',
      watchlist_add: '加入自选',
      watchlist_remove: '移出自选',
      open_changelog: '查看更新日志'
    };
    return labels[action] || action || '-';
  },

  _loadAdminUsers: async function() {
    var inner = document.getElementById('adminPanelInner');
    if (inner) inner.innerHTML = '<div class="market-empty-state">加载管理看板中...</div>';
    var range = encodeURIComponent(this._adminAnalyticsRange || '7d');
    var usersReq = this.api('/api/admin/users');
    var statsReq = this.api('/api/admin/stats');
    var healthReq = this.api('/api/admin/health');
    var overviewReq = this.api('/api/admin/analytics/overview?range=' + range);
    var insightsReq = this.api('/api/admin/analytics/insights?range=' + range);
    var activityUsersReq = this.api('/api/admin/analytics/users?range=' + range);
    var eventsReq = this.api(this._adminEventsUrl());
    var diagnosticsReq = this.api('/api/admin/diagnostics?status=open&limit=120');
    var resps = await Promise.all([usersReq, statsReq, healthReq, overviewReq, insightsReq, activityUsersReq, eventsReq, diagnosticsReq]);
    var usersResp = resps[0];
    if (!usersResp.ok) {
      if (inner) inner.innerHTML = '<div class="admin-empty">无法加载账号</div>';
      return;
    }
    var data = await usersResp.json();
    this._adminUsers = data.users || [];
    this._adminStats = resps[1].ok ? await resps[1].json() : null;
    this._adminHealth = resps[2].ok ? await resps[2].json() : null;
    this._adminAnalytics = resps[3].ok ? await resps[3].json() : null;
    this._adminInsights = resps[4].ok ? await resps[4].json() : null;
    this._adminActivityUsers = resps[5].ok ? await resps[5].json() : null;
    this._adminActivityEvents = resps[6].ok ? await resps[6].json() : null;
    this._adminDiagnostics = resps[7].ok ? await resps[7].json() : null;
    this._renderAdminUsers(data.current_user || this.user);
  },

  _adminEventsUrl: function(offset) {
    var range = encodeURIComponent(this._adminAnalyticsRange || '7d');
    var f = this._adminEventFilters || {};
    var qs = ['range=' + range, 'limit=120', 'offset=' + Math.max(0, Number(offset || 0))];
    if (f.username) qs.push('username=' + encodeURIComponent(f.username));
    if (f.feature) qs.push('feature=' + encodeURIComponent(f.feature));
    if (f.action) qs.push('action=' + encodeURIComponent(f.action));
    if (f.status) qs.push('status=' + encodeURIComponent(f.status));
    if (f.query) qs.push('query=' + encodeURIComponent(f.query));
    return '/api/admin/analytics/events?' + qs.join('&');
  },

  _adminSetRange: async function(range) {
    this._adminAnalyticsRange = range || '7d';
    await this._loadAdminUsers();
  },

  _adminSetActivityFilter: async function(key, value) {
    this._adminEventFilters = this._adminEventFilters || {};
    this._adminEventFilters[key] = value || '';
    await this._reloadAdminEvents(true);
  },

  _adminApplyActivityQuery: async function(value) {
    this._adminEventFilters = this._adminEventFilters || {};
    this._adminEventFilters.query = String(value || '').trim();
    await this._reloadAdminEvents(true);
  },

  _adminClearActivityFilters: async function() {
    this._adminEventFilters = {username: '', feature: '', action: '', status: '', query: ''};
    await this._reloadAdminEvents(true);
    this._renderAdminUsers(this._adminCurrentUser || this.user);
  },

  _reloadAdminEvents: async function(reset) {
    var box = document.getElementById('adminEventTimeline');
    if (box) box.innerHTML = '<div class="admin-empty small">加载行为明细中...</div>';
    var r = await this.api(this._adminEventsUrl(reset ? 0 : (((this._adminActivityEvents || {}).events || []).length)));
    this._adminActivityEvents = r.ok ? await r.json() : {events: []};
    if (box) box.innerHTML = this._renderAdminEventRows();
  },

  _adminLoadMoreEvents: async function() {
    var current = this._adminActivityEvents || {events: []};
    var existing = current.events || [];
    var r = await this.api(this._adminEventsUrl(existing.length));
    if (!r.ok) return;
    var next = await r.json();
    next.events = existing.concat(next.events || []);
    this._adminActivityEvents = next;
    var box = document.getElementById('adminEventTimeline');
    if (box) box.innerHTML = this._renderAdminEventRows();
  },

  _adminSetTab: function(tab) {
    if (!this.isAdmin) return;
    if (this._pauseOperations) this._pauseOperations();
    this._adminTab = tab || 'overview';
    this._renderAdminUsers(this._adminCurrentUser || this.user);
    if (this._adminTab === 'operations' && this._activateOperations) this._activateOperations();
  },

  _adminInspectUser: async function(username) {
    this._adminTab = 'activity';
    this._adminEventFilters = this._adminEventFilters || {};
    this._adminEventFilters.username = username || '';
    var r = await this.api(this._adminEventsUrl(0));
    this._adminActivityEvents = r.ok ? await r.json() : {events: []};
    this._renderAdminUsers(this._adminCurrentUser || this.user);
    setTimeout(function() {
      var box = document.getElementById('adminEventTimeline');
      if (box) box.scrollIntoView({behavior: 'smooth', block: 'start'});
    }, 0);
  },

  _adminStatusPill: function(ok, label, detail) {
    return '<div class="admin-health-item ' + (ok ? 'ok' : 'bad') + '">' +
      '<span>' + esc(label) + '</span>' +
      '<strong>' + esc(ok ? '正常' : '异常') + '</strong>' +
      (detail ? '<em>' + esc(detail) + '</em>' : '') +
    '</div>';
  },

  _renderAdminHealth: function() {
    var h = this._adminHealth;
    if (!h) return '';
    var checks = h.checks || {};
    var db = checks.database || {};
    var native = checks.native_core || {};
    var tools = checks.tool_catalog || {};
    var provider = checks.provider_configuration || {};
    var models = h.models || [];
    var configuredModels = models.filter(function(m){ return !!m.configured; }).length;
    var cards = [
      this._adminStatusPill(!!h.ok, '总览', (h.version || '-') + ' · ' + (h.deployed_revision || '未部署')),
      this._adminStatusPill(db.status === 'ok', '数据库', db.latency_ms != null ? db.latency_ms + ' ms' : (db.error || '-')),
      this._adminStatusPill(native.status === 'ok', '研究运行器', 'Prism Native'),
      this._adminStatusPill(tools.status === 'ok', '平台能力', (tools.count || 0) + ' 项'),
      this._adminStatusPill(provider.status === 'ok', '模型配置', configuredModels + ' / ' + models.length + ' 已配置'),
    ].join('');
    var modelRows = models.map(function(m) {
      return '<tr>' +
        '<td>' + esc(m.name || m.id) + '</td>' +
        '<td><span class="admin-pill ' + (m.configured ? 'ok' : 'off') + '">' + esc(m.configured ? '已配置' : '未配置') + '</span></td>' +
        '<td>直接 API</td>' +
      '</tr>';
    }).join('');
    return '<section class="admin-health">' +
      '<div class="admin-section-title">系统健康</div><p>配置检查不调用模型；研究质量与真实接入另行验证。</p>' +
      '<div class="admin-health-grid">' + cards + '</div>' +
      '<div class="admin-table-wrap admin-health-table"><table class="admin-table"><thead><tr><th>模型</th><th>配置</th><th>接入</th></tr></thead><tbody>' + modelRows + '</tbody></table></div>' +
    '</section>';
  },

  _renderAdminStats: function() {
    var stats = this._adminStats || {};
    var t = stats.totals || {};
    var cards = [
      ['用户', t.users, '启用 ' + this._adminFmtNum(t.enabled_users) + ' / 禁用 ' + this._adminFmtNum(t.disabled_users)],
      ['活跃会话', t.active_sessions, '近24h登录 ' + this._adminFmtNum(t.logins_24h)],
      ['对话', t.conversations, '近24h新增 ' + this._adminFmtNum(t.conversations_24h)],
      ['消息', t.messages, '近24h新增 ' + this._adminFmtNum(t.messages_24h)],
    ].map(function(c) {
      return '<div class="admin-stat-card"><span>' + esc(c[0]) + '</span><strong>' + esc(App._adminFmtNum(c[1])) + '</strong><em>' + esc(c[2]) + '</em></div>';
    }).join('');

    return '<section class="admin-stats">' +
      '<div class="admin-section-title">平台规模</div>' +
      '<div class="admin-stat-grid">' + cards + '</div>' +
      '</section>';
  },

  _renderAdminAnalytics: function() {
    var a = this._adminAnalytics || {};
    var t = a.totals || {};
    var prev = a.previous || {};
    var insights = this._adminInsights || {};
    var engagement = insights.engagement || {};
    var range = this._adminAnalyticsRange || '7d';
    var rangeBtns = [['24h', '24小时'], ['7d', '7天'], ['30d', '30天'], ['90d', '90天']].map(function(r) {
      return '<button class="admin-range-btn ' + (range === r[0] ? 'active' : '') + '" onclick="App._adminSetRange(\'' + r[0] + '\')">' + r[1] + '</button>';
    }).join('');
    var trend = Number(t.events || 0) - Number(prev.events || 0);
    var trendText = (trend >= 0 ? '+' : '') + this._adminFmtNum(trend) + ' vs 上期';
    var cards = [
      ['行为事件', t.events, '近24h ' + this._adminFmtNum(t.events_24h) + ' · ' + trendText],
      ['真实活跃', engagement.active_users, '新注册 ' + this._adminFmtNum(engagement.new_users) + ' · 沉默 ' + this._adminFmtNum(engagement.dormant_users)],
      ['使用会话', engagement.sessions, '会话均 ' + this._adminFmtNum(engagement.events_per_session) + ' 次行为'],
      ['回访率', (engagement.returning_rate || 0) + '%', '回访用户 ' + this._adminFmtNum(engagement.returning_users)],
      ['成功率', (engagement.success_rate || 0) + '%', '失败 ' + this._adminFmtNum(t.failed_events) + ' 次'],
      ['P95 耗时', this._adminFormatDuration(engagement.p95_duration_ms), '慢请求 ' + this._adminFmtNum(engagement.slow_events) + ' 次'],
    ].map(function(c) {
      return '<div class="admin-stat-card"><span>' + esc(c[0]) + '</span><strong>' + esc(String(c[1] == null ? 0 : c[1])) + '</strong><em>' + esc(c[2]) + '</em></div>';
    }).join('');
    return '<section class="admin-analytics">' +
      '<div class="admin-section-row"><div><div class="admin-section-title">运营概览</div><p>统计时区为北京时间，回访按区间内至少 2 个活跃日计算</p></div><div class="admin-range">' + rangeBtns + '</div></div>' +
      '<div class="admin-stat-grid admin-stat-grid--analytics">' + cards + '</div>' +
      this._renderAdminChartShell() +
      '<div class="admin-insight-grid">' + this._renderAdminEngagementCard() + this._renderAdminFunnels() + '</div>' +
    '</section>';
  },

  _adminFormatDuration: function(ms) {
    ms = Number(ms || 0);
    if (!ms) return '-';
    if (ms >= 60000) return (Math.round(ms / 6000) / 10) + ' min';
    if (ms >= 1000) return (Math.round(ms / 100) / 10) + ' s';
    return Math.round(ms) + ' ms';
  },

  _renderAdminChartShell: function() {
    return '<div class="admin-chart-grid">' +
      '<div class="admin-chart-card"><div class="admin-chart-head"><strong>活跃趋势</strong><span>事件 / 活跃用户 / 失败率</span></div><div class="admin-chart" id="adminTrendChart" aria-label="活跃趋势图"></div></div>' +
      '<div class="admin-chart-card"><div class="admin-chart-head"><strong>访问时段</strong><span>北京时间 0-23 时</span></div><div class="admin-chart" id="adminHourlyChart" aria-label="访问时段图"></div></div>' +
    '</div>';
  },

  _renderAdminEngagementCard: function() {
    var e = ((this._adminInsights || {}).engagement) || {};
    var rows = [
      ['日均活跃', this._adminFmtNum(e.avg_daily_active), '区间内每日平均'],
      ['活跃粘性', (e.stickiness || 0) + '%', '日均活跃 / 区间活跃'],
      ['人均行为', this._adminFmtNum(e.events_per_active_user), '每位活跃用户'],
      ['平均活跃天', this._adminFmtNum(e.avg_active_days), '每位活跃用户'],
      ['平均会话', this._adminFormatDuration(Number(e.avg_session_duration_sec || 0) * 1000), '30 分钟无操作切分'],
      ['功能覆盖', this._adminFmtNum(((this._adminAnalytics || {}).totals || {}).feature_count), '发生过使用的功能'],
    ].map(function(row) {
      return '<div class="admin-metric-row"><span>' + esc(row[0]) + '</span><strong>' + esc(String(row[1])) + '</strong><em>' + esc(row[2]) + '</em></div>';
    }).join('');
    return '<div class="admin-insight-card"><div class="admin-section-title">参与度与留存</div><div class="admin-metric-list">' + rows + '</div></div>';
  },

  _renderAdminFunnels: function() {
    var funnels = (this._adminInsights && this._adminInsights.funnels) || [];
    var html = funnels.map(function(funnel) {
      var steps = (funnel.steps || []).map(function(step, index) {
        var width = Math.max(3, Math.min(100, Number(step.conversion || 0)));
        return '<div class="admin-funnel-step">' +
          '<div><span>' + esc(step.label) + '</span><strong>' + esc(App._adminFmtNum(step.users)) + ' 人</strong><em>' + esc(index ? (step.conversion || 0) + '%' : '起点') + '</em></div>' +
          '<i><b style="width:' + width + '%"></b></i>' +
        '</div>';
      }).join('');
      return '<div class="admin-funnel"><h4>' + esc(funnel.label) + '</h4>' + steps + '</div>';
    }).join('');
    return '<div class="admin-insight-card admin-insight-card--funnels"><div class="admin-section-title">核心路径转化</div>' +
      (html || '<div class="admin-empty small">暂无足够的转化数据</div>') + '</div>';
  },

  _adminDisposeCharts: function() {
    var charts = this._adminCharts || {};
    Object.keys(charts).forEach(function(key) {
      try { charts[key].dispose(); } catch(e) {}
    });
    this._adminCharts = {};
  },

  _renderAdminCharts: function() {
    if (typeof echarts === 'undefined') return;
    var trendEl = document.getElementById('adminTrendChart');
    var hourlyEl = document.getElementById('adminHourlyChart');
    if (!trendEl || !hourlyEl) return;
    this._adminDisposeCharts();
    var styles = getComputedStyle(document.documentElement);
    var text = styles.getPropertyValue('--text2').trim() || '#8b949e';
    var muted = styles.getPropertyValue('--text3').trim() || '#6e7681';
    var border = styles.getPropertyValue('--border').trim() || 'rgba(128,128,128,.18)';
    var accent = styles.getPropertyValue('--accent').trim() || '#78a5dc';
    var daily = ((this._adminAnalytics || {}).daily) || [];
    var trend = echarts.init(trendEl);
    trend.setOption({
      animationDuration: 260,
      textStyle: {fontFamily: 'inherit', color: text},
      grid: {left: 42, right: 42, top: 20, bottom: 34},
      tooltip: {trigger: 'axis', backgroundColor: styles.getPropertyValue('--bg2').trim(), borderColor: border, textStyle: {color: text}},
      legend: {top: 0, right: 0, textStyle: {color: muted, fontSize: 10}, data: ['事件', '活跃用户', '失败率']},
      xAxis: {type: 'category', data: daily.map(function(r){ return (r.day || '').slice(5); }), axisLabel: {color: muted, fontSize: 10}, axisLine: {lineStyle: {color: border}}, axisTick: {show: false}},
      yAxis: [
        {type: 'value', minInterval: 1, axisLabel: {color: muted, fontSize: 10}, splitLine: {lineStyle: {color: border}}},
        {type: 'value', min: 0, max: 100, axisLabel: {color: muted, fontSize: 10, formatter: '{value}%'}, splitLine: {show: false}}
      ],
      series: [
        {name: '事件', type: 'bar', data: daily.map(function(r){ return Number(r.events || 0); }), barMaxWidth: 18, itemStyle: {color: accent, borderRadius: [3,3,0,0]}},
        {name: '活跃用户', type: 'line', data: daily.map(function(r){ return Number(r.active_users || 0); }), smooth: true, symbolSize: 5, lineStyle: {color: '#2da44e', width: 2}, itemStyle: {color: '#2da44e'}},
        {name: '失败率', type: 'line', yAxisIndex: 1, data: daily.map(function(r){ return 100 - Number(r.success_rate || 100); }), smooth: true, symbol: 'none', lineStyle: {color: '#cf222e', width: 1.5}}
      ]
    });
    var hourly = (((this._adminInsights || {}).hourly) || []);
    var hourlyChart = echarts.init(hourlyEl);
    hourlyChart.setOption({
      animationDuration: 260,
      textStyle: {fontFamily: 'inherit', color: text},
      grid: {left: 38, right: 18, top: 20, bottom: 34},
      tooltip: {trigger: 'axis', backgroundColor: styles.getPropertyValue('--bg2').trim(), borderColor: border, textStyle: {color: text}},
      xAxis: {type: 'category', data: hourly.map(function(r){ return String(r.hour).padStart(2, '0'); }), axisLabel: {color: muted, fontSize: 9, interval: 2}, axisLine: {lineStyle: {color: border}}, axisTick: {show: false}},
      yAxis: {type: 'value', minInterval: 1, axisLabel: {color: muted, fontSize: 10}, splitLine: {lineStyle: {color: border}}},
      series: [{name: '事件', type: 'bar', data: hourly.map(function(r){ return Number(r.events || 0); }), barMaxWidth: 12, itemStyle: {color: accent, borderRadius: [3,3,0,0]}}]
    });
    this._adminCharts = {trend: trend, hourly: hourlyChart};
    if (!this._adminChartResizeBound) {
      this._adminChartResizeBound = true;
      window.addEventListener('resize', function() {
        var charts = App._adminCharts || {};
        Object.keys(charts).forEach(function(key) { try { charts[key].resize(); } catch(e) {} });
      });
    }
  },

  _renderAdminFeatureTable: function() {
    var rows = ((this._adminAnalytics || {}).features || []).map(function(f) {
      return '<tr>' +
        '<td><span class="admin-user-name">' + esc(App._adminFeatureLabel(f.feature)) + '</span><em>' + esc(f.feature) + '</em></td>' +
        '<td>' + esc(App._adminFmtNum(f.avg_dau)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(f.peak_dau)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(f.active_users)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(f.events)) + '</td>' +
        '<td>' + esc(String(f.event_share || 0)) + '%</td>' +
        '<td>' + esc(App._adminPct(f.failed_events, f.events)) + '</td>' +
        '<td>' + esc(App._adminFormatDuration(f.avg_duration_ms)) + '</td>' +
        '<td>' + esc(App._adminFormatDuration(f.p95_duration_ms)) + '</td>' +
        '<td title="' + escAttr(App._adminFormatDate(f.last_event_at)) + '">' + esc(App._adminFormatDate(f.last_event_at)) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="admin-analytics-card"><div class="admin-section-title">功能使用矩阵</div>' +
      (rows ? '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-feature-table"><thead><tr><th>功能</th><th>平均 DAU</th><th>峰值 DAU</th><th>区间用户</th><th>事件</th><th>占比</th><th>失败率</th><th>平均耗时</th><th>P95</th><th>最近使用</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty small">暂无功能行为数据</div>') +
    '</div>';
  },

  _adminTargetTypeLabel: function(type) {
    var labels = {ticker: '股票', query: '搜索', indicator: '指标', report: '报告', vibe_plan: 'Vibe 计划'};
    return labels[type] || type || '-';
  },

  _renderAdminTopTargets: function() {
    var rows = (((this._adminInsights || {}).top_targets) || []).map(function(item) {
      return '<tr>' +
        '<td><span class="admin-user-name">' + esc(item.target_label || item.target_id) + '</span><em>' + esc(item.target_id || '') + '</em></td>' +
        '<td>' + esc(App._adminTargetTypeLabel(item.target_type)) + '</td>' +
        '<td>' + esc(App._adminFeatureLabel(item.feature)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(item.active_users)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(item.events)) + '</td>' +
        '<td title="' + escAttr(App._adminFormatDate(item.last_event_at)) + '">' + esc(App._adminFormatDate(item.last_event_at)) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="admin-analytics-card"><div class="admin-section-title">热门研究对象</div>' +
      (rows ? '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-target-table"><thead><tr><th>对象</th><th>类型</th><th>来源功能</th><th>用户</th><th>次数</th><th>最近使用</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty small">暂无对象数据</div>') +
    '</div>';
  },

  _renderAdminPerformance: function() {
    var rows = (((this._adminInsights || {}).slow_actions) || []).map(function(item) {
      var bad = Number(item.failure_rate || 0) > 0;
      return '<tr>' +
        '<td><span class="admin-user-name">' + esc(App._adminFeatureLabel(item.feature)) + '</span><em>' + esc(App._adminActionLabel(item.action)) + '</em></td>' +
        '<td>' + esc(App._adminFmtNum(item.events)) + '</td>' +
        '<td><span class="admin-pill ' + (bad ? 'off' : 'ok') + '">' + esc(String(item.failure_rate || 0)) + '%</span></td>' +
        '<td>' + esc(App._adminFormatDuration(item.avg_duration_ms)) + '</td>' +
        '<td>' + esc(App._adminFormatDuration(item.p95_duration_ms)) + '</td>' +
        '<td>' + esc(App._adminFormatDuration(item.max_duration_ms)) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="admin-analytics-card"><div class="admin-section-title">性能与失败诊断</div>' +
      (rows ? '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-performance-table"><thead><tr><th>功能 / 动作</th><th>次数</th><th>失败率</th><th>平均</th><th>P95</th><th>最慢</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty small">暂无性能数据</div>') +
    '</div>';
  },

  _renderAdminErrors: function() {
    var rows = (((this._adminInsights || {}).errors) || []).map(function(item) {
      return '<tr>' +
        '<td>' + esc(App._adminFeatureLabel(item.feature)) + '</td>' +
        '<td>' + esc(App._adminActionLabel(item.action)) + '</td>' +
        '<td><span class="admin-pill off">HTTP ' + esc(item.status_code || '-') + '</span></td>' +
        '<td>' + esc(App._adminFmtNum(item.events)) + '</td>' +
        '<td title="' + escAttr(App._adminFormatDate(item.last_event_at)) + '">' + esc(App._adminFormatDate(item.last_event_at)) + '</td>' +
      '</tr>';
    }).join('');
    return '<div class="admin-analytics-card"><div class="admin-section-title">失败分布</div>' +
      (rows ? '<div class="admin-table-wrap"><table class="admin-table admin-error-table"><thead><tr><th>功能</th><th>动作</th><th>状态码</th><th>次数</th><th>最近发生</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty small">当前区间没有失败事件</div>') +
    '</div>';
  },

  _renderAdminUserActivityTable: function() {
    var rows = (((this._adminActivityUsers || {}).users) || []).map(function(u) {
      var disabled = u.disabled_at ? '<span class="admin-pill off">禁用</span>' : '<span class="admin-pill ok">启用</span>';
      return '<tr>' +
        '<td><span class="admin-user-name">' + esc(u.username) + '</span> ' + disabled + '</td>' +
        '<td>' + esc(App._adminFmtNum(u.events)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(u.sessions)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(u.feature_count)) + '</td>' +
        '<td>' + esc(App._adminFmtNum(u.active_days)) + '</td>' +
        '<td>' + esc(String(u.success_rate || 0)) + '%</td>' +
        '<td>' + esc(App._adminFeatureLabel(u.top_feature)) + '</td>' +
        '<td title="' + escAttr(App._adminFormatDate(u.last_event_at)) + '">' + esc(App._adminFormatDate(u.last_event_at)) + '</td>' +
        '<td><button class="admin-btn" onclick="App._adminInspectUser(\'' + escAttr(u.username) + '\')">查看轨迹</button></td>' +
      '</tr>';
    }).join('');
    return '<div class="admin-analytics-card"><div class="admin-section-title">用户活跃明细</div>' +
      (rows ? '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-user-usage-table"><thead><tr><th>账号</th><th>事件</th><th>会话</th><th>功能数</th><th>活跃天</th><th>成功率</th><th>最常用</th><th>最近行为</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty small">暂无用户行为数据</div>') +
    '</div>';
  },

  _renderAdminEventTimeline: function() {
    var a = this._adminAnalytics || {};
    var features = (a.features || []).map(function(f) {
      var val = escAttr(f.feature);
      return '<option value="' + val + '"' + ((App._adminEventFilters || {}).feature === f.feature ? ' selected' : '') + '>' + esc(App._adminFeatureLabel(f.feature)) + '</option>';
    }).join('');
    var users = (this._adminUsers || []).map(function(u) {
      var val = escAttr(u.username);
      return '<option value="' + val + '"' + ((App._adminEventFilters || {}).username === u.username ? ' selected' : '') + '>' + esc(u.username) + '</option>';
    }).join('');
    var f = this._adminEventFilters || {};
    var seenActions = {};
    var actions = (a.actions || []).filter(function(item) {
      if (!item.action || seenActions[item.action]) return false;
      seenActions[item.action] = true;
      return true;
    }).map(function(item) {
      var val = escAttr(item.action);
      return '<option value="' + val + '"' + (f.action === item.action ? ' selected' : '') + '>' + esc(App._adminActionLabel(item.action)) + '</option>';
    }).join('');
    return '<div class="admin-events-card">' +
      '<div class="admin-section-row"><div><div class="admin-section-title">行为时间线</div><p>查看账号在什么时间做了什么；不记录对话正文、密码或模型密钥</p></div>' +
      '<div class="admin-event-tools"><button class="admin-btn" onclick="App._adminExportEvents()">导出 CSV</button><button class="admin-btn" onclick="App._adminClearActivityFilters()">清空筛选</button></div></div>' +
      '<div class="admin-event-filters">' +
        '<div class="admin-event-search"><input id="adminEventQuery" value="' + escAttr(f.query || '') + '" placeholder="搜索账号、动作、股票或对象" onkeydown="if(event.key===\'Enter\') App._adminApplyActivityQuery(this.value)"><button class="admin-btn primary" onclick="App._adminApplyActivityQuery(document.getElementById(\'adminEventQuery\').value)">搜索</button></div>' +
        '<select onchange="App._adminSetActivityFilter(\'username\', this.value)"><option value="">全部账号</option>' + users + '</select>' +
        '<select onchange="App._adminSetActivityFilter(\'feature\', this.value)"><option value="">全部功能</option>' + features + '</select>' +
        '<select onchange="App._adminSetActivityFilter(\'action\', this.value)"><option value="">全部动作</option>' + actions + '</select>' +
        '<select onchange="App._adminSetActivityFilter(\'status\', this.value)"><option value="">全部状态</option><option value="success"' + (f.status === 'success' ? ' selected' : '') + '>成功</option><option value="failed"' + (f.status === 'failed' ? ' selected' : '') + '>失败</option></select>' +
      '</div>' +
      '<div id="adminEventTimeline">' + this._renderAdminEventRows() + '</div>' +
    '</div>';
  },

  _adminExportEvents: function() {
    var events = (((this._adminActivityEvents || {}).events) || []);
    if (!events.length) { this._toast && this._toast('暂无可导出的行为记录'); return; }
    function csvCell(value) { return '"' + String(value == null ? '' : value).replace(/"/g, '""') + '"'; }
    var rows = [['时间', '账号', '功能', '动作', '对象类型', '对象', '状态', '耗时ms', '请求ID']];
    events.forEach(function(ev) {
      rows.push([
        App._adminFormatDate(ev.created_at), ev.username || '匿名', App._adminFeatureLabel(ev.feature),
        App._adminActionLabel(ev.action), App._adminTargetTypeLabel(ev.target_type),
        ev.target_label || ev.target_id || '', ev.status, ev.duration_ms || 0, ev.request_id || ''
      ]);
    });
    var csv = '\ufeff' + rows.map(function(row){ return row.map(csvCell).join(','); }).join('\r\n');
    var url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
    var link = document.createElement('a');
    link.href = url;
    link.download = 'prism-activity-' + (this._adminAnalyticsRange || '7d') + '.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 0);
  },

  _renderAdminEventRows: function() {
    var payload = this._adminActivityEvents || {};
    var events = payload.events || [];
    var rows = events.map(function(ev) {
      var ok = ev.status === 'success';
      var target = ev.target_label || ev.target_id || '';
      var meta = ev.meta || {};
      var statusCode = meta.status_code ? 'HTTP ' + meta.status_code : '';
      return '<tr>' +
        '<td title="' + escAttr(App._adminFormatDate(ev.created_at)) + '">' + esc(App._adminFormatDate(ev.created_at)) + '</td>' +
        '<td><span class="admin-user-name">' + esc(ev.username || '匿名') + '</span></td>' +
        '<td><span class="admin-pill">' + esc(App._adminFeatureLabel(ev.feature)) + '</span></td>' +
        '<td>' + esc(App._adminActionLabel(ev.action)) + '<em>' + esc(ev.action || '') + '</em></td>' +
        '<td>' + (target ? '<span title="' + escAttr(target) + '">' + esc(target) + '</span><em>' + esc(App._adminTargetTypeLabel(ev.target_type)) + '</em>' : '-') + '</td>' +
        '<td><span class="admin-pill ' + (ok ? 'ok' : 'off') + '">' + esc(ok ? '成功' : '失败') + '</span></td>' +
        '<td>' + esc((ev.duration_ms || 0) + ' ms') + (statusCode ? '<em>' + esc(statusCode) + '</em>' : '') + '</td>' +
      '</tr>';
    }).join('');
    if (!rows) return '<div class="admin-empty small">暂无匹配的行为记录</div>';
    var total = Number(payload.total == null ? events.length : payload.total);
    var more = payload.has_more ? '<button class="admin-btn" onclick="App._adminLoadMoreEvents()">加载更多</button>' : '';
    return '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-event-table"><thead><tr><th>时间</th><th>账号</th><th>功能</th><th>动作</th><th>对象</th><th>状态</th><th>耗时</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="admin-event-foot"><span>已显示 ' + esc(String(events.length)) + ' / ' + esc(String(total)) + ' 条</span>' + more + '</div>';
  },

  _renderAdminTabs: function() {
    var tabs = [
      ['overview', '概览'], ['operations', '发布控制台'], ['features', '功能分析'], ['activity', '用户行为'],
      ['diagnostics', '异常诊断'], ['feedback', '反馈评审'], ['accounts', '账号管理'], ['health', '系统健康']
    ];
    return '<div class="admin-tabs" role="tablist">' + tabs.map(function(tab) {
      return '<button role="tab" aria-selected="' + (App._adminTab === tab[0] ? 'true' : 'false') + '" class="admin-tab ' + (App._adminTab === tab[0] ? 'active' : '') + '" onclick="App._adminSetTab(\'' + tab[0] + '\')">' + tab[1] + '</button>';
    }).join('') + '</div>';
  },

  _renderAdminRangeHeader: function(title, description) {
    var range = this._adminAnalyticsRange || '7d';
    var buttons = [['24h', '24小时'], ['7d', '7天'], ['30d', '30天'], ['90d', '90天']].map(function(item) {
      return '<button class="admin-range-btn ' + (range === item[0] ? 'active' : '') + '" onclick="App._adminSetRange(\'' + item[0] + '\')">' + item[1] + '</button>';
    }).join('');
    return '<div class="admin-section-row admin-tab-heading"><div><div class="admin-section-title">' + esc(title) + '</div><p>' + esc(description) + '</p></div><div class="admin-range">' + buttons + '</div></div>';
  },

  _renderAdminFeaturesTab: function() {
    return '<section class="admin-tab-section">' +
      this._renderAdminRangeHeader('功能分析', '比较功能活跃、热门研究对象、失败率与接口耗时') +
      this._renderAdminFeatureTable() +
      '<div class="admin-data-grid">' + this._renderAdminTopTargets() + this._renderAdminPerformance() + '</div>' +
      this._renderAdminErrors() +
    '</section>';
  },

  _renderAdminActivityTab: function() {
    return '<section class="admin-tab-section">' +
      this._renderAdminRangeHeader('用户行为', '按账号查看活跃天、使用会话、成功率和逐条操作轨迹') +
      this._renderAdminUserActivityTable() +
      this._renderAdminEventTimeline() +
    '</section>';
  },

  _renderAdminDiagnosticsTab: function() {
    var payload = this._adminDiagnostics || {};
    var stats = payload.stats || {};
    var events = payload.events || [];
    var cards = [
      ['待核对记录', events.length, '含历史记录，累计出现 ' + this._adminFmtNum(stats.occurrences || 0) + ' 次'],
      ['严重', stats.critical || 0, '需要优先处理'],
      ['错误', stats.error || 0, '按版本与复现结果判定影响'],
      ['警告', stats.warning || 0, '可能的退化或空白状态']
    ].map(function(item) {
      return '<div class="admin-stat-card"><span>' + esc(item[0]) + '</span><strong>' + esc(String(item[1])) + '</strong><em>' + esc(item[2]) + '</em></div>';
    }).join('');
    var rows = events.map(function(item) {
      var ctx = item.context || {};
      var contextText = Object.keys(ctx).map(function(key) { return key + ': ' + ctx[key]; }).join(' · ');
      var detail = [item.message || '', contextText, item.stack || ''].filter(Boolean).join('\n');
      return '<tr>' +
        '<td>' + esc(App._adminFormatDate(item.last_seen)) + '<em>首次 ' + esc(App._adminFormatDate(item.first_seen)) + '</em></td>' +
        '<td><span class="admin-pill ' + (item.severity === 'critical' || item.severity === 'error' ? 'off' : '') + '">' + esc(item.severity || 'error') + '</span><em>' + esc(item.kind || '') + '</em></td>' +
        '<td><b>' + esc(item.component || '-') + '</b><em>' + esc(item.page || '-') + '</em><em>' + esc(item.version || '版本未记录') + ' · ' + esc(({current_version:'当前版本',historical_version:'历史版本，待复测',unattributed:'版本未知，待核对'})[item.version_scope] || '待核对') + '</em></td>' +
        '<td><details class="admin-diagnostic-detail"><summary>' + esc(item.message || '异常') + '</summary><pre>' + esc(detail) + '</pre></details></td>' +
        '<td>' + esc(String(item.occurrences || 1)) + '</td>' +
        '<td><button class="admin-btn" onclick="App._adminResolveDiagnostic(' + Number(item.id) + ')">归档记录</button></td>' +
      '</tr>';
    }).join('');
    return '<section class="admin-tab-section">' +
      '<div class="admin-section-row admin-tab-heading"><div><div class="admin-section-title">异常诊断</div><p>区分当前、历史和版本未知记录。历史记录需复测；归档不代表修复完成。匿名 401/403 与未匹配路径请求在诊断包中单独分类。</p></div>' +
      '<button class="admin-btn primary" onclick="App._adminCopyDiagnosticBundle()">复制 Codex 诊断包</button></div>' +
      '<div class="admin-stat-grid">' + cards + '</div>' +
      (rows ? '<div class="admin-table-wrap admin-scroll-table"><table class="admin-table admin-diagnostic-table"><thead><tr><th>时间</th><th>级别</th><th>组件</th><th>异常</th><th>次数</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty">当前没有未处理异常</div>') +
    '</section>';
  },

  _adminResolveDiagnostic: async function(id) {
    var r = await this.api('/api/admin/diagnostics/' + encodeURIComponent(id) + '/resolve', {method:'POST'});
    if (!r.ok) { this._toast && this._toast('处理状态更新失败'); return; }
    var next = await this.api('/api/admin/diagnostics?status=open&limit=120');
    this._adminDiagnostics = next.ok ? await next.json() : {events:[], stats:{}};
    this._renderAdminUsers(this._adminCurrentUser || this.user);
  },

  _adminCopyDiagnosticBundle: async function() {
    var r = await this.api('/api/admin/diagnostics/bundle?limit=120');
    if (!r.ok) { this._toast && this._toast('诊断包生成失败'); return; }
    var text = JSON.stringify(await r.json(), null, 2);
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      var area = document.createElement('textarea');
      area.value = text; area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.appendChild(area); area.select(); document.execCommand('copy'); area.remove();
    }
    this._toast && this._toast('Codex 诊断包已复制');
  },

  _renderAdminAccounts: function(currentUser) {
    var users = this._adminUsers || [];
    var rows = users.map(function(u) {
      var disabled = !!u.disabled_at;
      var current = !!u.is_current || u.username === currentUser;
      var status = disabled ? '<span class="admin-pill off">禁用</span>' : '<span class="admin-pill ok">启用</span>';
      var admin = u.is_admin ? '<span class="admin-pill">Admin</span>' : '';
      var selfMark = current ? '<span class="admin-pill">当前</span>' : '';
      var toggleText = disabled ? '启用' : '禁用';
      return '<tr>' +
        '<td><div class="admin-user-main"><span class="admin-user-name">' + esc(u.username) + '</span>' + selfMark + admin + '</div></td>' +
        '<td>' + status + '</td>' +
        '<td>' + esc(String(u.active_sessions || 0)) + '</td>' +
        '<td>' + esc(String(u.conversation_count || 0)) + '</td>' +
        '<td title="' + escAttr(this._adminFormatDate(u.created_at)) + '">' + esc(this._adminFormatDate(u.created_at)) + '</td>' +
        '<td><div class="admin-actions">' +
          '<button class="admin-btn" onclick="App._adminResetPassword(\'' + escAttr(u.username) + '\')">重置</button>' +
          '<button class="admin-btn" ' + (current && !disabled ? 'disabled ' : '') + 'onclick="App._adminToggleUser(\'' + escAttr(u.username) + '\',' + (!disabled) + ')">' + toggleText + '</button>' +
          '<button class="admin-btn danger" ' + (current ? 'disabled ' : '') + 'onclick="App._adminDeleteUser(\'' + escAttr(u.username) + '\')">删除</button>' +
        '</div></td>' +
      '</tr>';
    }, this).join('');
    return '<section class="admin-tab-section"><div class="admin-section-row"><div><div class="admin-section-title">账号控制</div><p>创建入口位于左侧，仅管理员可重置、禁用或删除账号</p></div></div>' +
      (users.length ? '<div class="admin-table-wrap"><table class="admin-table"><thead><tr>' +
        '<th style="width:24%">账号</th><th style="width:10%">状态</th><th style="width:12%">会话</th><th style="width:12%">对话</th><th>创建时间</th><th style="width:230px;text-align:right">操作</th>' +
        '</tr></thead><tbody>' + rows + '</tbody></table></div>' : '<div class="admin-empty">暂无账号</div>') +
      '</section>';
  },

  _renderAdminUsers: function(currentUser) {
    var inner = document.getElementById('adminPanelInner');
    if (!inner) return;
    if (!this.isAdmin) { inner.textContent = '无管理权限'; return; }
    this._adminCurrentUser = currentUser || this.user;
    this._adminDisposeCharts();
    var content = '';
    if (this._adminTab === 'operations') content = this._renderOperationsPanel ? this._renderOperationsPanel() : '<div class="admin-empty">发布控制台未加载，请刷新页面。</div>';
    else if (this._adminTab === 'features') content = this._renderAdminFeaturesTab();
    else if (this._adminTab === 'activity') content = this._renderAdminActivityTab();
    else if (this._adminTab === 'diagnostics') content = this._renderAdminDiagnosticsTab();
    else if (this._adminTab === 'feedback') content = '<section class="admin-tab-section"><h3>反馈评审</h3><p>异常优先排查，建议每日集中评审；答复和验证结果对提交者可见。</p><button class="admin-btn primary" onclick="App._openFeedback();App._feedbackView(\'admin\')">打开反馈队列</button></section>';
    else if (this._adminTab === 'accounts') content = this._renderAdminAccounts(this._adminCurrentUser);
    else if (this._adminTab === 'health') content = this._renderAdminHealth();
    else { this._adminTab = 'overview'; content = this._renderAdminStats() + this._renderAdminAnalytics(); }
    inner.innerHTML = '<div class="admin-console">' +
      '<div class="admin-head"><div><h2>管理看板</h2><div class="admin-head-meta">运营、功能、用户行为、账号与系统状态</div></div>' +
      '<div class="admin-toolbar"><button class="admin-btn" onclick="App.openReleaseNotes()">更新日志</button><button class="admin-btn primary" onclick="'+(this._adminTab === 'operations' ? 'App._loadOperations()' : 'App._loadAdminUsers()')+'">刷新</button></div></div>' +
      this._renderAdminTabs() + content + '</div>';
    if (this._adminTab === 'overview') {
      var self = this;
      setTimeout(function(){ self._renderAdminCharts(); }, 0);
    }
  },

  _adminCreateUser: async function() {
    var userEl = document.getElementById('adminNewUser');
    var passEl = document.getElementById('adminNewPass');
    var username = (userEl && userEl.value || '').trim();
    var password = (passEl && passEl.value || '');
    if (!username || !password) { this._adminSetStatus('请输入用户名和密码'); return; }
    if (password.length < 8) { this._adminSetStatus('密码至少 8 位'); return; }
    var r = await this.api('/api/admin/users', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username: username, password: password})
    });
    if (!r.ok) {
      var err = await r.json().catch(function(){ return {}; });
      this._adminSetStatus(err.detail || '创建失败');
      return;
    }
    if (userEl) userEl.value = '';
    if (passEl) passEl.value = '';
    this._adminSetStatus('已创建 ' + username);
    if (this._toast) this._toast('账号已创建');
    this._loadAdminUsers();
  },

  _adminResetPassword: async function(username) {
    var password = prompt('输入 ' + username + ' 的新密码（至少 8 位）');
    if (password === null) return;
    if (password.length < 8) { this._toast && this._toast('密码至少 8 位'); return; }
    var r = await this.api('/api/admin/users/' + encodeURIComponent(username) + '/password', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: password})
    });
    if (!r.ok) { this._toast && this._toast('重置失败'); return; }
    this._toast && this._toast('密码已重置');
    this._loadAdminUsers();
  },

  _adminToggleUser: async function(username, disabled) {
    if (disabled && !confirm('禁用 ' + username + ' 并清除其登录会话？')) return;
    var r = await this.api('/api/admin/users/' + encodeURIComponent(username) + '/status', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({disabled: !!disabled})
    });
    if (!r.ok) { this._toast && this._toast('操作失败'); return; }
    this._toast && this._toast(disabled ? '账号已禁用' : '账号已启用');
    this._loadAdminUsers();
  },

  _adminDeleteUser: async function(username) {
    if (!confirm('删除账号 ' + username + '？历史分析记录会保留。')) return;
    var r = await this.api('/api/admin/users/' + encodeURIComponent(username), {method: 'DELETE'});
    if (!r.ok) { this._toast && this._toast('删除失败'); return; }
    this._toast && this._toast('账号已删除');
    this._loadAdminUsers();
  }
});
