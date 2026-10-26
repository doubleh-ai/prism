# 对话中的平台能力

> 本文记录 **旧 CLI / MCP 桥适配器** 的协议与兼容边界，不是 Native Agent 的默认执行链路。当前原生能力白名单、直接进程内调度与限制见 [Native Agent v1](native-agent-framework.md)、[功能指南](PROJECT_GUIDE.md#功能与当前限制)和[架构地图](ARCHITECTURE.md)。下文出现的 Skill、子进程凭证及回环 HTTP 仅适用于旧适配器，不能据此判断原生对话需要安装 CLI 或外部股票 Skill。

Prism 对话使用内置 `prism-research` 方法。已安装的外部研究技能仍可提供方法指导；选择了未安装的旧技能时，会明确告知并回退内置方法。未知技能不执行。

新研究进程只提供 `WebSearch`、`WebFetch`、`AskUserQuestion` 与明确列出的 Prism MCP 工具。没有任意 shell、本地文件读写、SQL、任意 HTTP、管理员、删除或部署工具。历史研究报告仍可按当前用户权限读取。

## 运行边界

应用进程为每轮已授权会话产生独立的短时随机凭证，数据库只保存其 SHA-256 摘要。凭证通过子进程环境传给 stdio 桥，不放入提示词、命令参数、MCP 配置、日志或前端事件。进程结束、取消或启动失败后，凭证被撤销；每次请求还会重新验证用户有效、会话未删除且仍属于该用户。

桥只能访问配置的数字回环地址，禁用环境代理及 HTTP 重定向。服务端同时检查回环来源、Host、Bearer 授权，拒绝 Origin 和代理转发头。请求照常携带现有 CSRF 标记，没有新增 CSRF 豁免。

`--strict-mcp-config` 只加载本次配置的 `prism` 服务；`--setting-sources ""` 不加载用户、项目或本地 settings；`--tools` 限制内置工具，`--allowedTools` 列出同样的三项内置工具和精确的 Prism 工具名称。技能指导以一份合并的系统补充提示传入。凭证安全仍以受信任的应用主机和 CLI 为前提；这些约束不等同于操作系统沙箱。

| 服务端配置 | 默认值 | 用途 |
| --- | --- | --- |
| `PRISM_INTERNAL_API_URL` | `http://127.0.0.1:8088` | 应用自身监听地址，必须与实际端口一致；桥仅接受 HTTP 数字回环地址 |
| `PRISM_CAPABILITY_TOKEN_TTL` | `1800` | 凭证有效秒数，限制在 60–3600 秒 |

`PRISM_CAPABILITY_URL` 与 `PRISM_CAPABILITY_TOKEN` 是 runner 为每个子进程设置的内部环境变量，不是需要保存的部署凭证。应用采用其他端口时必须相应设置 `PRISM_INTERNAL_API_URL`。

## 能力与任务

| 工具 | 页面 | 行为 |
| --- | --- | --- |
| `market_quote/fundamentals/kline/search` | market | 查询行情、基本面、K 线和标的 |
| `indicator_list/eval` | indicators | 读取当前用户指标并实际执行指定指标 |
| `scan_start/status` | screener | 发起自选或全市场扫描，查询已有结果 |
| `industry_overview/chain/context` | industry | 查询现有图谱与已有研究关联 |
| `report_list/get` | reports | 读取当前用户已保存的研究 |
| `analysis_start/status` | analysis | 创建 Astock 任务、读取已有记录 |
| `vibe_start/status` | vibe | 创建 Vibe 任务、读取已有记录 |

三个 `*_start` 工具会创建任务，标记 `read_only=false`，仅用于用户请求的扫描或研究。后台任务在应用进程运行，返回记录后不依赖 stdio 桥继续存在。点击调用结果只打开已有记录，不会重复发起。调用请求在每轮内有幂等标识，同一请求重复提交不会重复创建任务。进程重启后的 Astock 记录仍可读，未完成记录明确标记中断而不是自动重跑。

所有参数使用封闭 schema，不接受用户名、会话 cookie、URL、SQL、命令或文件路径。记录 ID 也需经过拥有者校验。每轮最多 100 次调用，启动任务另有按用户限流。单次调度有超时，结果大小受限；完整结果通过目标页面查看。

## 前端合同

- `GET /api/capabilities` 返回 `capabilities`：`id`、`label`、`description`、`input_schema`、`source_kind`、`view`、`target_view`、`read_only`。
- `GET /api/convs/{conv_id}/capability-calls` 返回当前用户拥有的会话调用历史。
- `GET /api/capability-calls?view=market&limit=20` 返回当前用户在指定能力页的跨会话记录；不传 `view` 则返回所有能力。
- runner 发出 `{type:'platform_call',call:record}`。聊天转为 SSE `{platform_call:record}`，历史保存为 `{t:'platform_call',call:record}`。同一 `call_id` 的运行态与终态需更新同一条记录。

公开记录包含 `call_id/run_id/conv_id/capability_id/label/status/created_at/completed_at/input_summary/result_summary/source_kind/target`。`target` 是 `{view,id?,ticker?}`；时间戳为 Unix 秒。输入摘要不持久化任意原始提示词，结果先做凭证字段过滤和大小限制。

桥端成功响应为 `{ok:true,call,data,source_kind,evidence_status}`，失败包含 `{ok:false,call,error:{code,message}}`。通常失败时 `data` 为空；若引擎不可用但已创建失败记录，返回该记录和可打开的 `target`。错误分类包括 `invalid_arguments/not_found/unavailable/conflict`；认证、非法 schema、限流等 HTTP 错误由桥转为安全的工具失败，不转发内部异常体。

## 研究证据

每轮使用 `research_context` 中的新 `as_of` 与 `Asia/Shanghai` 时间。研究时间不能代替行情交易时间、数据所属期间或来源发布时间。

图谱工具标记 `source_kind=curated_unverified`：已有结构是待核验背景，不能仅凭种子连线断言商业供货、市占率或置信度。报告工具标记 `source_kind=prior_research`：历史观点不是当前外部独立验证。其他平台结果也统一标记 `platform_result_not_external_verification`，应保留其来源和数据时间，再寻找适合问题的原始证据。

## 协议依据与验证范围

实现依据 [Claude CLI 官方参数](https://code.claude.com/docs/en/cli-reference)、[MCP stdio 传输](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) 和 [MCP 生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)。桥支持初始化、通知、ping、工具枚举与工具调用，不提供 resources/prompts 或其他操作。

自动测试使用临时数据库、模拟外部引擎与进程，不调用真实模型或生产服务。覆盖权限隔离、失效与撤销、幂等任务发起、真实 DSL 计算、持久结果读取、来源标记、协议格式和凭证不进入参数/事件。实际 CLI 版本和部署监听端口仍需在部署验收时验证，不能以模拟通过声称真实模型调用已完成。
