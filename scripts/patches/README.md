# Vibe 会话迭代预算补丁

`stop004-reconciliation-candidate.patch` 已晋升到正式控制器，保留原始补丁用于审阅和历史核对。包含 `RETIREMENT_RETAINED_OBSERVER_START` 的当前源码直接接受测试，不反向套用再重放旧补丁；仅缺少该常量的旧基底可向前应用。该控制只核对固定 004 的只读保留状态，不授权停止或清理保留单元，离线测试也不构成 Linux 验收证据。

`vibe-session-iteration-budget.patch` 保留 Prism v0.19.7 使用的 Vibe sidecar 改动：从会话配置读取 `max_iterations`，无有效配置时使用 50，将预算限制在 3–50 后传入 `AgentLoop`。

核验基底为 [HKUDS/Vibe-Trading 的固定提交](https://github.com/HKUDS/Vibe-Trading/commit/d456025672ae6cff1851ba2e77ca1a8f9abdcb47)：

- 提交：`d456025672ae6cff1851ba2e77ca1a8f9abdcb47`
- 目标文件：`agent/src/session/service.py`
- 上游 Git blob：`99e4c2b24f64be195dc292073e42b4b5101477b4`
- 补丁 SHA-256：`db16c14c9c08140b3627aec9ca823fb4cdb9f366d9b493333bbf2dd11d95e51f`
- 应用后目标文件 SHA-256：`fe6899db5a55b57e8d12ea6352b4db43620e16ecb89b0a03a37c6f5f706f79bc`
- 在隔离目录执行 `git apply --check` 通过；应用后，该目标文件与已验证的生产备份逐字节一致。

应用时先在独立的 Vibe 工作目录确认基底，再使用本补丁：

```sh
git rev-parse HEAD
git apply --check /path/to/prism/scripts/patches/vibe-session-iteration-budget.patch
git apply /path/to/prism/scripts/patches/vibe-session-iteration-budget.patch
```

撤销前先检查：

```sh
git apply --reverse --check /path/to/prism/scripts/patches/vibe-session-iteration-budget.patch
git apply --reverse /path/to/prism/scripts/patches/vibe-session-iteration-budget.patch
```

本核验只证明上述目标文件与固定基底加补丁一致，不推断其他 Vibe 文件的版本。Vibe 全量源码、虚拟环境、会话、数据库和实际服务配置均保留在私有加密备份中，不导入 Prism 仓库。应用补丁不隐含启动或部署服务。
