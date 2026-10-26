# Changelog

All notable changes to Prism are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/), and this project
follows [Semantic Versioning](https://semver.org/).

## [v0.21.0] - 2026-10-26

### 原生研究 Agent
- 研究对话使用自有模型与工具循环、预算、持久化事件及后台任务归属，直接调用模型 API。
- 刷新或重开会话后以 GET 接回原研究，停止确认后台终态后恢复发送；断线不会重复提交研究。
- 自动保留网页研究版本，支持所属账号历史检索；来源、日期、证据缺口和未核验状态分别保存。
- 原始引用链接保留工具返回的查询参数；搜索线索不冒充原文核验或正式发布日期。

### 兼容与项目文档
- 保留旧对话的真实历史边界；新数据表及持久删除触发器支持代码回退与旧版删除行为。
- 初始化失败会释放尚未执行的研究配额，不误终结其他执行者的任务。
- 新增中文项目指南、架构地图和关键设计决定；公开发布规范保留可移植规则，环境专属运维继续私有保存。
- 本条记录代码变更；实际是否上线以服务完整修订号及独立发布记录为准。Astock、Vibe 等专用引擎继续逐项迁移。

### 反馈与持续改进
- 每页提供反馈入口、异常提示、本人处理进度和管理员评审队列；建议与异常分别进入处理流程，保留答复与验证历史。
- 诊断区分版本与匿名拒绝访问，主动取消不再误记网络故障，登录输入错误不再抛出服务器异常。
- 自动处理使用独立受限身份与私有运维流程；应用不执行反馈文字，完成状态保留验证说明和目标版本。

## [v0.20.0] - 2026-10-21

### 研究工作台与完整旅程
- 对话、平台能力、任务与报告共用可回看的记录；能力结果保存实际参数与快照，跳转不重复执行。
- 修复问答与报告显示、股票/周期切换、迟到响应、断线及重启恢复；统一北京时间，未配置模型不再默默成为默认选择。
- 新增逐轮研究账本、原始工具收据、资料定位、候选结论及产业机制矩阵；区分线索、披露、计算、推断、冲突和缺口，不生成虚构可信度。
- 报告按用户和研究轮次保留固定版本；修复共享文件报告隔离，生成 HTML 采用响应级 sandbox。
- 修复 RSI 及指标预热、基本面缺失、扫描覆盖与过期行情保留规则；未知数据不会被当作有效的未匹配结果。

### 发布与恢复
- 增加固定目标的受控发布执行器、加密备份恢复检查与独立操作记录；候选、运行版本及核心验收分别核对。
- 固定运行依赖和摘要，保留旧私有历史、凭证与运行数据在 Git 之外；公开候选不含这些内容。
- 保留 CI 与候选构建，移除非必要的 Codecov 上传；同一 PR 新检查会取消过期的旧检查。

## 工作台前序开发记录（已纳入 v0.20.0）

以下保留前序隔离开发时的说明；当前交付能力以发布流程文档及实际阶段记录为准。

### Added
- **发布观察与候选边界**：管理员可查看真实 GitHub 工作流结果及观察时间；当前流程仅准备候选版本（candidate-only），未接入生产发布、重启或回滚。外部取消链接用于前往 GitHub 操作，不表示生产已停止或已回滚。
- **公开基础就绪检查**：新增 `/health/ready`，只执行数据库基础检查，返回状态、版本、完整修订号或 `unknown`、检查时间及时区；不暴露运行路径或模型配置，不把数据库可用当作发布验收。
- **统一研究入口**：对话内展示后端真实能力清单、运行状态和结果卡；会话与各能力工作区均可查看自己的调用记录，并在结果与原会话间往返。打开既有分析、Vibe 或选股任务只导航，不重复启动。
- **内置研究方法**：新会话默认使用 `prism-research`；缺失的外部技能会明确回退。仍需配置真实模型及相应能力依赖，不生成虚构结果替代失败。
- **能力与历史接口**：新增 `/api/capabilities`、`/api/convs/{conv_id}/capability-calls` 和按 `view` 筛选的 `/api/capability-calls`，均按当前账号隔离。内部 Agent 桥默认使用 `PRISM_INTERNAL_API_URL=http://127.0.0.1:8088`；自定义服务端口须同步配置。

### Changed
- **发布操作说明**：移除旧 `/tmp` 发布脚本与覆盖线上目录的部署示例；记录生产接入前必须完成的环境确认、不可变候选、备份、单写锁、预检、功能验收和回滚边界，未实施的步骤明确保留为后续要求。
- **全局工作台外壳**：命名导航、统一排版与颜色、移动端工作区入口；网页研报优先，已有导出方式保留。静态资源版本更新为 `v=78` / `prism-workbench-v78`。
- **研究与数据时点分离**：每轮保存实际研究截至时间、北京时间时区与运行编号，来源发布、采集和行情数据时点独立标注；未知日期不补造。
- **来源边界**：外部网页、平台数据与历史研究分别记录；采集成功和链接数量不等于事实核验。Vibe 原报告与后续追问保留各自研究时点，历史产业种子信息不再默认展示为已核验关系。

### Fixed
- **健康检查误报**：管理诊断使用实际默认的内置研究方法，缺失旧方法不再单独判为故障；Docker 改用免登录的基础就绪接口，避免受保护接口返回 401 导致恒定失败。
- **会话与来源归属**：迟到响应不再覆盖当前会话；后台流保留原会话消息引用，重复渲染不再累加来源，技能回退提示和能力调用进度可在流式回复中显示。
- **登录与恢复**：退出会调用服务端注销并清理本地对话缓存；缓存按账号隔离。Vibe 进度读取遇临时网络错误会重试；聊天连接中断不再自动重复提交研究 POST。
- **分析结果与历史**：分析结果可按所属账号读取既有记录；流在结构化提取完成后明确结束，避免普通报告完成事件提前截断后续仪表盘结果。

### Tests
- 新增执行真实前端代码的 Node 回归，覆盖乱序响应、后台流归属、来源去重与时间边界、注销、Vibe 断网恢复、能力卡跳转及发送按钮绑定；既有 Markdown 与静态契约继续保留。此开发条目不表示已经部署，真实模型及外部数据源可用性需在配置环境中另行验证。

## [v0.19.7] - 2026-07-13

### Added
- **产业图谱实时数据层**: 图谱现在会按当前账号汇总 Astock 分析、Vibe 研究候选、报告库和行情自选四类数据，并把最新评级、来源和更新时间叠加到已有产业节点。
- **跨模块动态与待归链**: 总览新增最近研究动态和待归链候选；无法由现有证据可靠匹配产业的标的不会被强行写入结构图，可直接跳转行情继续核查。
- **节点联动详情**: 产业卡展示活跃研究标的，链路页展示来源计数和最新信号，节点抽屉展示该股票最近的分析与报告证据。

### Changed
- **自动刷新**: 打开产业图谱时立即获取跨模块数据，停留期间每 90 秒刷新一次，也可通过顶栏刷新按钮手动同步。
- **结构与信号分层**: `industry_nodes` / `industry_edges` 继续只承载经过验证的产业关系；跨模块数据采用只读实时聚合，不把股票相关性自动伪造成供应链连接。

### Security
- **账号数据隔离**: 联动 API 强制登录并只读取当前账号的分析缓存、Vibe 任务、保存报告和自选行为，不向其他用户暴露研究记录。

### Tests
- **产业联动回归**: 覆盖四类来源聚合、股票代码规范化、最新自选状态、用户隔离、已归链与待归链判定，以及前端自动刷新和响应式界面契约；在桌面与 `390px` 手机视口完成浏览器验证，发布资源升级为 `v=77` / `prism-v0197`。

## [v0.19.6] - 2026-07-12

### Added
- **对话内完整报告**: Agent 交付 HTML、PDF 或 Markdown 报告后，会在同一条回复下方直接展开安全阅读器；用户可随时收起，或点击“独立阅读”进入完整页面。
- **报告文件元数据**: 新回复会持久化本次交付的报告文件列表；旧会话也会从历史思考与工具记录中识别 `reports/...` 路径，无需重新生成即可使用内嵌阅读。

### Changed
- **交付回复分层**: 对话正文只保留交付主题、数据日期和一句话结论，完整长报告交给下方阅读器承载，减少重复滚动。
- **完成态自动折叠**: 已完成的思考过程与工具命令默认折叠；流式生成期间仍保持展开，不影响观察 Agent 当前进度。

### Fixed
- **中文交付标题识别**: 修复 JavaScript 单词边界无法匹配中文“报告已交付”的问题，确保长报告摘要能够稳定收敛。
- **协议残片清理**: 清除主题行中偶发的 `">`、`'>` 等结构化输出残片，避免其混入最终报告标题。

### Security
- **隔离报告执行环境**: 对话内 HTML 报告使用仅允许脚本、禁止同源权限的 sandbox iframe，并关闭 referrer 传递，降低报告内容访问 Prism 会话数据的风险。

### Tests
- **报告阅读回归**: 覆盖中文标题识别、旧会话报告发现、元数据持久化、安全沙箱、协议残片清理与完成态折叠；在桌面和 `390 x 844` 竖屏视口完成实际浏览器检查，发布资源升级为 `v=76` / `prism-v0196`。

## [v0.19.5] - 2026-07-12

### Fixed
- **Vibe 原始错误墙**: 工具活动不再直接展示 `HTTPConnectionPool`、搜索后端 JSON 和 `No results found` 等原始返回；后端保存简短用户摘要，同时将原始详情单独保留用于审计与故障排查。
- **历史任务兼容**: 前端会识别旧研究记录中的 JSON、连接错误和空搜索结果，并转换为统一的“数据源不可达”“未找到公开证据”等可读状态，无需重新执行历史研究。

### Changed
- **重复缺口聚合**: 相同工具、状态和原因的失败会合并显示；例如连续 7 次公开检索未命中只占一个条目，具体查询默认折叠在“7 次调用”中。
- **研究质量摘要**: 左侧统计改为采集有效率、三色结果比例条和简洁图例，明确区分有效结果、未命中和接口异常；报告缺口与来源链接保留为独立证据指标。
- **过程信息降噪**: 完成态默认只显示“数据缺口”和“已完成”两个折叠入口，运行中的工具最多保留最近 5 项，避免活动列表挤压报告阅读空间。

### Tests
- **Vibe 活动回归**: 覆盖工具摘要生成、原始详情留存、旧 JSON 自动清理、重复问题分组和前端质量指标契约；使用截图中的 `21 / 13 / 1 / 7` 数据在桌面及 `390px` 手机视口完成视觉检查，发布资源升级为 `v=75` / `prism-v0195`。

## [v0.19.4] - 2026-07-12

### Fixed
- **分析报告列表越界**: 修复全局样式清零 `ul/ol` 缩进后，风险评估等 Markdown 列表的项目符号落到折叠区边线之外的问题；无序列表改为正文内的统一圆点，有序列表恢复清晰编号。
- **分析角色标题渲染**: `Aggressive Analyst: # 标题` 等 Agent 原始前缀会在渲染前规范化，不再把英文角色名和 Markdown 井号直接显示在正文中。

### Changed
- **报告阅读排版**: 分析报告增加可读行宽、稳定的标题层级、段落节奏、嵌套列表、引用块与代码块样式；展开的分析分支使用轻量底色和强调边线区分层级。
- **移动端报告密度**: 竖屏手机缩小报告内边距和标题字号，列表与长句保持自然换行，不产生横向滚动。

### Tests
- **Markdown 视觉回归**: 新增分析角色规范化与标题/列表运行时测试、报告 CSS 契约测试，并在 `1440px` 桌面与 `390px` 竖屏视口完成视觉检查；发布资源升级为 `v=74` / `prism-v0194`。

## [v0.19.3] - 2026-07-12

### Added
- **多级均线放量共振**: 指标库新增一个真正的复合信号，默认组合 `MA5 上穿 MA10`、`MA10 上穿 MA20` 和“当日成交量大于前 5 日均量 3 倍”三项条件；只有三项全部满足时才命中，可直接用于当前股票试算或全市场选股。
- **组合条件配置**: 编辑器逐条展示复合指标中的三个条件并标记 `全部满足 AND`；短/中/长期均线、交叉共振窗口、前序均量窗口和放量倍数均可独立调整，并提供同日严格、三日共振和中期趋势三个预设。

### Changed
- **大白话多条件翻译**: 自然语言翻译器现在能识别三条均线、重复出现的中期均线、中文数字“前五日”和成交量倍数，不再只取前两条均线后丢弃剩余条件。
- **前序量能口径**: “前 N 日均量”明确排除触发当日，通过前移后的滚动均量计算，避免当天巨量同时抬高自身比较基准。
- **信号窗口消歧**: 内部“交叉共振窗口”控制两次金叉允许相隔几天；扫描页面原“触发窗口”改名为“信号回溯”，表示结果向前查找多少根 K 线。
- **现有账号补种**: 新复合模板会对已有指标库一次性补充；用户主动删除后不会在刷新时重复出现。

### Tests
- **复合策略回归**: 覆盖三条件严格 AND、前序均量排除当天、中文自然语言翻译、现有账号补种幂等性、删除保留和组合配置界面；发布资源升级为 `v=73` / `prism-v0193`。

## [v0.19.2] - 2026-07-12

### Fixed
- **真实周线与月线**: Tencent/Sina 和 AkShare 仅提供日线时，服务端按中国市场时区重新聚合 OHLCV；周线使用首日开盘、周期最高/最低、末日收盘并累计成交量，月线按自然月聚合，不再把同一组日线重复显示成周线或月线。
- **数据源周期契约**: Baostock 改为调用原生 `d / w / m` 周期并按周期扩展历史回溯窗口；Provider Chain 对所有数据源统一做周期规范化，避免单个数据源忽略 `period` 后污染图表。
- **旧周期缓存隔离**: K 线缓存键升级为 `kline-v2`，不会继续读取此前错误缓存的“伪周线/伪月线”。
- **A 股颜色规范**: K 线、成交量、最新价线、行情报价、自选列表和报价卡统一为红涨绿跌，修复局部沿用美股绿涨红跌的问题。

### Changed
- **周期独立视野**: 日线、周线、月线分别记忆自己的数据量；日线提供 3 月至全部，周线提供半年至 5 年，月线提供 1 年至 10 年的快捷范围，切换周期不再复用上一周期的窗口。

### Tests
- **周期与颜色回归**: 新增日转周/月 OHLCV、三个行情数据源、缓存代际和前端红涨绿跌契约测试；发布资源升级为 `v=72` / `prism-v0192`。

## [v0.19.1] - 2026-07-11

### Added
- **Vibe 硬轮次预算**: 聚焦、深度和多 Agent 研究分别使用 `6 / 14 / 28` 轮上限；最后一轮沿用官方引擎的纯文本强制收敛，减少无止境工具探索但仍保证交付报告。
- **预算可见性**: Vibe 研究侧栏展示本次轮次上限和预计时长，用户在启动前选择的研究深度现在对应明确的执行成本。
- **可追踪 sidecar 补丁**: 保存针对 `HKUDS/Vibe-Trading@d456025` 的独立统一 diff；补丁只让官方 `SessionService` 读取会话 `max_iterations`，并将值夹紧在 `3-50`。

### Changed
- **官方引擎会话配置**: Prism 创建 Vibe sidecar 会话时传入研究深度与对应 `max_iterations`，不再只依赖模型遵守提示词里的软预算。

### Tests
- **真实运行 A/B**: 同一“存储产业”聚焦研究从 `251.0s / 12轮 / 25工具 / 484,759 tokens` 降至 `135.6s / 5轮 / 11工具 / 192,674 tokens`，仍生成 9,058 字报告和 5 个候选；耗时减少约 46%，工具调用减少 56%，token 减少约 60%。
- **发布与补丁校验**: 覆盖三档预算映射、sidecar 会话配置，以及补丁对当前官方源码的 dry-run；静态资源和 Service Worker 升级到 `v=71` / `prism-v0191`。

## [v0.19.0] - 2026-07-11

### Added
- **异常自检测中心**: 浏览器自动上报 JavaScript 异常、未处理 Promise、可见的 `[object Object]`、空白核心面板和 K 线空画布；服务端同时捕获 API 5xx，并按指纹聚合重复问题。
- **Codex 诊断包**: 管理看板新增“异常诊断”页，可查看发生时间、用户、组件、版本、脱敏栈和出现次数，支持标记已处理及一键复制 Codex 诊断包；不会采集对话正文、密码、Cookie、请求体或模型密钥。
- **专业 K 线工具栏**: 新增 3 月/半年/1 年/全部区间、MA5/10/20/60 显隐、缩放复位和全屏；十字光标补充涨跌幅、振幅、成交量/额和均线值，并标记区间高低点、昨收及最新价。

### Fixed
- **AStock 最终状态**: 正确兼容 TradingAgents-astock 的 `stream_mode=values` 完整快照，不再把 `investment_debate_state`、`risk_debate_state` 拆平，修复分析到 100% 后报 `KeyError: investment_debate_state` 的根因；异常退出也会关闭图运行资源。
- **对话最终报告**: 恢复仅出现在 Claude CLI `result` 事件中的最终文本；若 Agent 已生成报告文件则自动在对话中交付链接，落库和报告快照完成后才发送 `[DONE]`，错误事件不再让前端提前断开。
- **Vibe 报告对象渲染**: Vibe 正文统一通过全局安全 Markdown 渲染器处理，结构化 sidecar 内容先提取人类可读文本，避免对象被隐式拼接。
- **产业链连线**: 链路视图只绘制上游到中游、中游到下游的相邻主链；同层、逆向与跨层关系保留在详情和瓶颈统计中，不再穿过中间卡片。
- **自选股名称**: 行情源正式股票名称优先于 Vibe 候选角色或旧缓存名称，并在批量刷新后持久化到自选列表。

### Changed
- **报告优先交付**: 对话 Agent 在核心证据足够后先输出以“报告已交付”开头的中文 Markdown，再生成可选 HTML/PDF，避免任务停在工具或写文件阶段。
- **Vibe 深度与质量口径**: 聚焦/深度/多 Agent 模式分别使用明确的工具预算；区分有效成功、真实失败、空结果/降级和报告披露缺口，不再把 `ok:false` 或无结果响应统计为成功。
- **Vibe 轮询负载**: 改为无重叠的自适应轮询，前 30 秒快速刷新，长任务及后台标签页自动降频。

### Tests
- **关键故障回归**: 覆盖 AStock 嵌套状态、Claude terminal result 恢复、Vibe 结构化消息/业务失败识别、诊断脱敏与管理员权限，以及 K 线、产业链和诊断前端契约。
- **静态资源版本**: CSS、前端 JS、第三方资源和 Service Worker 缓存统一升级到 `v=70` / `prism-v0190`。

## [v0.18.3] - 2026-07-11

### Fixed
- **全局 Markdown 渲染**: 按内置 `marked v15` 的 token 接口重写段落、引用、链接和图片渲染器，修复报告、聊天、AStock 与 Vibe 页面把 Markdown token 显示为 `[object Object]` 的根因。
- **报告详情浅色主题**: 顶部操作栏改用主题背景与边框，不再在浅色页面出现黑色渐变条。
- **报告输出契约**: 报告详情只接受经过校验的 HTML 字符串；渲染器返回对象或异常时安全降级为纯文本，不再发生对象隐式拼接。

### Changed
- **未完成研究识别**: 深度研究在形成最终报告前中止时，报告库明确显示“研究未完成”，过程信息默认折叠并提供返回原对话继续分析的入口。
- **聊天报告快照**: 新生成的聊天报告只保留“报告已交付/最终结论”等最终交付段落；超时或中止任务记录为 `incomplete`，不再把 Agent 开场过程当成最终决策和摘要。

### Tests
- **真实 Marked 运行时回归**: 直接加载项目内置 `marked v15.0.12` 验证段落、加粗、列表、链接和报告正文，断言输出不得包含 `[object Object]`。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=69` / `prism-v0183`。

## [v0.18.2] - 2026-07-11

### Fixed
- **产业图谱浅色主题**: 修复产业 KPI、关键节点、产业卡片、链路节点和详情抽屉写死深色主题颜色，导致浅色背景下文字与边框近乎不可见的问题。
- **主题切换重绘**: 在产业工作台切换深浅主题时主动重建区域地图和链路小地图，地图底色、边界与散点不再保留上一主题的配色。
- **Service Worker 更新缓存**: `/static/sw.js` 改为强制重新验证，不再继承静态资源的一年 immutable 策略，避免旧版本页面长期停留在历史版本号。

### Tests
- **双主题与缓存回归**: 新增产业工作台浅色对比度、主题重绘和 Service Worker 非 immutable 缓存测试。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=68` / `prism-v0182`。

## [v0.18.1] - 2026-07-11

### Changed
- **指标参数语义化**: 周期、阈值、倍率不再统一显示为同一种滑杆；周期提供常用值快捷选择，阈值显示触发边界，倍率提供常用倍数组合，并为每项补充用途和敏感度说明。
- **指标逻辑优先**: 配置页先展示随参数实时变化的触发逻辑与“灵敏 / 经典 / 稳健”等快速方案；通用股票池、周期和排序配置收纳到独立的“扫描方案”折叠区。
- **选股参数一致性**: 全市场选股页复用指标库的语义化控件和实时逻辑说明，编辑与实际扫描不再出现两套交互。

### Fixed
- **旧指标自动升级**: 已存在账号中的内置和自定义指标会在读取时自动补齐控件类型、参数分组、帮助文案、逻辑模板和预设，无需删除后重新创建。

### Tests
- **参数元数据回归**: 覆盖均线、RSI、放量、MACD、布林带的不同参数家族，以及旧版参数记录的无损兼容。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=67` / `prism-v0181`。

## [v0.18.0] - 2026-07-11

### Added
- **官方 Vibe-Trading 引擎**: Prism 通过独立、仅监听本机回环地址的 sidecar 直接接入 `HKUDS/Vibe-Trading 0.1.11`，固定上游修订 `d456025`，研究过程可调用行情、财务、新闻、网页检索、技能和多 Agent 工具。
- **深度研究协议**: 新增聚焦研究、深度研究和多 Agent 三档；模糊主题会先做语义消歧，再完成产业链、卡点、证据账本、候选池、反方论证、失效条件和数据缺口。
- **实时研究轨迹**: 工作台展示当前阶段、耗时、工具调用/成功/失败数、参数摘要和结果状态；已完成工具默认折叠，不显示模型私有思考文本。
- **上下文追问与取消**: 完整报告生成后可在同一官方会话继续追问；长任务可主动停止，历史记录仍保留此前进度和工具证据。

### Changed
- **Vibe 工作台重构**: 原候选卡片页升级为宽版研究工作台，完整渲染 Markdown 报告、横向表格、来源链接、证据质量警告和报告候选快捷入口。
- **失败语义透明化**: 移除关键词命中静态主题后冒充最终分析的路径；官方引擎不可用、超时或无报告时明确显示失败，不再静默返回规则候选池。
- **“存储”研究消歧**: 不再把“存储”直接映射为半导体设备；研究协议要求区分存储芯片、企业数据存储、能源储能等可能含义并用证据决定重点。

### Fixed
- **官方报告候选识别**: 同时识别候选表格和 `候选N：名称（代码）` 分节格式，确保完整报告生成后仍可直接打开行情或加入自选。

### Security
- **隔离执行**: 官方引擎不对公网开放，Prism 继续负责登录与数据所有权；sidecar 禁用 shell/live execution，前端只接收脱敏后的工具参数和结果摘要。

### Tests
- **Vibe 端到端契约**: 覆盖任务即时入历史、官方会话创建、工具轨迹、报告持久化、候选提取、上下文追问、失败不兜底及工作台静态契约。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=66` / `prism-v0180`。

## [v0.17.0] - 2026-07-11

### Added
- **全市场指标选股**: 行情工作台新增后台扫描任务，可按指标库在沪深京全部 A 股中筛选，并按市场、板块、周期、触发窗口、成交额与结果数量过滤。
- **可调指标方案**: 指标库中的内置与自定义指标均可用滑杆和数字输入微调参数，并保存股票池、周期、排序方向、成交额、市场/板块及显示样式等默认方案。
- **任务进度与历史**: 扫描过程实时展示股票池、已处理、命中和失败数；任务离开页面后继续执行，返回后自动恢复，并支持查看或删除历史结果。
- **结果联动**: 筛选结果可直接打开个股行情或转入 A 股多 Agent 分析，行情详情也可一键试算指标或进入全市场扫描。

### Changed
- **行情工作台重构**: 行情页改为“个股行情 / 全市场选股 / 指标库”三页签宽版工作区，移除挤在窄侧栏中的指标配置。
- **折叠屏与手机布局**: `900px` 以下使用底部导航和可拉起侧栏，释放横向空间；扫描结果在手机上自动切换为信息卡片。
- **北交所兼容**: 股票搜索、行情与扫描统一识别 `4xx`、`8xx` 和新 `920xxx` 代码，股票池补全北交所名称数据。

### Performance
- **并行行情读取**: 技术指标扫描复用长连接并以受控并发读取 K 线；真实 5,315 只股票全市场扫描约 35 秒完成，失败数为 0。
- **持久化后台任务**: 扫描结果分批写入数据库，避免长任务阻塞页面请求，并可在刷新页面后继续查看进度和部分命中结果。

### Tests
- **扫描器回归**: 覆盖股票池过滤、任务创建/取消/持久化、指标命中、前端工作台契约及桌面、折叠屏、竖屏手机布局。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=65` / `prism-v0170`。

## [v0.16.7] - 2026-07-11

### Fixed
- **分析深度选项白屏**: 修复 Windows/Chrome 原生下拉选项忽略深色背景、导致未选模式呈现白底白字的问题。

### Changed
- **分析模式切换**: 将“快速 / 深度”原生下拉框改为双按钮分段控件，直接显示 `快速 · 2 Agent` 与 `深度 · 7 Agent`，并同步无障碍选中状态。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=64` / `prism-v0167`。

### Tests
- **模式控件回归**: 新增分段控件、状态同步、样式和原生选项移除的前端契约测试。

## [v0.16.6] - 2026-07-10

### Added
- **管理分析中心**: 管理看板拆分为概览、功能分析、用户行为、账号管理和系统健康五个页签，新增事件/活跃趋势图与北京时间访问时段图。
- **运营洞察 API**: 新增 `/api/admin/analytics/insights`，提供回访率、近似使用会话、活跃粘性、核心路径转化、热门研究对象、P95 耗时和失败状态码分布。
- **用户行为轨迹**: 用户活跃表新增会话数、成功率和“查看轨迹”，行为明细支持账号、功能、动作、状态、关键词组合筛选、分页加载与 CSV 导出。
- **前端行为埋点**: 新增白名单化 `/api/activity`，补充功能打开及自选股添加/移除等纯前端行为，让功能 DAU 不再只依赖 API 调用推断。

### Changed
- **统计口径**: 趋势和活跃日统一按北京时间计算；回访用户定义为所选区间内至少有 2 个活跃日，会话按 30 分钟无操作切分。
- **事件查询性能**: 为状态和研究对象增加活动索引，行为事件自动保留最近 180 天，兼顾 90 天分析能力和数据库体积。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=63` / `prism-v0166`。

### Security
- **最小化采集**: UI 埋点只接受服务端白名单中的功能/动作，不采集对话正文、密码、模型密钥或报告内容。

### Tests
- **分析中心回归**: 新增洞察聚合、转化路径、热门对象、失败状态码、事件搜索/分页、UI 埋点白名单及前端看板契约测试。

## [v0.16.5] - 2026-07-10

### Fixed
- **自选股删除入口**: 行情看板自选列表的删除按钮改为常驻可见，桌面、手机和折叠屏都能直接移除自选股。
- **自选添加稳定性**: 修复行情侧边栏搜索回包误校验隐藏弹窗输入框导致搜索/添加偶发失效的问题。
- **自选代码归一化**: 加入自选前会统一 `sz300750`、`300750.SZ`、`300750` 等 A 股代码写法，并清理 localStorage 中的重复/异常记录。

### Changed
- **Vibe 候选入自选**: 单只和批量加入自选会区分“新增”和“已存在”，避免用户误以为点击无效。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=62` / `prism-v0165`。

### Tests
- **自选回归测试**: 新增前端静态契约覆盖搜索输入校验、ticker 归一化、删除按钮可见性和 Vibe 入自选返回值。

## [v0.16.4] - 2026-07-08

### Added
- **产业链研究工作台**: 产业图谱升级为总览、产业雷达、关键节点、区域地图、链路三列和节点抽屉组成的研究工作台。
- **产业概览 API**: 新增 `/api/industry/overview`，返回产业、节点、关系、地区、标的、关键节点和每条产业链的上下游结构统计。
- **产业搜索增强**: `/api/industry/search` 支持按公司、角色描述、产业分类和股票代码检索，并优先返回完整代码/前缀代码命中。

### Changed
- **产业链交互**: 链路详情从简单地图/列表升级为上游-中游-下游分层视图，支持悬浮高亮供应关系、瓶颈节点排序和一键转入 AI 深挖。
- **移动端适配**: 产业图谱在窄屏和折叠屏上改为单列链路，节点抽屉切换为底部面板。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=61` / `prism-v0164`。

### Tests
- **产业图谱回归**: 新增产业概览 API、搜索排序、工作台 DOM/CSS 静态契约测试。

## [v0.16.3] - 2026-07-08

### Added
- **行为事件中心**: 新增 `activity_events` 持久化表，自动记录登录、后台管理、对话、行情、指标、A 股分析、Vibe Trading、报告库等粗粒度使用行为。
- **管理统计 API**: 新增 `/api/admin/analytics/overview`、`/api/admin/analytics/events`、`/api/admin/analytics/users`，管理员可查看功能 DAU、使用量、失败率、耗时、用户活跃和行为时间线。
- **后台行为看板**: 管理控制台新增行为分析、功能使用排行、用户活跃明细和可按账号/功能/状态筛选的时间线。

### Security
- **敏感信息保护**: 行为统计只记录路径分类、目标摘要、状态和耗时，不保存请求正文；客户端 IP 和 User-Agent 仅保存短哈希。

### Changed
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=60` / `prism-v0163`。

## [v0.16.2] - 2026-07-08

### Fixed
- **选股搜索匹配**: 输入完整 6 位 A 股代码会直接兜底匹配并优先展示，避免完整代码仍停留在相邻代码列表。
- **搜索结果串台**: 行情页和一键分析页会丢弃过期搜索响应，防止旧请求晚返回覆盖当前输入框结果。

### Added
- **多方式搜索**: A 股搜索支持代码精确/前缀/包含匹配、名称匹配、拼音全拼（可用时）和中文首字母匹配，例如 `300607`、`sz300607`、`tsd` 均可命中同一标的。

### Changed
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=59` / `prism-v0162`。

## [v0.16.1] - 2026-07-07

### Fixed
- **聊天底部间距**: 消息滚动区增加底部安全距离和 scroll padding，避免最后一条流式内容、报告块或光标贴到输入框。

### Changed
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=58` / `prism-v0161`。

## [v0.16.0] - 2026-07-07

### Added
- **指标计划工作台**: 指标翻译结果现在会生成可调参数、扫描范围、回看周期、排序方式和显示样式，用户可以在保存前直接调整。
- **K 线信号轨道**: 自定义 signal 指标会在 K 线下方显示独立信号轨道，并在悬浮 tooltip 中展示触发原因、价格和强度。
- **指标扫描说明**: 自选股扫描结果会展示命中时间、价格、强度条和命中原因，方便快速复核。

### Changed
- **指标 DSL 能力**: 支持参数化基础指标、成交额、涨跌幅、区间计数、最近窗口触发和区间波动等表达式。
- **指标翻译策略**: 常见均线、MACD、RSI、放量、突破和 BOLL 先走本地规则翻译，复杂描述再交给 LLM，减少等待时间。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=57` / `prism-v0160`。

### Fixed
- **报告库对象块清洗**: 保存和读取报告时会过滤 `tool_use`、Claude content block 和残留 `[object Object]`，避免最终决策继续显示对象占位符。

### Tests
- **指标回归测试**: 新增参数化 DSL、指标事件返回、前端信号轨道和报告对象清洗的回归覆盖。

## [v0.15.11] - 2026-07-07

### Fixed
- **Astock 进度节点识别**: 后端会把 LangGraph state snapshot 归一化成真实分析节点，不再把 `messages` 显示成分析过程条目。
- **Astock 进度展示**: 流式事件携带中文 Agent 标签，前端使用后端标签并过滤旧事件中的 `messages` 噪音。

### Changed
- **Astock 引擎更新**: 线上 `TradingAgents-astock` 升级到 upstream `b811976` / `0.2.16`，匹配最新 graph 接口和依赖声明。
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=56` / `prism-v01511`。

### Tests
- **Astock stream contract**: 新增回归测试覆盖 `messages` 过滤、报告字段到 Agent 的映射、重复 snapshot 去重。

## [v0.15.10] - 2026-07-07

### Fixed
- **指标叠加 K 线复用**: 行情页叠加技术指标时会把当前图上的 K 线传给指标 API，避免图表已加载但指标评估又重复请求行情源并报“无法获取K线数据”。
- **指标后端兜底**: 指标 API 自行拉取 K 线时会从 150 根自动降级复用 120 根缓存，匹配行情页默认图表缓存。
- **指标周期一致性**: 指标评估跟随当前 K 线周期，切到周线/月线后不再固定按日线计算。

### Changed
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=55` / `prism-v01510`。

## [v0.15.9] - 2026-07-07

### Fixed
- **报告库对象渲染**: 报告详情页会递归抽取 Claude/content block、数组、metrics、分析师报告和引用中的可读文本，避免正文继续显示 `[object Object]`。
- **报告详情布局**: 收紧报告详情页顶部操作区、标题摘要卡片和正文 Markdown 样式，长链接、代码块和列表不会撑破页面。
- **报告摘要抽取**: 后端保存报告时可从对象型 `final_decision` / `content` / `summary` 中生成列表摘要和评级，减少空摘要或脏摘要。

### Changed
- **静态资源版本**: CSS、前端 JS、第三方前端资源和 Service Worker 缓存升级到 `v=54` / `prism-v0159`。

## [v0.15.8] - 2026-07-07

### Fixed
- **Tool step folding**: completed `Read` / `Write` / `Bash` / `TaskUpdate` command steps are now collapsed into an `已完成 N 步` block instead of staying expanded in the conversation.
- **Streaming command display**: while a response is still generating, only the current tool step is shown as active; previous tool steps are folded as completed.

### Changed
- **Static asset version**: CSS, frontend JS, third-party frontend assets, and the Service Worker cache were bumped to `v=53` / `prism-v0158`.

### Tests
- **Chat renderer contract**: added coverage for streaming completed-tool folding and explicit CSS hiding of closed step details.

## [v0.15.7] - 2026-07-07

### Added
- **Mobile shell**: added a phone-first layout with a bottom navigation rail, safe-area padding, and a bottom drawer for mode sidebars.
- **Foldable layout**: added medium-width breakpoints so Vibe Trading, Astock reports, report library cards, admin tables, and market panels collapse cleanly on foldable screens.

### Changed
- **Sidebar behavior**: the top-left menu now opens the mobile drawer on compact screens and keeps the desktop collapse behavior on wider screens.
- **Static asset version**: CSS, frontend JS, third-party frontend assets, and the Service Worker cache were bumped to `v=52` / `prism-v0157`.

### Tests
- **Responsive contract**: added static coverage for viewport safe-area support, bottom navigation, drawer state, compact mode switching, and foldable layout rules.

## [v0.15.6] - 2026-07-07

### Fixed
- **聊天流假死保护**：Claude CLI 连续无输出时后端会发送心跳，超过默认 5 分钟无新内容会自动停止并返回可见提示，避免对话停在空光标和红色停止按钮状态。
- **前端流收尾兜底**：浏览器端增加 7 分钟无事件 watchdog，网络或代理层静默断流时会主动结束生成并展示提示。
- **指标 K 线评估**：修复指标评估把同步行情链路工厂误写成 `await get_chain()` 的问题，避免“无法获取K线数据”误报。

### Changed
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=51`，Service Worker 缓存名升级到 `prism-v0156`。
- **聊天流配置**：新增 `PRISM_CHAT_STREAM_HEARTBEAT` 和 `PRISM_CHAT_STREAM_IDLE_TIMEOUT`，可按部署环境调整心跳和无输出超时。

## [v0.15.5] - 2026-07-06

### Fixed
- **Astock 报告对象污染**：后端会把 LangGraph/LangChain 的 dict/list/message block 转成可读文本，前端也会递归清理旧缓存中的对象值，避免最终决策出现 `[object Object]`。
- **Astock 报告完整性**：流式节点输出改为累计合并状态后再抽取报告，减少只拿到最后一个节点导致的内容缺失。

### Changed
- **Vibe 候选分析默认快速**：从 Vibe 候选点击“分析”会强制使用 Astock 快速模式，并关闭强制重跑，避免继承深度模式导致 7 agent 慢跑。
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=50`，Service Worker 缓存名升级到 `prism-v0155`。

## [v0.15.4] - 2026-07-06

### Fixed
- **Vibe Markdown 渲染**：Vibe 计划的验证触发器、失效条件、风险边界和下一步动作支持安全的行内 Markdown，`**重点**` 不再以源码形式显示。

### Changed
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=49`，Service Worker 缓存名升级到 `prism-v0154`。

## [v0.15.3] - 2026-07-06

### Added
- **Vibe 即时历史记录**：点击“生成 Vibe 计划”后会先保存一条 `生成中` 记录，历史列表立即可见；agent 完成后同一条记录自动更新为最终计划。
- **Vibe 历史删除**：历史列表新增删除按钮，可删除不需要的 Vibe 记录。

### Changed
- **Vibe 生成状态**：计划详情页新增 `Agent 生成中` 状态，生成期间可先查看规则候选池，完成后自动刷新 agent 富化内容。
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=48`，Service Worker 缓存名升级到 `prism-v0153`。

## [v0.15.2] - 2026-07-06

### Added
- **Astock 快速/深度模式**：一键 A 股分析默认使用“快速：行情 + 基本面”，需要完整上下游时可切换“深度：7 agent 全链路”。
- **Astock 报告缓存**：同一用户、股票、交易日和模式下会复用今日报告，可勾选“重新跑”跳过缓存。
- **Astock 耗时追踪**：分析事件流和报告元信息会展示模式、命中的 agent、是否缓存和总耗时，方便定位慢节点。

### Changed
- **Astock 加载体验**：快速模式会把 `selected_analysts` 传入 TradingAgents-astock，并把辩论/风控轮次降到 0，减少不必要的全链路等待。
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=47`，Service Worker 缓存名升级到 `prism-v0152`。

## [v0.15.1] - 2026-07-05

### Added
- **Vibe Trading agent 富化**：`/api/vibe/plan` 默认先调用 Claude/模型 agent 深挖交易直觉，补充供应链瓶颈、验证信号、失效条件和候选排序；调用失败或超时时才回退到规则候选池。
- **Vibe 状态可见**：Vibe 结果页新增 agent 状态标记，明确展示“Agent 已参与”或“规则兜底”。
- **Vibe 模型自动选择**：未显式设置 `PRISM_VIBE_AGENT_MODEL` 时，会优先选择当前已配置 API key 的模型，避免默认 MiniMax 未配置时直接兜底。
- **对话报告快照**：聊天流式输出结束后会把完整回答自动保存到报告库，并关联当前会话，避免只有文本消息、没有可复盘报告。

### Changed
- **Astock 超时策略**：一键 A 股分析硬超时改为 `PRISM_ASTOCK_HARD_TIMEOUT` 配置，默认 1800 秒，避免 15 分钟前端断流但后端仍在跑。
- **Astock 模型默认值**：TradingAgents quick/deep 默认改为 `deepseek-chat`，避免 thinking 模型与工具调用不兼容导致反复重试。
- **静态资源版本**：CSS、前端 JS 和第三方前端资源统一升级到 `v=46`，Service Worker 缓存名升级到 `prism-v0151`。

### Fixed
- **A 股数据源代理绕行**：Astock 分析线程会临时把 eastmoney、sina、cls、同花顺等行情/新闻域名加入 `no_proxy`，减少本机代理导致的数据源连接失败和长时间重试。
- **会话报告入口**：本对话“报告”弹窗现在同时展示 skill 生成文件和保存报告快照，快照可直接跳转到报告库详情。

## [v0.15.0] - 2026-07-03

### Added
- **Vibe Trading 工作台**：新增左侧 Vibe Trading 入口，把一句交易直觉转换为可验证的主题、候选池、触发信号、失效条件、风险边界和下一步动作。
- **Vibe 计划 API**：新增 `/api/vibe/plan`、`/api/vibe/plans` 和 `/api/vibe/plans/{plan_id}`，按登录账号保存历史计划。
- **候选池联动**：Vibe 候选标的可直接加入自选、打开行情或进入一键分析，也可把结构化提示词交给聊天继续深挖。

### Changed
- **静态资源版本**：CSS、所有前端 JS 和第三方前端资源统一升级到 `v=45`，Service Worker 缓存名升级到 `prism-v0150`。

### Fixed
- **A 股 ticker 抽取**：Vibe Trading 在 A 股模式下只识别 6 位股票代码，避免把 `AI`、`CPO`、`HBM` 等主题缩写误加入候选池。

### Security
- **研究边界**：Vibe Trading 仅用于投研灵感整理和风险校验，不接入实盘下单，也不构成投资建议。

## [v0.14.9] - 2026-07-03

### Fixed
- **旧缓存对象占位符**：升级 IndexedDB 会话缓存版本并重建 `convs` store，清除浏览器本地保存的旧 `[object Object]` 消息块。
- **渲染后兜底清理**：聊天消息渲染完成后会扫描 assistant bubble 文本节点，移除残留对象占位符并删除空的 Thinking 块。
- **旧数据库消息清洗**：读取 SQLite fallback 消息时递归清理旧 JSON payload，避免旧脏数据重新进入前端缓存。
- **左下角 SVG 入口**：回收站数量更新不再覆盖 SVG 图标，改为 `data-count` 小徽标。

### Changed
- **静态资源版本**：CSS、所有前端 JS 和第三方前端资源统一升级到 `v=44`，Service Worker 缓存名升级到 `prism-v0149`。

## [v0.14.8] - 2026-07-03

### Fixed
- **流式对象占位符**：SSE 输出边界会清理字符串里的 `[object Object]`，避免 Claude CLI 已经字符串化的对象继续进入浏览器。
- **实时聊天渲染**：`ChatRender.stringify` 保留流式文本空格但移除对象占位，`think` / `text` / `tool` chunk 清理后为空时不再渲染。

### Changed
- **静态资源版本**：CSS、所有前端 JS 和第三方前端资源统一升级到 `v=43`，Service Worker 缓存名升级到 `prism-v0148`。

## [v0.14.7] - 2026-07-03

### Fixed
- **对象占位串渲染**：后端读取 Claude JSONL 和流式事件时会清理 `[object Object]`，前端聊天渲染器也会兜底过滤，避免思考块、工具步骤和历史会话继续显示对象占位串。
- **聊天静态缓存**：聊天渲染相关 JS 统一升级到 `v=42`，避免浏览器继续使用旧 `app.chat-render.js?v=39`。
- **左下角功能图标**：回收站和关系图入口改为 SVG 图标，避免 emoji 字形在部分浏览器中显示成小方块。

### Changed
- **静态资源版本**：CSS、所有前端 JS 和第三方前端资源统一升级到 `v=42`，Service Worker 缓存名升级到 `prism-v0147`。

## [v0.14.6] - 2026-07-03

### Added
- **报告库**：新增用户级报告库，已保存的一键分析报告可在独立页面回看、删除和复盘。
- **一键分析归档**：分析完成后可直接“保存到报告库”，报告快照写入 SQLite，避免刷新后只剩内存态结果。
- **报告库 API**：新增 `/api/reports/library` 和 `/api/analysis/{analysis_id}/save-report`，报告按账号隔离。

### Changed
- **静态资源版本**：报告库 JS/CSS 资源升级到 `v=41`，Service Worker 缓存名升级到 `prism-v0146`。

## [v0.14.5] - 2026-07-03

### Added
- **系统健康面板**：管理员后台新增系统健康区，展示版本、部署修订、数据库、Claude CLI、默认技能、模型 Key 和 skill 路径状态。
- **健康检查 API**：新增 `/api/admin/health`，只对管理员开放，用于排查 default block、skill 缺失、Claude CLI 不可用等链路问题。

### Changed
- **静态资源版本**：后台管理 JS 与 CSS 资源升级到 `v=40`，Service Worker 缓存名升级到 `prism-v0145`。

## [v0.14.4] - 2026-07-03

### Fixed
- **默认分析技能启动**：默认 `bottleneck-hunter` 会在 `~/.claude/skills` 缺失或坏链接时自动回退到项目/用户级 skills 目录，避免流式分析直接失败。
- **Default block 根因修复**：修复线上 `serenity-bottleneck-hunter` 旧路径坏链接导致真实 SSE 只返回错误、页面显示异常块的问题。
- **额外技能映射**：`extra_skills` 现在会先通过 `SKILL_MAP` 映射到真实目录名，避免把 `serenity`、`cn` 当作裸目录名查找。

### Verified
- **真实流式探针**：线上 `/api/chat/stream` 已验证返回 `think + text`，没有 `[object Object]`，数据库保存内容也无对象字符串污染。

## [v0.14.3] - 2026-07-03

### Fixed
- **旧页面缓存滞留**：应用壳页面和登录页改为 `no-store`，Service Worker 对导航请求改为 network-first，避免浏览器继续展示旧版 `v0.14.1` 页面。
- **Service Worker 自动接管**：新 SW 激活后会清理旧缓存、接管客户端并刷新当前页面，减少手动清缓存的需要。

### Changed
- **静态资源版本**：聊天、核心、后台、发布日志和 CSS 资源版本升级到 `v=39`，Service Worker 缓存名升级到 `prism-v0143`。

## [v0.14.2] - 2026-07-03

### Fixed
- **更新日志 Markdown 渲染**：更新日志条目现在复用 `marked` + `sanitizeHtml`，正确显示加粗、行内代码和链接，不再把 `**...**` / `` `...` `` 当作普通文本。

### Changed
- **静态资源版本**：升级 release/js 与 CSS 资源版本，并更新 Service Worker 缓存名，降低浏览器继续显示旧日志弹窗的概率。

## [v0.14.1] - 2026-07-03

### Fixed
- **消息对象渲染**：修复 Claude JSON/SSE 内容块被浏览器显示为 `[object Object]` 的问题。
- **前端缓存**：升级 Service Worker 缓存名和静态资源版本，降低部署后仍看到旧页面的概率。

### Changed
- **管理员权限**：默认只有 `ADMIN_USER`（未设置时为 `admin`）拥有后台管理权限；普通账号不会显示管理入口，也不能访问管理 API。
- **管理入口位置**：管理入口从左侧功能图标区移到左下角 footer，避免与核心功能混在一起。

### Added
- **用户行为统计看板**：后台新增用户数、活跃会话、24h 登录/对话/消息、最近用户活跃等统计数据。
- **会话创建时间**：sessions 表新增 `created_at` 迁移，用于统计最近登录行为。

## [v0.14.0] - 2026-07-03

### Added
- **发布记录中心**：新增 `/api/releases`，从 `CHANGELOG.md` 读取版本、日期、分组变更和部署修订号。
- **页面内更新日志**：左下角版本号可点击查看更新历史，账号控制台也提供“更新日志”入口。
- **新功能主动提示**：浏览器首次看到新版本时展示一次轻量提示，帮助用户发现新增能力。
- **账号控制台上线记录**：记录管理员创建账号、禁用/启用、重置密码、删除账号等能力的上线版本。

### Changed
- **夜间演进规范**：自动演进任务必须同步维护版本号、变更日志、用户可见说明和验证记录。
- **静态资源版本**：前端资源缓存标记升级到 `v=36`，确保用户加载到最新控制台能力。

### Security
- **后台可追溯**：版本信息只对登录用户开放，部署修订号来自服务器 `.deploy-revision`，不暴露密钥配置。
- **账号治理补强**：禁用账号会清理其会话，管理员不能禁用或删除当前登录账号。

## [v0.10] - 2026-06-24

### Added — Astock Multi-Agent Analysis (Phase 2)
- **7-agent LangGraph pipeline**: Market/Social/News/Fundamentals/Policy/HotMoney/Lockup
  analysts → Bull/Bear debate → Research Manager → Trader → Risk debate
  (Aggressive/Conservative/Neutral) → Portfolio Manager decision.
- **SSE real-time progress**: stage timeline + agent grid + progress bar with
  percentage tracking. Each stage emits events so the frontend shows which
  analyst is running.
- **Analysis persistence**: analysis state survives page refresh via
  sessionStorage; auto-reconnects SSE or fetches completed report on restore.
- **Structured report**: rating badge (BUY/SELL/HOLD), foldable analyst
  reports, debate summary, trading plan, risk assessment cards.
- **Copy to chat**: one-click "add to conversation" pastes final decision.

### Added — Industry Chain Graph (Phase 6)
- **`industry_nodes` / `industry_edges` / `geo_regions`** tables in SQLite.
- **12-country seed data** (CN/TW/KR/JP/US/DE/AU/CL/SA/IN/VN/MY) with key
  industries and industrial policy summaries.
- **4 API endpoints**: `/api/industry/graph` (filtered nodes+edges with depth
  expansion), `/api/industry/geo` (region data for world-map rendering),
  `/api/industry/search` (fuzzy name search), `/api/industry/ticker/{ticker}/chain`
  (upstream/downstream traversal).
- **Accumulates over time**: each astock analysis can feed new nodes/edges.

### Added — Technical Indicators
- **30+ indicators** via pandas-ta: SMA, EMA, MACD, RSI, BOLL, KDJ, ATR, OBV, etc.
- **DSL expression parser**: `GET /api/indicators/{ticker}?expr=macd(12,26,9)|rsi(14)`.
- **Cached**: results cached per-ticker for 60s.

### Added — Performance Optimizations (Phase 1)
- **LRU caches**: explain-term (256 entries), market data (64 entries).
- **SQLite PRAGMAs**: WAL mode, 64MB page cache, 256MB mmap, memory temp store,
  5s busy timeout, thread-local connection reuse.
- **aiofiles**: async file I/O for report serving.
- **Tencent async**: tencent market provider uses `asyncio.to_thread`.
- **GZip + Cache-Control**: gzip responses ≥512 bytes; `immutable` for /static/.

### Added — Offline + PWA (Phase 5)
- **Service Worker**: caches static assets (CSS/JS/lib/HTML) for offline access.
- **IndexedDB conversation cache**: stores recent messages locally.
- **SSE reconnect**: exponential backoff (5 retries) on stream disconnect.

### Added — Frontend Optimizations (Phase 3)
- **chunkSig O(1)**: hash-based dedup for chat text chunks, replacing linear search.
- **renderOne**: incremental DOM update — each message renders in isolation.
- **Markdown fixes**: `**bold**` rendering and HTML residual `">` cleanup.

### Fixed
- **Analysis button visibility**: CSS selector (child vs descendant) fixed.
- **AskUserQuestion**: now POSTs `/api/chat/respond` instead of only filling input.
- **Analysis refresh**: sessionStorage persistence + auto-restore on page load.
- **persist_graph atomicity**: explicit `BEGIN`/`COMMIT`/`ROLLBACK` transaction.
- **mmap_size fallback**: graceful degradation when mmap is unsupported.
- **Mode transition**: CSS `@keyframes modeFadeIn` for smooth chat↔market switching.
- **XSS in ref.url**: `safeUrl()` strips `javascript:`/`vbscript:`/`data:` schemes.
- **IDOR in api_graph**: per-user ownership check on analysis nodes.
- **CRLF in X-Request-ID**: stripped before use.
- **Search bar alignment**: matched sidebar width.
- **akshare cache warmup**: pre-fetched on search for faster results.

### Changed
- **JS modules**: 1,830-line `app.js` split into 9 modules (util, core, chat,
  chat-render, market, analysis, industry, indicators, tour).
- **Analysis panel redesign**: progress bar + stage timeline + agent grid (2-col)
  + report cards with foldable sections.
- **Quote refresh throttling**: 30s for active watchlist, 120s for background.
- **K-line periods**: added weekly/monthly support.
- **Default model**: changed to `deepseek-v4-pro`.

## [v0.9] - 2026-06-23

### Added
- **Phase 4 — Quote panel optimization**: batch quote queries, watchlist
  refresh throttling (30s/120s), K-line weekly/monthly, search cache warmup.
- **Phase 5 — Offline + PWA**: Service Worker, IndexedDB conversation cache,
  SSE disconnect reconnection with exponential backoff (5 retries).
- **Phase 6 prerequisites**: DB schema + seed data.

### Fixed
- **D1**: `persist_graph` now wrapped in explicit `BEGIN`/`COMMIT`/`ROLLBACK`.
- **D3**: `mmap_size` graceful fallback tests (2 new tests).
- **U4**: Mode transition fade-in animation (`@keyframes modeFadeIn`).
- **Analysis refresh**: sessionStorage state persistence + `_restoreAnalysisState()`.

## [v0.8.2] - 2026-06-20

### Fixed (production market data unavailable)
- **Root cause**: baostock ranked first in provider chain but its TCP port
  (`baostock.com:10087`) was unreachable in production; returning partial data
  with `None` for non-optional `Quote.change / change_pct` (float) triggered
  `ValidationError`. The failover chain only caught `ProviderUnavailable`, so
  the exception leaked through without falling back to akshare.
- **Fix**:
  - Provider chain now failovers on **any exception**, not just `ProviderUnavailable`.
  - K-line empty results treated as "no data from this provider", try next.
  - baostock `get_quote` normalizes missing `change/change_pct` to `0.0`.
  - Default provider order changed to `akshare,baostock` (akshare via Sina most
    stable in current network).

## [v0.8.1] - 2026-06-20

### Fixed (UI redesign finishing)
- **Icon rail**: conversation/market tabs separated into fixed 52px left column.
- **Composer controls**: 🛠 tools / model selector / send button unified to 34px height.
- **Skill drawer anchoring**: `position:absolute` pinned to button, closes on outside click.
- **Sidebar width**: both modes now 264px (removed market-mode 300px override).
- **Logo static**: Prism icon no longer toggles on click; collapse moved to ☰ button.

## [v0.8] - 2026-06-20

### Refactored
- **App skeleton**: three-column layout (icon rail + content sidebar + main area).
- **Chat cards**: messages rendered as cards; composer redone (chip + toolbar + send/stop).
- **Frontend modularization**: 1,830-line `app.js` split into `app.util.js` / `app.core.js` /
  `app.chat.js` / `app.market.js`, mounted on global `App` object.
- Default model changed from `deepseek` to `minimax-m3`.

## [v0.7.1] - 2026-06-20

### Fixed
- **baostock concurrency race**: `asyncio.Lock` serializes login → query → drain into
  atomic `_query()`, preventing session cross-talk / hangs under concurrent requests.

## [v0.7] - 2026-06-19

### Added
- **Standalone market panel**: market upgraded to first-class mode (icon rail "行情").
- **Market sidebar**: search + watchlist (localStorage, 60s batch refresh), "⊕ 自选" button.
- **Market main panel**: detail card (price/change/OHLCV) + ECharts K-line (candlestick+
  volume dual-axis, 1d/1w/1m, drag-zoom, theme-aware) + fundamentals (PE/PB/PS).
- **Cross-mode links**: `[[quote:TICKER]]` chip click → market panel; "问 AI" button →
  chat mode with pre-filled "分析一下 {ticker} {name}".

## [v0.6] - 2026-06-19

### Added
- **Market data layer**: pluggable providers (akshare/baostock), SQLite TTL cache
  (quote 60s/kline 5min/fundamentals 24h), provider failover with stale-data fallback.
- **5 endpoints**: `/api/quote/{ticker}`, `/api/quote/{ticker}/kline`,
  `/api/fundamentals/{ticker}`, `/api/search`, `/api/data-providers`.
- **Chat enhancement**: `[[quote:TICKER]]` directive in system prompt; AI output
  auto-renders real-time price chips (`.quote-chip`).
- **Market query modal**: toolbar "行情" button opens search + quote popup.

### Optional dependencies
- `akshare>=1.13,<2` and `baostock>=0.8.11,<1` moved to `requirements-extra.txt`.

## [v0.5] - 2026-05-15

### Added
- Inline report viewer (sandboxed iframe).
- Recycle bin with 7-day soft delete and permanent delete.
- Batch delete conversations.
- Glossary panel: hover term → definition; click → AI deep explanation.
- Inline reference citations from WebSearch/WebFetch results.
- Per-conversation cancel/stop button.

### Fixed
- Historical conversation display during streaming (switching mid-stream).
- Toolbar visual separation from conversation content.
- Skill HTML report presentation (sandboxed iframe viewer).
