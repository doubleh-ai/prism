## 变更摘要

<!-- 简要说明本次 PR 解决的问题或新增的能力 -->

## 关联 Issue

Closes #

## 变更类型

- [ ] Bug 修复
- [ ] 新功能
- [ ] 文档 / 规范更新
- [ ] 重构 / 性能优化

## 验证清单

- [ ] 已运行 `python3 -I -S -B scripts/check_provider_key_isolation.py`
- [ ] 已运行 `python -m ruff check server/ tests/` 与 `python -m ruff format --check server/ tests/`
- [ ] 已补充或更新自动化测试并验证通过
- [ ] 若涉及前端静态资源变更，已同步更新资源版本号并通过 `tests/test_asset_coherence.py`
- [ ] 已确认不包含任何凭证、私有主机地址、运行数据库或真实用户数据
