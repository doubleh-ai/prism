# Prism — 技能依赖声明

## 核心技能

| 技能 | 仓库 | 锁定版本 |
|---|---|---|
| **human-readable-ai** ⭐ | https://github.com/Huadous/human-readable-ai | `08aa873` |
| serenity-bottleneck-hunter | https://github.com/Mrjie7205/serenity-bottleneck-hunter | `c480f6d` |
| serenity-skill | https://github.com/ZadAnthony/serenity-skill | `ab7fb2c` |

> ⭐ **human-readable-ai** 是所有分析的**必备基础技能**，通过系统提示词自动注入，确保输出排版规范（Markdown 有序列表、标题层次、表格、加粗重点等），禁止使用 ①②③ 等内联序号。

```bash
# human-readable-ai — 中文 AI 输出可读性格式化
git clone https://github.com/Huadous/human-readable-ai.git ~/.claude/projects/human-readable-ai
cd ~/.claude/projects/human-readable-ai && git checkout 08aa873
mkdir -p ~/.claude/skills/human-readable-ai
cp ~/.claude/projects/human-readable-ai/CLAUDE.md ~/.claude/skills/human-readable-ai/

# 安装分析技能
git clone https://github.com/Mrjie7205/serenity-bottleneck-hunter.git ~/.claude/skills/serenity-bottleneck-hunter
cd ~/.claude/skills/serenity-bottleneck-hunter && git checkout c480f6d

git clone https://github.com/ZadAnthony/serenity-skill.git ~/.claude/skills/serenity-skill
cd ~/.claude/skills/serenity-skill && git checkout ab7fb2c

# human-readable-ai 安装见上方，无需额外克隆

# 升级（测试通过后更新上面的 commit hash）
cd ~/.claude/projects/human-readable-ai && git pull && git rev-parse --short HEAD
cd ~/.claude/skills/serenity-bottleneck-hunter && git pull && git rev-parse --short HEAD
cd ~/.claude/skills/serenity-skill && git pull && git rev-parse --short HEAD
```

## 可选技能（金融数据）

```bash
# Vibe-Trading — yfinance + akshare + mootdx（77 skills）
npx skills add https://github.com/HKUDS/Vibe-Trading

# claude-for-financial-services-cn — A 股专业分析（63 skills）
claude plugin marketplace add jwangkun/claude-for-financial-services-cn
```

## 全局语言规范

1. **中文标点** — 使用 ，。、；：""''！？ 禁止英文标点
2. **概念解释** — 新术语先给简明定义再展开
3. **排版美观** — 标题层次分明，表格标注单位，关键结论加粗
4. **回复风格** — 先结论后分析，数据优先于观点

## 项目内置技能

| 技能 | 目录 | 用途 |
|------|------|------|
| **force-chinese** | `skills/force-chinese/` | 强制中文输出，确保 AI 始终使用简体中文回复 |

```bash
# 部署到 Claude Code 技能目录
cp skills/force-chinese/CLAUDE.md ~/.claude/skills/force-chinese/
```
