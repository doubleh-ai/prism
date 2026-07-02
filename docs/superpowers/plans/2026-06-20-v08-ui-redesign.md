# Prism v0.8 UI 重设计 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新的「42px 图标栏」骨架上重做整个前端 —— 行情 K 线（样式1，红涨绿跌+均线+涨跌量，跟随主题）、对话消息（卡片式）、输入区（悬浮 Composer+工具抽屉），并把 `app.js`（1755 行）拆成 4 个模块。

**Architecture:** 三层 shell：`[图标栏 42px] + [per-panel 内容侧栏] + [主区(精简顶栏+内容)]`。`#appShell[data-mode]` 属性选择器驱动各板块显隐（沿用 v0.7）。`window.App` 全局对象保留，4 个 JS 文件各自往 `App` 上挂方法，HTML 内联 `onclick` 不动。分 3 阶段交付，每阶段可独立部署验收。

**Tech Stack:** 原生 HTML/CSS/JS（无构建步骤）、ECharts 5.6.1（本地 `static/lib/echarts.min.js`）、FastAPI StaticFiles 服务前端、localStorage 持久化（主题/自选/宽窄）。

---

## ⚠️ 验证与提交约定（覆盖 skill 默认）

1. **不产生 git commit** —— 用户铁律：全部实现完、人工验收通过后才提交。本计划每个任务结尾是「验证检查点」而非 commit。
2. **无 JS 单测框架** —— 前端验证手段：
   - 语法检查：`node -c static/js/<file>.js`
   - 结构断言：`grep` 确认产物含预期 class/元素
   - 手动浏览器检查：每任务列出"看什么"
   - 部署 smoke：rsync + `?v` 破缓存 + curl 验证静态资源 200
3. **后端无改动** —— v0.8 纯前端。每阶段部署后跑一次 `pytest -q` 作为 no-regression smoke（应全绿，因为后端没动）。
4. **`?v` 版本号**：阶段1 → `?v=24`，阶段2 → `?v=25`，阶段3 → `?v=26`。
5. **前置依赖**：v0.7.1 baostock 并发锁修复（已编码于 `server/market/baostock_provider.py`）应在阶段1前部署，保证行情数据稳定。部署脚本 `/tmp/prism-deploy-v071-baostock-lock.sh` 已就绪。

---

## File Structure（改动地图）

| 文件 | 阶段 | 职责 |
|---|---|---|
| `templates/app.html` | 1/2/3 | 增图标栏、改顶栏、改 composer、改 script 引用顺序 |
| `static/css/app.css` | 1/2 | 图标栏、K线容器底色、消息卡片、composer 样式 |
| `static/js/app.js` | 1/2 | switchMode、renderKlineChart、消息卡片渲染、composer/抽屉 |
| `static/js/app.util.js` | 3 | 新建：esc/格式化/glossary/报告查看器 |
| `static/js/app.core.js` | 3 | 新建：init/state/switchMode/theme/api |
| `static/js/app.chat.js` | 3 | 新建：流式/消息渲染/composer/技能 |
| `static/js/app.market.js` | 3 | 新建：报价卡/K线/自选/搜索 |
| `/tmp/prism-deploy-v08-pN.sh` | 1/2/3 | 每阶段部署脚本（rsync+chown，阶段1含图表无需重启） |

---

# 阶段 1 — 骨架 + K 线重做

## Task 1.1: 图标栏 markup + CSS

**Files:**
- Modify: `templates/app.html`（在 `.sidebar-head` 后、`.nav-tabs` 处替换）
- Modify: `static/css/app.css`（替换 `.nav-tabs`/`.nav-tab` 块，约 496-498 行）

- [ ] **Step 1: 改 app.html —— 用图标栏替换 nav-tabs**

把 `templates/app.html` 第 13-16 行的 `<div class="nav-tabs">...</div>` 整块替换为：

```html
    <div class="icon-rail" id="iconRail">
      <button class="icon-rail-btn active" data-mode="chat" title="对话" onclick="App.switchMode('chat')">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M4 5h16v10H8l-4 4V5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>
      </button>
      <button class="icon-rail-btn" data-mode="market" title="行情" onclick="App.switchMode('market')">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none"><path d="M4 19V5m0 14h16M8 16l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
    </div>
```

- [ ] **Step 2: 改 app.css —— 用图标栏样式替换 nav-tabs 块**

把 `.nav-tabs{...}` / `.nav-tab{...}` / `.nav-tab:hover{...}` / `.nav-tab.active{...}`（约 496-499 行，`.nav-tabs` 到其 active 规则）整块替换为：

```css
.icon-rail{display:flex;flex-direction:column;align-items:center;gap:4px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}
.icon-rail-btn{width:34px;height:34px;display:flex;align-items:center;justify-content:center;background:none;border:none;color:var(--side-text);cursor:pointer;border-radius:var(--r);transition:all .15s}
.icon-rail-btn:hover{color:#fff;background:var(--side-hover)}
.icon-rail-btn.active{color:#fff;background:var(--accent)}
```

- [ ] **Step 3: 结构断言**

Run: `grep -c 'class="icon-rail"' templates/app.html && grep -c '.icon-rail-btn' static/css/app.css`
Expected: 两个都 ≥1。

- [ ] **Step 4: 验证检查点（浏览器本地预览或部署后）**

启动后点图标栏两个图标：当前图标变 accent 高亮、侧栏内容切换。顶栏原"行情"按钮本任务先保留（Task 1.4 删）。

---

## Task 1.2: switchMode 改为图标栏驱动

**Files:**
- Modify: `static/js/app.js`（`switchMode` 方法 + `init` 里的 nav-tab 绑定）

- [ ] **Step 1: 定位现有 switchMode**

Run: `grep -n 'switchMode' static/js/app.js`
确认当前实现（v0.7 版设 `#appShell[data-mode]` + 切 `.nav-tab.active` + 启停 watchlist 刷新 + 切 `btn-market.active`）。

- [ ] **Step 2: 改 switchMode —— 图标栏高亮替代 nav-tab**

把 `switchMode` 内切换 nav-tab active 的部分改为切图标栏。将方法体中所有 `.nav-tab` 相关 DOM 操作替换为图标栏：

```javascript
  switchMode: function(mode) {
    this._mode = mode;
    var shell = document.getElementById('appShell');
    if (shell) shell.setAttribute('data-mode', mode);
    // 图标栏高亮
    document.querySelectorAll('.icon-rail-btn').forEach(function(b){
      b.classList.toggle('active', b.getAttribute('data-mode') === mode);
    });
    // 侧栏内容切换（沿用 v0.7 显隐）
    var sm = document.getElementById('sidebarMarket');
    if (sm) sm.style.display = (mode === 'market') ? '' : 'none';
    document.querySelectorAll('.new-chat-btn, .bulk-bar, .conv-list').forEach(function(el){
      el.style.display = (mode === 'chat') ? '' : 'none';
    });
    // 主区切换
    var mp = document.getElementById('marketPanel');
    if (mp) mp.style.display = (mode === 'market') ? '' : 'none';
    document.querySelectorAll('.msgs-wrap, .input-zone').forEach(function(el){
      el.style.display = (mode === 'chat') ? '' : 'none';
    });
    // 行情自选刷新定时器
    if (mode === 'market') { this._startWatchlistRefresh(); }
    else { this._stopWatchlistRefresh(); }
    // 顶栏行情按钮高亮（若仍存在）
    var bm = document.getElementById('btn-market');
    if (bm) bm.classList.toggle('active', mode === 'market');
  },
```

> 注：保留与 v0.7 等价的显隐逻辑；若原 switchMode 还做了别的（如 K 线 resize），原样保留那部分，只替换 nav-tab/btn 高亮两行。

- [ ] **Step 3: 移除 init 里残留的 nav-tab 绑定（若有）**

Run: `grep -n 'nav-tab\|navTabs' static/js/app.js`
若 `init` 中有 `document.querySelectorAll('.nav-tab')` 之类的绑定，删除（图标栏用内联 onclick，不需要 JS 绑定）。

- [ ] **Step 4: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出（=语法 OK）。

- [ ] **Step 5: 验证检查点**

点图标栏 💬/📈：`#appShell[data-mode]` 正确切换、对应图标高亮、侧栏与主区内容切换、切到 market 时自选刷新定时器启动。

---

## Task 1.3: 行情内容侧栏 300px

**Files:**
- Modify: `static/css/app.css`（`.sidebar` 宽度变量化 + market 模式加宽）

- [ ] **Step 1: 让 sidebar 宽度随模式变化**

在 `static/css/app.css` 的 `.sidebar{...}`（约 53 行）下方新增一条 market 模式加宽规则：

```css
#appShell[data-mode="market"] .sidebar{width:300px}
```

（默认 `--sidebar-w:236px` 不变；只在 market 模式覆写为 300px。`.app-shell.collapsed .sidebar{width:56px}` 优先级更高，折叠时仍收起。）

- [ ] **Step 2: 结构断言**

Run: `grep -c 'data-mode="market"\] .sidebar{width:300px' static/css/app.css`
Expected: 1。

- [ ] **Step 3: 验证检查点**

切到行情模式：侧栏宽 300px，搜索框 + 自选列表（代码/名称/价/涨跌）不挤；切回对话回到 236px；折叠侧栏仍能收到 56px。

---

## Task 1.4: 顶栏精简（移除行情按钮）

**Files:**
- Modify: `templates/app.html`（topbar 内 `#btn-market` 按钮，约 45 行）

- [ ] **Step 1: 删除顶栏行情按钮**

删除 `templates/app.html` 中这一行（约 45 行）：

```html
      <button class="tb" id="btn-market" onclick="App.switchMode('market')" title="切换到行情面板">行情</button>
```

- [ ] **Step 2: 清理 app.js 中对 btn-market 的引用**

Run: `grep -n 'btn-market' static/js/app.js`
把 Task 1.2 switchMode 末尾的 `var bm = document.getElementById('btn-market'); if (bm) ...` 两行删除（按钮已不存在）。

- [ ] **Step 3: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 4: 验证检查点**

顶栏不再有"行情"按钮；切换走图标栏。模型选择器本阶段仍留顶栏（阶段2 挪进 composer）。

---

## Task 1.5: K 线重做为样式1（亮色配色 + 均线 + 涨跌量 + 十字线）

**Files:**
- Modify: `static/js/app.js`（`renderKlineChart` 方法，约 633 行起）
- Modify: `static/css/app.css`（K 线容器底色）

- [ ] **Step 1: 给 K 线容器加护眼底色**

在 `static/css/app.css` 的 `#marketKlineChart{height:360px}` 附近新增（亮/暗各一条）：

```css
.market-kline-wrap{background:var(--bg);border-radius:10px;padding:6px 4px 0}
.light .market-kline-wrap{background:#e4e1d8}
.dark .market-kline-wrap{background:#101114}
```

> 图表 echarts `backgroundColor:'transparent'`，故显示容器底色。

- [ ] **Step 2: 重写 renderKlineChart option**

把 `static/js/app.js` 的 `renderKlineChart` 方法（约 633-680 行）整体替换为下面的样式1 版（亮色配色）。MA 计算、dataZoom、十字线、涨跌着色成交量齐全：

```javascript
  renderKlineChart: function(container, kline) {
    if (typeof echarts === 'undefined') { container.innerHTML = '图表库未加载'; return; }
    if (this._klineChart) { try { this._klineChart.dispose(); } catch (e) {} this._klineChart = null; }
    var chart = echarts.init(container, null, {renderer: 'canvas'});
    this._klineChart = chart;
    var dates = [], ohlc = [], vols = [];
    for (var i = 0; i < kline.length; i++) {
      var k = kline[i];
      var ts = k.ts || k.timestamp || 0;
      var d = new Date(ts * 1000);
      var ds = d.getFullYear() + '-' + ('0' + (d.getMonth() + 1)).slice(-2) + '-' + ('0' + d.getDate()).slice(-2);
      dates.push(ds);
      ohlc.push([+k.open, +k.close, +k.low, +k.high]);
      vols.push(+k.volume || 0);
    }
    // 均线
    function ma(arr, n) {
      var r = [];
      for (var i = 0; i < arr.length; i++) {
        if (i < n - 1) { r.push(null); continue; }
        var s = 0; for (var j = 0; j < n; j++) s += +arr[i - j];
        r.push(+(s / n).toFixed(2));
      }
      return r;
    }
    var closes = kline.map(function(k){return +k.close;});
    var ma5 = ma(closes, 5), ma20 = ma(closes, 20);
    var P = this._chartPalette();   // {up,dn,ma1,ma2,grid,axl,txt,tipBg,tipBd,tipTxt,zoomBd,zoomFill}
    chart.setOption({
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {trigger: 'axis', axisPointer: {type: 'cross', lineStyle: {color: P.cross, type: 'dashed'}},
        backgroundColor: P.tipBg, borderColor: P.tipBd, textStyle: {color: P.tipTxt, fontSize: 11}},
      legend: {data: ['日K', 'MA5', 'MA20'], top: 4, textStyle: {color: P.txt, fontSize: 10}, itemWidth: 12, itemHeight: 8},
      grid: [{left: 54, right: 18, top: 28, height: '56%'}, {left: 54, right: 18, top: '74%', height: '15%'}],
      xAxis: [
        {type: 'category', data: dates, boundaryGap: false, axisLine: {lineStyle: {color: P.axl}}, splitLine: {show: false}, axisLabel: {color: P.txt, fontSize: 9}},
        {type: 'category', gridIndex: 1, data: dates, boundaryGap: false, axisLine: {show: false}, axisLabel: {show: false}, splitLine: {show: false}}
      ],
      yAxis: [
        {scale: true, splitLine: {lineStyle: {color: P.grid}}, axisLabel: {color: P.txt, fontSize: 9}},
        {scale: true, gridIndex: 1, splitNumber: 2, splitLine: {show: false}, axisLabel: {show: false}}
      ],
      dataZoom: [
        {type: 'inside', xAxisIndex: [0, 1], start: 35, end: 100},
        {show: true, type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, start: 35, end: 100, borderColor: P.zoomBd, fillerColor: P.zoomFill}
      ],
      series: [
        {name: '日K', type: 'candlestick', data: ohlc, itemStyle: {color: P.up, color0: P.dn, borderColor: P.up, borderColor0: P.dn}},
        {name: 'MA5', type: 'line', data: ma5, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma1}, z: 5},
        {name: 'MA20', type: 'line', data: ma20, smooth: true, showSymbol: false, lineStyle: {width: 1.3, color: P.ma2}, z: 5},
        {name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
          data: vols.map(function(v, i) { return {value: v, itemStyle: {color: closes[i] >= +kline[i].open ? P.up : P.dn}}; })}
      ]
    });
    if (!this._klineResizeBound) {
      var self = this;
      window.addEventListener('resize', function() { if (self._klineChart) self._klineChart.resize(); });
      this._klineResizeBound = true;
    }
  },
```

- [ ] **Step 3: 新增 _chartPalette() 助手（亮/暗）**

在 `renderKlineChart` 上方新增：

```javascript
  _chartPalette: function() {
    var dark = document.documentElement.classList.contains('dark');
    return dark
      ? {up:'#ef5350', dn:'#26a69a', ma1:'#fbbf24', ma2:'#a78bfa', grid:'#202228', axl:'#282a32', txt:'#706c64', cross:'#706c64', tipBg:'#18191e', tipBd:'#282a32', tipTxt:'#d8d5cd', zoomBd:'#282a32', zoomFill:'#202228'}
      : {up:'#d6332f', dn:'#1f9d83', ma1:'#cf8a3a', ma2:'#7c5fd0', grid:'#d2cec3', axl:'#c8c2b4', txt:'#8a8478', cross:'#9d9890', tipBg:'#ffffff', tipBd:'#e2e0da', tipTxt:'#1c1a16', zoomBd:'#d5d2cb', zoomFill:'#ebe9e4'};
  },
```

- [ ] **Step 4: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 5: 验证检查点（部署后）**

K 线：红涨绿跌、MA5(橙)/MA20(紫) 均线、成交量按涨跌着色、十字虚线、tooltip、dataZoom 可拖。亮色底为暖灰 `#e4e1d8`。

---

## Task 1.6: K 线跟随主题切换重绘

**Files:**
- Modify: `static/js/app.js`（`toggleTheme` 方法，约 98 行）

- [ ] **Step 1: toggleTheme 后触发 K 线重绘**

定位 `toggleTheme`（约 98 行）。在方法末尾（`localStorage.setItem('cl-theme', n);` 之后）追加：

```javascript
    // 主题切换后重绘当前 K 线（换配色）
    if (this._mode === 'market' && this._currentTicker) {
      this._renderKline(this._currentTicker, this._currentPeriod || '1d');
    }
```

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 3: 验证检查点**

行情模式下点主题按钮：K 线立即换亮/暗配色（蜡烛、均线、网格、底色全部切换）。

---

## Task 1.7: 阶段1 部署 + smoke

**Files:**
- Create: `/tmp/prism-deploy-v08-p1.sh`

- [ ] **Step 1: bump ?v=23 → 24**

`templates/app.html` 中 3 处 `?v=23`（css + 3 个 script，共 4 处含 css）改为 `?v=24`。

- [ ] **Step 2: 写部署脚本**

写 `/tmp/prism-deploy-v08-p1.sh`（纯前端，无需重启服务）：

```bash
#!/bin/bash
set -e
SRC=/home/develop/projects/prism
DST=/home/ubuntu/stock-analyzer
echo "==> rsync 前端 → 生产"
sudo rsync -av "$SRC/templates/app.html" "$DST/templates/app.html"
sudo rsync -av "$SRC/static/css/app.css"  "$DST/static/css/app.css"
sudo rsync -av "$SRC/static/js/app.js"    "$DST/static/js/app.js"
sudo chown ubuntu:ubuntu "$DST/templates/app.html" "$DST/static/css/app.css" "$DST/static/js/app.js"
echo "==> smoke"
for f in css/app.css js/app.js; do
  echo -n "  $f?v=24: "; curl -sS -o /dev/null -w "%{http_code}\n" "http://localhost:8088/static/$f?v=24"
done
echo -n "  icon-rail in page: "; curl -sS http://localhost:8088/ | grep -o icon-rail | head -1 || true
echo "=== 阶段1 完成（刷新浏览器）==="
```

- [ ] **Step 3: 后端 no-regression smoke**

Run: `cd /home/develop/projects/prism && /home/ubuntu/stock-venv/bin/python -m pytest tests/ -q 2>&1 | tail -3`
Expected: 全绿（后端未动）。

- [ ] **Step 4: 人工验收（用户）**

浏览器硬刷新后核对：图标栏切换、行情侧栏 300px、顶栏无行情按钮、K 线样式1（红涨绿跌+均线+涨跌量+十字线+护眼底）、主题切换 K 线重绘。**通过后再进阶段2。**

---

# 阶段 2 — 对话卡片化 + Composer

## Task 2.1: 消息卡片化 CSS

**Files:**
- Modify: `static/css/app.css`（替换 `.msg`/`.msg-inner`/`.msg-byline` 相关规则，约 112-127 行；新增卡片样式）

- [ ] **Step 1: 移除全宽条纹样式，新增卡片样式**

把 `.msg.assistant{border-bottom...}` / `.msg.user{background:var(--bg2)...}` / `.msg-inner` / `.msg-byline` 那一组（约 119-127 行）替换为：

```css
.msg{padding:14px 0;animation:msgIn .45s ease both}
.msg-inner{max-width:var(--msg-w);margin:0 auto;padding:0 28px}
/* 用户消息：右对齐气泡 */
.msg.user .msg-inner{display:flex;justify-content:flex-end}
.msg.user .u-bubble{background:var(--accent);color:#fff;padding:9px 15px;border-radius:16px 16px 4px 16px;font-size:14.5px;line-height:1.7;max-width:75%}
/* AI 消息：卡片 */
.msg-card{background:#fff;border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.dark .msg-card{background:var(--bg2)}
.msg-card__hd{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--bg2)}
.dark .msg-card__hd{background:var(--bg3)}
.msg-card__who{font-family:var(--mono);font-size:10px;letter-spacing:.6px;text-transform:uppercase;color:var(--text3)}
.msg-card__acts{display:flex;gap:12px;color:var(--text3);font-size:11px}
.msg-card__acts span{cursor:pointer}
.msg-card__acts span:hover{color:var(--accent)}
.msg-card .bubble{padding:14px 16px}
```

> 注：`.bubble` 正文排版规则（196-209 行）保持不变，卡片只包裹它。

- [ ] **Step 2: 结构断言**

Run: `grep -c '.msg-card__hd' static/css/app.css`
Expected: ≥1。

- [ ] **Step 3: 验证检查点**

本任务只改 CSS；消息暂时仍用旧结构渲染，可能暂时错位——下一任务改 JS 渲染后即正常。

---

## Task 2.2: 消息渲染改为卡片（AI）/ 气泡（用户）

**Files:**
- Modify: `static/js/app.js`（`_renderMsg` 及用户消息渲染，约 927 行起）

- [ ] **Step 1: 定位消息 DOM 构造处**

Run: `grep -n 'msg-inner\|msg-byline\|class="msg' static/js/app.js`
找到构造 `.msg` / `.msg-inner` / `.bubble` 的位置（通常在 `_renderMsg` 或 `appendMsg`/`send` 流式逻辑里）。

- [ ] **Step 2: AI 消息包卡片头，用户消息包气泡**

在构造 AI 消息 DOM 处，把原本 `<div class="msg-inner"><div class="msg-byline">…</div><div class="bubble">…</div></div>` 改为：

```javascript
// AI 卡片
'<div class="msg-inner">' +
  '<div class="msg-card">' +
    '<div class="msg-card__hd">' +
      '<span class="msg-card__who">PRISM · ' + esc(modelLabel || '分析') + '</span>' +
      '<span class="msg-card__acts">' +
        '<span onclick="App._copyMsg(this)">复制</span>' +
        '<span onclick="App._regenMsg(this)">重生成</span>' +
        '<span onclick="App._exportMsg(this)">导出报告</span>' +
      '</span>' +
    '</div>' +
    '<div class="bubble">' + /* 原 rendered HTML */ + '</div>' +
  '</div>' +
'</div>'
```

用户消息 DOM 改为：

```javascript
// 用户气泡
'<div class="msg user"><div class="msg-inner"><div class="u-bubble">' + esc(userText) + '</div></div></div>'
```

> 关键：保留流式更新逻辑——流式时仍往 `.bubble` 内增量写 HTML；卡片头一次性写好。若原代码用 `innerHTML` 整块重建，确保流式只替换 `.bubble` 子节点而非整卡。

- [ ] **Step 3: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 4: 验证检查点**

发一条消息：用户消息为右对齐 accent 气泡；AI 回复为卡片（头部 PRISM·分析 + 复制/重生成/导出，正文带左右圆角边框）。流式增量只更新卡片正文，头部不闪。

---

## Task 2.3: 卡片操作 复制 / 重生成 / 导出报告

**Files:**
- Modify: `static/js/app.js`（新增 3 个方法）

- [ ] **Step 1: 新增三个 handler**

在 `app.js` 合适位置（如 `_renderMsg` 附近）新增：

```javascript
  _copyMsg: function(el) {
    var bubble = el.closest('.msg-card').querySelector('.bubble');
    if (!bubble) return;
    var txt = bubble.innerText;
    navigator.clipboard.writeText(txt).then(function(){
      var o = el.textContent; el.textContent = '已复制'; setTimeout(function(){ el.textContent = o; }, 1200);
    });
  },
  _regenMsg: function(el) {
    // 取该卡片所属用户消息文本，重新发送
    var card = el.closest('.msg');
    var prev = card && card.previousElementSibling;
    var uText = prev && prev.querySelector('.u-bubble');
    if (uText) { document.getElementById('input').value = uText.innerText; this.send(); }
  },
  _exportMsg: function(el) {
    // 复用现有报告导出：若有报告 ref 则打开，否则提示
    var card = el.closest('.msg');
    if (typeof this.openReports === 'function' && this._lastReports && this._lastReports.length) {
      this.openReports();
    } else {
      var o = el.textContent; el.textContent = '无报告'; setTimeout(function(){ el.textContent = o; }, 1200);
    }
  },
```

> 注：`_regenMsg` 依赖用户消息是 AI 卡片的上一兄弟节点（Task 2.2 结构保证）。若项目已有重生成/导出 API，优先接现有接口。

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 3: 验证检查点**

点「复制」→ 剪贴板含正文，按钮临时变"已复制"；点「重生成」→ 输入框填入上一条用户消息并发送；点「导出报告」→ 有报告则开报告列表，无则提示。

---

## Task 2.4: 悬浮 Composer markup + CSS

**Files:**
- Modify: `templates/app.html`（`.input-zone` 内，约 65-79 行）
- Modify: `static/css/app.css`（`.input-zone`/`.skills-bar`，约 248、274 行）

- [ ] **Step 1: 改 app.html —— composer 卡片结构**

把 `.input-zone` 内的 `.skills-bar` + `.input-wrap` + `.input-hint`（约 66-78 行）替换为：

```html
      <div class="composer" id="composer">
        <div class="composer-chips" id="composerChips"></div>
        <textarea id="input" rows="1" placeholder="分析一下液冷供应链被低估的上游标的…"></textarea>
        <div class="composer-bar">
          <button class="cmp-btn" id="skillDrawerBtn" onclick="App._toggleSkillDrawer()">🛠 工具</button>
          <select id="modelSelect" onchange="App.setModel(this.value)"></select>
          <button id="sendBtn" class="cmp-send" onclick="App.send()">➤ 发送</button>
        </div>
      </div>
      <div class="input-hint">ENTER 发送 · SHIFT+ENTER 换行 · 点 🛠 增删工具</div>
```

> 模型 `<select id="modelSelect">` 从顶栏移到这里（顶栏的删除，见 Step 3）。

- [ ] **Step 2: 改 app.css —— composer 样式**

把 `.input-zone`（约 274 行）的子内容规则调整，并新增：

```css
.input-zone{max-width:var(--msg-w);margin:0 auto;width:100%;padding:10px 28px 20px;background:var(--bg);position:relative;z-index:5}
.composer{background:#fff;border:1px solid var(--border);border-radius:16px;box-shadow:0 2px 14px rgba(0,0,0,.06);padding:10px 12px}
.dark .composer{background:var(--bg2)}
.composer-chips{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:7px}
.composer-chips:empty{display:none}
.composer-chips .chip{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:11px;background:var(--accent-bg);color:var(--accent);border:1px solid #cfdcea;font-size:11.5px}
.composer-chips .chip .x{opacity:.5;cursor:pointer}
.composer textarea{width:100%;border:none;background:transparent;resize:none;font-family:inherit;font-size:14.5px;color:var(--text);outline:none;line-height:1.6;max-height:200px}
.composer textarea::placeholder{color:var(--text3)}
.composer-bar{display:flex;align-items:center;gap:8px;margin-top:8px}
.cmp-btn{display:inline-flex;align-items:center;gap:5px;padding:6px 11px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:var(--text2);font-size:12.5px;cursor:pointer}
.cmp-btn:hover{background:var(--bg3)}
.composer-bar select{font-size:12px;padding:5px 10px;background:var(--bg2);border:1px solid var(--border);color:var(--text2);border-radius:8px;cursor:pointer}
.cmp-send{margin-left:auto;background:var(--accent);color:#fff;border:none;padding:7px 14px;border-radius:8px;font-size:13px;cursor:pointer}
.cmp-send:disabled{background:var(--bg4);color:var(--text3)}
```

并删除旧 `.skills-bar` / `.skills-bar-label` / `.skill-tag-block`（约 248-249、275 行）相关规则（已由 composer-chips 替代）。

- [ ] **Step 3: 顶栏删除模型选择**

`templates/app.html` 顶栏删除 `<select id="modelSelect" ...>`（约 42 行）。模型 select 已挪进 composer（Step 1）。`loadModels()` 仍按 id 填充，无需改。

- [ ] **Step 4: 语法/结构检查**

Run: `node -c static/js/app.js && grep -c 'id="composer"' templates/app.html`
Expected: 无语法错 + ≥1。

- [ ] **Step 5: 验证检查点**

输入区为浮起圆角 composer；模型选择器在 composer 内；顶栏无模型框。本任务技能 chip 行暂空（下一任务接）。

---

## Task 2.5: Composer 工具抽屉 + 已选工具 chip

**Files:**
- Modify: `static/js/app.js`（`renderSkillsBar` → 改为渲染抽屉 + chips）

- [ ] **Step 1: 定位现有技能逻辑**

Run: `grep -n 'renderSkillsBar\|_activeSkills\|skillsBar' static/js/app.js`
确认 `init` 调 `renderSkillsBar()` 填 `#skillsBar`（已不存在），需改向。

- [ ] **Step 2: 新增抽屉 toggle + chip 渲染**

把 `renderSkillsBar` 改名为 `_renderSkillDrawer`，渲染到动态创建的抽屉；新增 chips 渲染。在 `app.js` 新增/替换：

```javascript
  _toggleSkillDrawer: function() {
    var d = document.getElementById('skillDrawer');
    if (!d) {
      d = document.createElement('div');
      d.id = 'skillDrawer';
      d.className = 'skill-drawer';
      document.body.appendChild(d);
    }
    if (d.style.display === 'block') { d.style.display = 'none'; return; }
    d.innerHTML = '<div class="skill-drawer-hd">工具</div>' +
      this.skills.map(function(s){
        var on = self._activeSkills.has(s.id);
        return '<button class="skill-drawer-item' + (on ? ' on' : '') + '" onclick="App._toggleSkill(\'' + escAttr(s.id) + '\')">' + esc(s.name) + '</button>';
      }).join('');
    d.style.display = 'block';
  },
  _toggleSkill: function(id) {
    if (id === 'cn') return; // 强制中文不可关
    if (this._activeSkills.has(id)) this._activeSkills.delete(id);
    else this._activeSkills.add(id);
    this._toggleSkillDrawer(); // 关
    this._toggleSkillDrawer(); // 用新状态重开
    this._renderComposerChips();
  },
  _renderComposerChips: function() {
    var box = document.getElementById('composerChips');
    if (!box) return;
    var self = this;
    box.innerHTML = this.skills.filter(function(s){ return self._activeSkills.has(s.id) && s.id !== 'cn'; })
      .map(function(s){ return '<span class="chip">' + esc(s.name) + '<span class="x" onclick="App._toggleSkill(\'' + escAttr(s.id) + '\')">×</span></span>'; }).join('');
  },
```

并把 `init` 里的 `this.renderSkillsBar()` 调用改为 `this._renderComposerChips()`（技能加载完即渲染初始 chip，如 'cn' 默认不显示 chip 但生效）。

> `var self = this;` 需在 `_toggleSkillDrawer` 顶部声明（若方法内用了 self）。抽屉 CSS 见 Step 3。

- [ ] **Step 3: 抽屉 CSS**

`static/css/app.css` 新增：

```css
.skill-drawer{position:fixed;display:none;background:var(--bg2);border:1px solid var(--border);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.12);padding:8px;z-index:50;min-width:160px;bottom:90px;left:50%;transform:translateX(-50%)}
.skill-drawer-hd{font-size:10px;color:var(--text3);font-family:var(--mono);letter-spacing:.5px;text-transform:uppercase;padding:4px 8px}
.skill-drawer-item{display:block;width:100%;text-align:left;background:none;border:none;color:var(--text2);font-family:inherit;font-size:13px;padding:7px 10px;border-radius:6px;cursor:pointer}
.skill-drawer-item:hover{background:var(--bg3)}
.skill-drawer-item.on{color:var(--accent);font-weight:600}
```

- [ ] **Step 4: 语法检查**

Run: `node -c static/js/app.js`
Expected: 无输出。

- [ ] **Step 5: 验证检查点**

点 🛠 工具 → 弹抽屉列出技能，已选高亮；点技能增删 → 抽屉关闭重开 + composer 顶部 chip 同步增删；chip 的 × 也能移除。发送时 `_activeSkills` 正确传入请求（沿用现有 send 逻辑）。

---

## Task 2.6: 阶段2 部署 + smoke

**Files:**
- Create: `/tmp/prism-deploy-v08-p2.sh`

- [ ] **Step 1: bump ?v=24 → 25**

`templates/app.html` 4 处 `?v=24` → `?v=25`。

- [ ] **Step 2: 写部署脚本**（同阶段1 模板，rsync app.html/app.css/app.js + chown + curl smoke，改 `?v=25`）

- [ ] **Step 3: 后端 no-regression smoke**

Run: `cd /home/develop/projects/prism && /home/ubuntu/stock-venv/bin/python -m pytest tests/ -q 2>&1 | tail -3`
Expected: 全绿。

- [ ] **Step 4: 人工验收（用户）**

消息卡片化（头+操作）、用户气泡、悬浮 composer、工具抽屉+chip、模型在 composer 内。**通过后再进阶段3。**

---

# 阶段 3 — 代码拆分（app.js → 4 模块）

> 原则：纯搬迁，**零行为变更**。每文件挂 `window.App`，HTML 加载顺序 util→core→chat→market。逐文件迁、每文件迁完跑一遍全交互回归。

## Task 3.1: 建 app.util.js

**Files:**
- Create: `static/js/app.util.js`

- [ ] **Step 1: 新建文件骨架**

```javascript
/* app.util.js — 纯工具：转义、格式化、glossary、报告查看器。挂 window.App。 */
(function(App){
  'use strict';
  // 从 app.js 搬迁以下方法（保持函数体原样）：
  // esc, escAttr, _fmtVolume（若属通用）, _getCookie, glossify 相关, 报告查看器(openReportViewer/closeReportViewer/openReportInNewTab 等)
})(window.App);
```

- [ ] **Step 2: 搬迁工具方法**

把 `app.js` 中纯工具方法（`esc`、`escAttr`、`_getCookie`、glossary/术语解释 `_glossifyTerms`、报告 iframe 查看 `openReportViewer`/`closeReportViewer`/`openReportInNewTab` 等）的原函数体剪切到此文件的 IIFE 内。`app.js` 中删除这些定义。

- [ ] **Step 3: 语法检查**

Run: `node -c static/js/app.util.js && node -c static/js/app.js`
Expected: 均无输出。

- [ ] **Step 4: 验证检查点（暂不接入 HTML）**

此时 `app.js` 仍含其余方法；util 已独立。暂不改 HTML 引用（避免中途破坏）。本任务只确认两文件各自语法 OK。

---

## Task 3.2: 建 app.core.js

**Files:**
- Create: `static/js/app.core.js`

- [ ] **Step 1: 新建并搬迁核心方法**

```javascript
/* app.core.js — init/state/switchMode/theme/api/bootstrap。挂 window.App。 */
(function(App){
  'use strict';
  // 搬迁：App 对象声明(若在此初始化)、state 字段、init、switchMode、toggleTheme、toggleWidth、
  // api()、loadModels、_chartPalette、welcome/quickAsk、logout、newConv 等「核心/壳」方法
})(window.App);
```

> 注意：`App` 对象字面量的创建（`window.App = { ... }`）放在 core 文件顶部；其余三文件用 `(function(App){...})(window.App)` 往上挂方法。若现 app.js 是一个大字面量，需改为「core 建对象 + 各文件扩展」结构。

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/app.core.js`
Expected: 无输出。

- [ ] **Step 3: 验证检查点**

未接入 HTML 前，仅确认 core 文件语法 + `App` 对象创建语句存在：`grep -c 'window.App' static/js/app.core.js` ≥1。

---

## Task 3.3: 建 app.chat.js

**Files:**
- Create: `static/js/app.chat.js`

- [ ] **Step 1: 搬迁对话方法**

```javascript
/* app.chat.js — 流式/消息渲染卡片/composer/技能/自动补全。挂 window.App。 */
(function(App){
  'use strict';
  // 搬迁：send、_renderMsg、streamChat/流式接收、_copyMsg/_regenMsg/_exportMsg、
  // _toggleSkillDrawer/_toggleSkill/_renderComposerChips、自动补全(ac) 相关、openTrash/closeTrash 等
})(window.App);
```

从 `app.js` 剪切上述方法到此文件。

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/app.chat.js`
Expected: 无输出。

---

## Task 3.4: 建 app.market.js

**Files:**
- Create: `static/js/app.market.js`

- [ ] **Step 1: 搬迁行情方法**

```javascript
/* app.market.js — 报价卡/K线echarts/自选/侧栏搜索/联动。挂 window.App。 */
(function(App){
  'use strict';
  // 搬迁：_selectStock、_renderQuoteCard、_renderKline、renderKlineChart、_renderFundamentals、
  // _askAboutStock、watchlist(_load/_save/_add/_remove/_render/_start/_stop/_refresh)、
  // _marketSidebarRunSearch、_bindMarketPanel、openMarket/closeMarket 等
})(window.App);
```

从 `app.js` 剪切。搬迁后 `app.js` 应仅剩空壳或被删除（见 3.5）。

- [ ] **Step 2: 语法检查**

Run: `node -c static/js/app.market.js`
Expected: 无输出。

- [ ] **Step 3: 全四文件语法检查**

Run: `for f in util core chat market; do node -c static/js/app.$f.js || break; done && echo ALL_OK`
Expected: `ALL_OK`。

---

## Task 3.5: app.html 接入 4 文件 + 删除旧 app.js

**Files:**
- Modify: `templates/app.html`（script 引用，约 122-124 行）
- Delete: `static/js/app.js`（内容已全迁出）

- [ ] **Step 1: 改 app.html script 顺序**

把第 122-124 行替换为（`?v=26`）：

```html
<script src="/static/lib/marked.min.js?v=26"></script>
<script src="/static/lib/echarts.min.js?v=26"></script>
<script src="/static/js/app.util.js?v=26"></script>
<script src="/static/js/app.core.js?v=26"></script>
<script src="/static/js/app.chat.js?v=26"></script>
<script src="/static/js/app.market.js?v=26"></script>
```

> 顺序关键：util → core（建 App 对象 + init）→ chat → market。core 的 init 在 DOMContentLoaded 触发，需确保 init 监听在 core 内且所有方法已挂载后再跑（若 init 立即执行，改为在最后一个文件末尾或 DOMContentLoaded 内调用）。

- [ ] **Step 2: 删除旧 app.js**

Run: `rm static/js/app.js`
（确认四文件已含全部原方法后再删。）

- [ ] **Step 3: 结构断言**

Run: `ls static/js/app.util.js static/js/app.core.js static/js/app.chat.js static/js/app.market.js && ! ls static/js/app.js 2>/dev/null`
Expected: 4 文件存在、app.js 不存在。

---

## Task 3.6: 全交互回归 + 阶段3 部署

**Files:**
- Create: `/tmp/prism-deploy-v08-p3.sh`

- [ ] **Step 1: 本地全交互回归清单**

浏览器（本地或部署后）逐项验证**行为与拆分前完全一致**：
- 对话：发消息、流式渲染、卡片操作、composer、技能抽屉、自动补全 `/`、quick-ask
- 行情：图标切换、搜索、自选增删刷新、报价卡、K线（含周期切换/主题重绘）、quote-chip 联动、问AI
- 通用：主题/宽窄切换、折叠侧栏、登录态、回收站、报告列表

任一异常 → 方法漏迁或挂载顺序错，回到对应 3.x 任务补。

- [ ] **Step 2: 写部署脚本**（rsync 4 个新 js + app.html + 删 prod 旧 app.js + chown + curl smoke `?v=26`）

```bash
#!/bin/bash
set -e
SRC=/home/develop/projects/prism; DST=/home/ubuntu/stock-analyzer
sudo rsync -av "$SRC/templates/app.html" "$DST/templates/app.html"
for f in util core chat market; do sudo rsync -av "$SRC/static/js/app.$f.js" "$DST/static/js/app.$f.js"; done
sudo rm -f "$DST/static/js/app.js"
sudo chown ubuntu:ubuntu "$DST/templates/app.html" "$DST"/static/js/app.{util,core,chat,market}.js
for f in util core chat market; do echo -n "  app.$f.js?v=26: "; curl -sS -o /dev/null -w "%{http_code}\n" "http://localhost:8088/static/js/app.$f.js?v=26"; done
echo "=== 阶段3 完成 ==="
```

- [ ] **Step 3: 后端 no-regression smoke**

Run: `cd /home/develop/projects/prism && /home/ubuntu/stock-venv/bin/python -m pytest tests/ -q 2>&1 | tail -3`
Expected: 全绿。

- [ ] **Step 4: 人工验收（用户）**

浏览器硬刷新，跑 Step 1 回归清单。全过 → **v0.8 全部交付，可统一 commit。**

---

## Self-Review 结论

- **Spec 覆盖**：图标栏(1.1-1.2)、per-panel 侧栏宽(1.3)、精简顶栏(1.4)、K线样式1+配色表+主题跟随(1.5-1.6) ✓；消息卡片化(2.1-2.3)、悬浮composer+抽屉+模型挪入(2.4-2.5) ✓；4模块拆分(3.1-3.5) ✓；首页默认对话(core 的 init 不主动 switchMode('market')，默认 chat）✓；自选不做空壳（无任务=不建）✓；联动保留（3.6 回归覆盖）✓。
- **占位扫描**：无 TBD/TODO；代码步骤均含完整代码或明确搬迁清单。
- **类型/命名一致**：`_chartPalette`/`renderKlineChart`/`switchMode`/`_toggleSkillDrawer`/`_renderComposerChips`/`_copyMsg` 等命名前后一致；`window.App` 扩展模式统一。
