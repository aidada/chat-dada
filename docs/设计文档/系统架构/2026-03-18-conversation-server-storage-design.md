# 设计文档：对话数据服务端持久化

## 背景

当前对话数据完全存在浏览器 localStorage，换设备/清缓存即丢失。需将对话元数据存到 PostgreSQL，消息内容复用已有的 task_runs + task_events。

## 数据库变更

### 新增 conversations 表

```sql
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '新对话',
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
```

### task_runs 表新增字段

```sql
ALTER TABLE task_runs ADD COLUMN IF NOT EXISTS conversation_id TEXT;
CREATE INDEX IF NOT EXISTS idx_task_runs_conversation ON task_runs(conversation_id);
```

不存 entries/消息内容 — 消息已在 task_events 里，通过 task_runs 关联。conversations 只存元数据。

## API 设计

### GET /conversations?user_id=xxx

返回用户所有对话，服务端排序（pinned 优先 + updatedAt 降序）。每条包含 preview（最新 task_text 前 60 字）和 lastTaskId。

```json
[
  {
    "id": "conv_xxx",
    "title": "关于量子计算的研究",
    "pinned": true,
    "created_at": "...",
    "updated_at": "...",
    "last_task_id": "task_xxx",
    "preview": "帮我搜索一下最新的量子计算论文..."
  }
]
```

### POST /conversations

```json
// Request
{ "user_id": "xxx", "title": "新对话" }
// Response (201)
{ "id": "conv_xxx", "title": "新对话", "pinned": false, "created_at": "...", "updated_at": "..." }
```

### PATCH /conversations/{id}

```json
// Request (部分更新)
{ "title": "新标题" }
// 或
{ "pinned": true }
// Response (200) — 返回完整对话对象
```

### DELETE /conversations/{id}

```
Response (204 No Content)
```

### GET /conversations/{id}/entries

返回该对话下所有 task 的 events，按时间排序，格式对齐前端现有 entry 结构：

```json
[
  { "id": "evt_1", "type": "step", "content": "...", "render": "text", "createdAt": "..." },
  { "id": "evt_2", "type": "result", "content": "...", "render": "markdown", "createdAt": "..." }
]
```

聚合逻辑：查 task_runs WHERE conversation_id = ? → 查 task_events WHERE task_id IN (...) ORDER BY created_at, seq。

### POST /tasks 扩展

新增可选参数 `conversation_id`，创建 task 时关联到对话，并更新 conversations.updated_at。

## 前端改造

### 删除

- localStorage 中 conversations 的读写（CONVERSATIONS_KEY）
- normalizeConversation() 中的本地 entries 管理
- 前端 sortConversations()（服务端排序）

### 新增

- `fetchConversations(userId)` — GET /conversations，结果赋给 conversations 数组
- `fetchConversationEntries(convId)` — GET /conversations/{id}/entries
- 创建/更新/删除对话改为调 API
- 页面加载 await fetchConversations() 替代 loadConversations()

### 保留

- SSE 实时事件流
- 搜索/置顶/重命名 UI 交互（底层换成 API）
- renderConversationList() / renderActiveConversation() 渲染逻辑

## 前端数据流

```
页面加载 → GET /conversations → 渲染列表
点击对话 → GET /conversations/{id}/entries → 渲染消息
发送消息 → POST /tasks (带 conversation_id) → SSE 流式接收
重命名   → PATCH /conversations/{id} {title} → 刷新列表
置顶     → PATCH /conversations/{id} {pinned} → 刷新列表
删除     → DELETE /conversations/{id} → 刷新列表
```
