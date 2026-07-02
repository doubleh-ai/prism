# Prism 待解决问题 & 后续方案

> 更新: 2026-06-23 | Phase 1 ✅ 完成 (268 tests)

---

## 🐛 已知 Bug

### B1. Markdown 加粗 `**text**` 渲染异常
- **现象:** 对话中 AI 输出的 `**加粗文字**` 显示为原始星号，未转为粗体
- **根因:** `app.chat.js` 的 `_glossifyTerms()` 函数在 `marked.parse()` 之后对已渲染 HTML 做正则替换，`==([^=]+?)==` 正则可捕获包含 `<strong>` 标签的内容并重新转义
- **影响:** 所有 AI 对话中 markdown 加粗不生效
- **修法:** `_glossifyTerms` step 1 增加 markdown 语法检测，跳过含 `*_~`\`` 的匹配；或改为在 markdown 渲染之前做 glossary 替换

### B2. HTML 残留符号 `">` 出现在对话中
- **现象:** 对话输出出现 `能和成本。">` 等 HTML 属性碎片
- **根因:** `_glossifyTerms` step 2 使用 `>([^<]+)<` 正则处理已渲染 HTML，可能捕获 HTML 属性闭包中的文本片段并暴露为可见文本
- **修法:** 改用 DOM-based text node 遍历替代正则；或增加后处理过滤孤立的 `">` 模式

### B3. ~~分析模式"一键分析"按钮不可见~~ ✅ 已修复
- **根因:** CSS 选择器 `.new-chat-btn`（后代）错误隐藏了嵌套在 `.sidebar-analysis` 内的按钮
- **修复:** 改为 `.sidebar>.new-chat-btn`（子选择器）；按钮重设计为醒目的渐变蓝色样式

---

## 📋 Phase 2 — 前端流式渲染优化

| # | 项目 | 说明 |
|---|------|------|
| P2-1 | `renderOne` 热路径优化 | 当前每帧全量重建 innerHTML，改为增量更新（只更新变化的 chunk） |
| P2-2 | 虚拟滚动 | 对话消息超过 50+ 条时，只渲染可视区域 + 缓冲区 |
| P2-3 | SSE 断线重连 | `fetch+getReader` 无自动重连，需加 `reconnect` 逻辑 |
| P2-4 | Stream 去重逻辑加固 | `_lastSig` 基于 JSON.stringify(chunks)，大文本下性能差 |

## 📋 Phase 3 — 行情面板 + DOM 优化

| # | 项目 | 说明 |
|---|------|------|
| P3-1 | Quote chip 批量渲染 | 对话中多个 `[[quote:XXX]]` 改为一次 API 批量查询 |
| P3-2 | ECharts dispose | 切换股票时正确销毁旧图表实例，避免内存泄漏 |
| P3-3 | 消息渲染数量上限 | 对话消息超过阈值时折叠旧消息（可展开），限制 DOM 节点数 |
| P3-4 | 行情数据定时刷新节流 | 自选列表 60s 刷新改为指数退避（活跃时 30s，后台 120s） |

## 📋 Phase 4 — 离线 + Service Worker

| # | 项目 | 说明 |
|---|------|------|
| P4-1 | Service Worker 缓存 | 静态资源离线可用，第二次加载 < 50ms（当前已 gzip + immutable） |
| P4-2 | IndexedDB 对话缓存 | 切换对话时优先从本地读取，避免网络往返 |
| P4-3 | SSE reconnect with backoff | 断线后指数退避重连（1s → 2s → 4s → ... → max 30s） |

---

## 🔧 性能 Debt（Phase 1 遗留）

| # | 项目 | 说明 |
|---|------|------|
| D1 | `persist_graph` 多语句非原子 | DELETE + INSERT 在 autocommit 下不保证原子性；应加显式 `BEGIN/COMMIT` |
| D2 | `conversations.py:_read_jsonl_messages` 仍同步 | 与 `chat.py` 的 async 版本重复；两处逻辑可能漂移 |
| D3 | `mmap_size` 无降级测试 | 仅 `try/except + log.warning`，缺少单元测试覆盖 mmap 失败路径 |

---

## 🎨 UI/UX 小修

| # | 项目 | 说明 |
|---|------|------|
| U1 | 搜索栏位置对齐 ✅ | 行情/分析两个模式的搜索栏现在使用统一的 `.sidebar-mode-title` 间距 |
| U2 | 搜索响应速度 ✅ | debounce 250→150ms；akshare stock list 缓存 30min；模式切换预暖 |
| U3 | 一键分析按钮重设计 ✅ | 全宽渐变蓝色按钮 + 播放图标，hover 增亮 |
| U4 | 切换模式侧边栏宽度跳变 | 对话↔行情↔分析切换时侧边栏内容不同可能导致宽度瞬间变化 |

---

## 📊 统计

- **Phase 1 完成:** 7 tasks, 23 new tests, 268 total passing
- **待修 Bug:** 2 (B1 Markdown 加粗, B2 HTML 残留)
- **后续 Phase:** 3 (P2-P4, 共约 11 项)
- **性能 Debt:** 3 项
