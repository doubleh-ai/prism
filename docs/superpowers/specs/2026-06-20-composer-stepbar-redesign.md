# Prism v0.8.3 — Composer 扁平化 + 步骤条优化 + Markdown 乱码修复

## 范围

三个独立但同在 `static/css/app.css` / `static/js/app.chat.js` 里的改动，
一次部署一起上。

## 1. Composer 控件 → 胶囊/Chip 扁平风格

**目标：** 把 `.composer-bar` 内的 🛠 按钮、模型 select、发送按钮从当前的
8px 圆角 + 明显边框改为统一的胶囊（pill）风格——全圆角、微妙填充、细边线、
更"软"更扁平的视觉。

### CSS 改动（`static/css/app.css`）

| 选择器 | 属性 | 旧值 | 新值 |
|---|---|---|---|
| `.cmp-btn` | `border-radius` | `8px` | `14px` |
| `.cmp-btn` | `border` | `1px solid var(--border)` | `1px solid rgba(var(--border-rgb),0.12)` |
| `.cmp-btn` | `background` | `var(--bg)` | `rgba(var(--text-rgb),0.04)` |
| `.cmp-btn:hover` | `background` | `var(--bg3)` | `rgba(var(--text-rgb),0.08)` |
| `.composer-bar select` | `border-radius` | `8px` | `14px` |
| `.composer-bar select` | `border` | `1px solid var(--border)` | `1px solid rgba(var(--border-rgb),0.12)` |
| `.composer-bar select` | `background` | `var(--bg2)` | `rgba(var(--text-rgb),0.04)` |
| `.cmp-send` | `border-radius` | `8px` | `14px` |
| `.cmp-send` | `width` | `36px` | `34px` |

**注意：** `rgba(var(--border-rgb))` 不存在于当前变量体系。改用当前 theme 里已有的
半透明色值：light → `rgba(28,26,22,0.08)`、dark → `rgba(255,255,255,0.06)`。
或者直接保留 `var(--border)` 等现有 token 但**减细边框**（`border-color` 不变，
减小 `border-width` 或降低 opacity）——这样改动最小且效果类似。

**推荐方案（最简、不引入新 token）：**
- 按钮/select 的 `border-color` 从 `var(--border)` 改为 `rgba(255,255,255,0.06)`（dark）/
  `rgba(28,26,22,0.06)`（light）。这些值已存在于现有 CSS 中（sidebar 用了 `rgba(255,255,255,.06)`）。
- `border-radius:14px` 全覆盖。
- `background` 保持在 light/dark 下语义一致（`var(--bg)` / `var(--bg2)` 已足够微妙）。
- 发送按钮仅改 border-radius 到 14px，宽 34px 保持不变（已对齐）。

## 2. 步骤条自动折叠 + 单行截断

**目标：**
- 已完成步骤 ≥ 3 个时自动折叠为 `✓ N 步`（复用现有的 `details.step-fold`），
  点击展开才看到完整列表。
- ▶ 当前正在执行的步骤始终在折叠**外部**，不参与折叠。
- ▶ 步骤的标签（`.step-tag`）全部改为**自然宽度**（`flex:0 0 auto`），不再撑满行。
- ▶ 标签内的命令文本（`<i>`）采用**单行截断**（`white-space:nowrap + text-overflow:ellipsis`），
  不再折行，不再使用 `-webkit-line-clamp:2`。

### CSS 改动

| 选择器 | 属性 | 旧值 | 新值 |
|---|---|---|---|
| `.step-tag` | `flex` | 无 | `flex:0 0 auto` |
| `.step-tag i` | `white-space` | `nowrap` | **保持** |
| `.step-tag i` | `max-width` | `420px` | `300px`（更窄以防截断感知明显） |
| `.step-tag.now` | `flex` | `1 1 auto` | `0 0 auto` |
| `.step-tag.now` | `max-width` | `100%` | `100%`（保持，但要配合单行截断） |
| `.step-tag.now i` | `display` | `-webkit-box` | **移除** |
| `.step-tag.now i` | `-webkit-line-clamp` | `2` | **移除** |
| `.step-tag.now i` | `word-break` | `break-all` | **移除** |
| `.step-tag.now i` | `white-space` | - | `nowrap` |
| `.step-tag.now i` | `overflow` | `hidden` | **保持** |
| `.step-tag.now i` | `text-overflow` | - | `ellipsis` |
| `.step-tag.now i` | `max-width` | `100%` | `100%` |

### JS 改动（`static/js/app.chat.js`）

在流式渲染步骤的逻辑中（`_renderStepBar` 或等效函数），在渲染步骤标签之后增加折叠逻辑：

```javascript
// 伪代码
function wrapCompletedSteps(stepTags, nowTag) {
  // steps 数组里除了最后一个 now 以外的全部
  var completed = stepTags.slice(0, -1);
  if (completed.length >= 3) {
    var fold = document.createElement('details');
    fold.className = 'step-fold';
    fold.innerHTML = '<summary>✓ ' + completed.length + ' 步</summary>' +
      completed.map(function(t) { return t.outerHTML; }).join('');
    return [fold, nowTag];  // fold 在外，now 在右边
  }
  return stepTags;  // < 3，平铺
}
```

**关键约束：**
- 折叠仅在 ≥ 3 步时触发，< 3 步时保持原样（不引入多余的折叠 UI）。
- 新的一轮工具执行开始时（新 step-tag.now 出现），旧的 now 变为 completed，
  自动收进折叠。
- 折叠展开/收起不影响 `<step-fold[open]>` 现有的 CSS（已有）。

## 3. Markdown `>` 误渲染为 blockquote

**现象：** AI 输出中 `>供应链` / `> 某环节` 等包含前导 `>` 的文本行，
被 marked.js 解析为 HTML `<blockquote>`，显示异常。

**根因：** marked.js 的 GFM 模式下，行首 `>` 后跟空格或字符都被视为 blockquote 语法。

**修复：** 在 `sanitizeHtml()` 或消息渲染前（`static/js/app.core.js` 或 `app.chat.js` 中
调用 `marked.parse()` 的位置），对所有非代码块的行首 `>` 进行转义：

```javascript
function escapeLeadingGt(text) {
  // 把行首的 > 转义为 &gt;，防止 marked 当成 blockquote
  // 但跳过：行内 >（如 "A > B"）、反引号内的内容
  return text.replace(/^(>[>\s]*)/gm, function(match) {
    return match.replace(/>/g, '&gt;');
  });
}
```

这个转义放在 `marked.parse()` 之前、`sanitizeHtml()` 之后，
或者集成为 marked 的自定义 renderer。最简方案：marked 配置中
覆盖 blockquote renderer 使其不渲染，但那会禁掉真正的 blockquote。

**更精确的方案：** 覆盖 marked 的 `blockquote` tokenizer，检查
`>` 后面是不是中文 —— 中文紧接 `>` 说明是内容而非标记。但这个规则
太 heuristic。

**最稳妥的方案：** 在调用 `marked.parse(text)` 之前，预处理文本 ——
把行首 `>` 替换为 `&gt;`。blockquote 的视觉效果可以用 weasy/chat-gpt
风格自定义（很少用到真正 blockquote）。如果用户以后需要 blockquote，
可以通过 `>` + 空格 + 英文首字母的 pattern 区分。当前一律转义，
因为 AI 输出中的 `>` 几乎全是列表/箭头标记，不是 blockquote。

## 实现顺序

1. `app.css` — composer 胶囊 style
2. `app.css` — step-bar 单行截断 + 自然宽度
3. `app.chat.js` — 步骤自动折叠（≥3）JS 逻辑
4. `app.core.js` 或 `app.util.js` — `escapeLeadingGt()` + 在渲染管道中接入
5. `pytest` — 跑全量确认无回归
6. 前端仅部署（无需重启服务）

## 不在此版范围

- 移动端适配这些改动
- 步骤折叠的动画（CSS transition 留待后续）
- 全面 markdown 渲染器替换
