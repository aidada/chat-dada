# 设计文档：Agent 进度看板（浮动卡片）

## 背景

用户在发送消息后看不到 agent 在后台做什么，只能等最终结果。需要在聊天流中展示一个实时进度卡片，让用户知道当前执行到哪一步。

## 方案：纯前端实时流式看板

**不需要修改后端。** 复用已有的 `step` SSE 事件，在前端渲染为浮动进度卡片。

### 卡片生命周期

1. 收到 `start` 事件 → 在 `.log-body` 末尾插入空卡片
2. 每收到 `step` 事件 → 上一条标为 ✓，新增一条 → 行
3. 收到 `result` / `error` / `result_delta` → 卡片淡出消失（400ms 后移除 DOM）

### DOM 结构

```html
<div class="agent-progress" data-task-id="task_xxx">
  <div class="progress-header" onclick="toggleProgressCard(this)">
    <span class="progress-icon">⚙️</span>
    <span class="progress-title">正在处理中...</span>
    <span class="progress-toggle">▾</span>
  </div>
  <div class="progress-body">
    <div class="progress-step done">✓ 分析问题和意图</div>
    <div class="progress-step active">→ web_search: Caddy vs Nginx</div>
  </div>
</div>
```

### 样式

- 左边框 `3px solid var(--peach)` 强调
- 背景 `var(--warm0)`，与聊天气泡视觉层级区分
- 已完成步骤 `var(--dim)` 灰色，当前步骤 `var(--text)` 深色
- 最多显示最近 5 条步骤，更早的自动隐藏
- 消失动画：opacity 淡出 + max-height 收缩

### JS 核心函数

| 函数 | 触发时机 | 作用 |
|------|----------|------|
| `createProgressCard(taskId)` | `start` 事件 | 创建空卡片插入聊天流 |
| `appendProgressStep(taskId, content)` | `step` 事件 | 追加步骤行，上一步标完成 |
| `removeProgressCard(taskId)` | `result`/`error`/`result_delta` 事件 | 淡出移除卡片 |
| `toggleProgressCard(header)` | 点击卡片标题 | 展开/收起步骤列表 |

### 集成点

仅修改 `static/index.html`，在已有的 `persistAndRenderEvent()` 函数中按事件类型调用上述函数。

### step 文本处理

- 直接复用后端 `step` 事件的 `content` 字段
- 截断至 50 字符以内
- 移除 emoji 前缀（如 🧠🔍✅），用 ✓/→ 替代

## 修改文件

| 文件 | 操作 |
|------|------|
| `static/index.html` | CSS 样式 + JS 函数 + 事件处理集成 |

## 验证

1. 发送一个任务，观察卡片是否在 start 后出现
2. 验证 step 事件逐条追加到卡片
3. 验证结果到达后卡片淡出消失
4. 验证点击标题可展开/收起
5. 验证快速连续 step 不会卡顿
6. 验证聊天区域自动滚动到底部
