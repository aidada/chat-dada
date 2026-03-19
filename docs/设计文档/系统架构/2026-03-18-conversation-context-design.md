# 设计文档：对话内上下文记忆（混合方案 B+C）

## 背景

当前系统每次任务独立处理，LLM 只收到当前消息 + 用户长期画像（MemoryStoreV2），不包含同一对话的历史问答。用户在同一对话内的多轮交互无法关联上下文。

### 与现有记忆系统的关系

| 系统 | 定位 | 存储 | 内容 |
|------|------|------|------|
| MemoryStoreV2（已有） | 跨对话用户画像 | 文件 JSON | 结构化事实、项目状态 |
| 对话上下文（本设计） | 对话内多轮记忆 | PostgreSQL + pgvector | 摘要 + 原文片段 |

两者互补：`memory_context`（你是谁）+ `conversation_context`（聊了什么）一起注入 prompt。

### 业界参考

- **Manus**：分级压缩（无损 compact → 有损 summarize），最近 3 轮保留原文维持模型节奏感
- **MemGPT/Letta**：三层虚拟内存（核心/召回/归档），agent 自管理
- **LangChain**：ConversationSummaryBufferMemory，按 token 阈值触发摘要
- **AutoGPT**：纯向量检索存在 3.2x 幻觉放大风险，不宜单独使用
- **共识**：混合（摘要 + 近期原文 + 可选向量检索）是业界收敛方向

## 方案概述

三阶段渐进策略，根据对话长度自动切换：

```
对话 ≤ 5 轮  →  全部原文（零开销）
对话 6-20 轮 →  滚动摘要 + 最近 3 轮原文
对话 > 20 轮 →  滚动摘要 + 最近 3 轮原文 + pgvector 检索 top-3 补充
```

## Token 预算

基于 256K tokens 上下文窗口（GPT-5.4 / Claude Opus 4.6），预留 10K 安全余量，总可用 246K。

### 分配原则：优先级驱动，动态计算

预算不是固定值，而是按优先级顺序扣减。1-3 是刚性的（实际多少占多少），4 是弹性的（有上限），5 吃剩余。

```
可用 = 246K

① 系统提示 + 工具定义  → 实际值（~8K，不可压缩）      → 可用 -= 实际值
② 当前用户输入         → 实际值（不可截断，可能 1-20K） → 可用 -= 实际值
③ 用户记忆             → 实际值（通常 1-2K，上限 4K）   → 可用 -= 实际值
④ 对话上下文           → min(实际需求, 8K, 可用 * 5%)   → 可用 -= 实际值
⑤ Agent 工作区         → 全部剩余
```

### 示例

| 场景 | 用户输入 | 对话上下文 | Agent 工作区 |
|------|---------|-----------|-------------|
| 普通提问 | 0.5K | ≤ 8K | ~228K |
| 贴了一段代码 | 10K | ≤ 8K | ~218K |
| 贴了整篇论文 | 50K | ≤ 8K | ~178K |
| 极端长输入 | 100K | ≤ 7.3K (=剩余*5%) | ~130K |

极端情况下（用户输入 > 100K），对话上下文自动收缩至 `剩余 * 5%`，保证 Agent 工作区不被挤压。

### 对话上下文内部分配（≤ 8K）

- 滚动摘要：≤ 3K tokens（~4500 字）
- 近期原文（3 轮）：≤ 3K tokens（每轮 ~1K）
- 向量检索补充（3 条）：≤ 2K tokens（每条 ~650 字截断）

## 数据库变更

### conversations 表新增字段

```sql
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS context_summary TEXT DEFAULT '';
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary_through_seq INTEGER DEFAULT 0;
```

- `context_summary`：当前滚动摘要文本，缓存避免重复生成
- `summary_through_seq`：摘要覆盖到的最后一条 event seq，用于增量更新

### task_events 表新增 embedding 列

```sql
-- 需要先安装 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE task_events ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_task_events_embedding
  ON task_events USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

仅对 `user`、`result`、`error` 类型的事件生成 embedding，其他类型（start、step、monitoring）跳过。

## 核心模块：ConversationContextBuilder

新增 `runtime/conversation_context.py`：

```python
class ConversationContextBuilder:
    """构建对话上下文，注入 LLM prompt。"""

    MAX_CONTEXT_TOKENS = 8000
    SUMMARY_TOKENS = 3000
    RECENT_ROUNDS = 3
    RETRIEVAL_TOP_K = 3
    REFERENTIAL_MARKERS = {
        "刚才", "之前", "上面", "上次", "那个", "这个",
        "继续", "接着", "结合", "综合", "对比", "类似",
        "还是", "同样", "也", "再",
        "it", "that", "previous", "earlier", "above",
    }

    async def build_context(
        self,
        conversation_id: str,
        current_message: str,
        store: TaskRunStore,
    ) -> str:
        """返回格式化的对话上下文字符串，直接注入 prompt。"""
```

### 构建流程

```
build_context(conversation_id, current_message)
  │
  ├─ 1. 查询该对话所有 primary entries（user/result/error）
  │
  ├─ 2. 计算对话轮数
  │     ├─ ≤ 5 轮 → format_all_raw(entries)，直接返回
  │     └─ > 5 轮 → 继续
  │
  ├─ 3. 提取最近 3 轮原文
  │
  ├─ 4. 获取/更新滚动摘要
  │     ├─ 检查 conversation.summary_through_seq
  │     ├─ 如果有新 entries → 增量摘要（旧摘要 + 新内容 → LLM 生成新摘要）
  │     └─ 缓存到 conversation.context_summary
  │
  ├─ 5. 判断是否需要向量检索（仅对话 > 20 轮时）
  │     ├─ 检测引用性标记词 / 消息长度 < 15 字
  │     ├─ 命中 → embed(current_message) → pgvector 检索 top-3
  │     └─ 未命中 → 跳过检索
  │
  └─ 6. 组装 & 截断到 MAX_CONTEXT_TOKENS → 返回
```

### 输出格式

```
[对话历史摘要]
用户先询问了 GNSS 在自动驾驶中的定位精度问题，了解到 RTK 可达厘米级。
随后讨论了车规级传感器融合方案，重点关注 LiDAR+Camera+GNSS/IMU 架构。

[相关历史片段]
Q: GNSS 单点定位和 RTK 的精度差异？
A: 单点定位 2-5m，RTK 通过载波相位差分可达 1-2cm...

[最近对话]
Q: 车规级自动驾驶有哪些传感器融合方案？
A: 主流方案包括 LiDAR+Camera+GNSS/IMU 紧耦合...

Q: 传感器融合中 GNSS 失锁怎么处理？
A: 城市峡谷场景下 GNSS 失锁时，依赖 IMU 惯性推算 + 视觉里程计...
```

## 滚动摘要策略

### 增量更新

不是每次重新摘要全部历史，而是：

```python
新摘要 = LLM(旧摘要 + 自上次摘要后的新 entries)
```

摘要 prompt：

```
请将以下对话历史压缩为简洁摘要，保留：
1. 每个话题的核心结论
2. 关键数据和事实
3. 用户的具体需求和偏好
删除：寒暄、重复内容、中间推理过程。
控制在 500 字以内。

[现有摘要]
{existing_summary}

[新增对话]
{new_entries}
```

### 摘要触发时机

不是每条消息都触发，而是：
- 对话超过 5 轮 **且** 自上次摘要后有 ≥ 3 轮新对话
- 或 build_context 时发现 `summary_through_seq` 落后当前 seq 超过 6 条

### 摘要缓存

生成的摘要写入 `conversations.context_summary`，同时更新 `summary_through_seq`。下次 build_context 如果没有新对话，直接用缓存，零开销。

## 向量检索策略

### Embedding 生成时机

- 任务完成时（`finish_task` 后），对该任务的 `user`、`result`、`error` 类型 events 批量生成 embedding
- 使用 `text-embedding-3-small`（1536 维），成本 $0.02/1M tokens
- 异步后台处理，不影响响应延迟

### 检索流程

```python
async def retrieve_relevant(self, conversation_id, query, top_k=3):
    query_embedding = await embed(query)
    rows = await store.pool.fetch("""
        SELECT e.event_type, e.payload, e.created_at,
               e.embedding <=> $1 AS distance
        FROM task_events e
        JOIN task_runs t ON t.task_id = e.task_id
        WHERE t.conversation_id = $2
          AND e.embedding IS NOT NULL
          AND e.event_type IN ('user', 'result', 'error')
        ORDER BY e.embedding <=> $1
        LIMIT $3
    """, query_embedding, conversation_id, top_k)
    return rows
```

### 检索触发条件

仅在以下条件**全部满足**时触发：
1. 对话超过 20 轮
2. 消息命中引用性标记词 **或** 消息长度 < 15 字
3. 摘要已存在（确保不会在无摘要时单独用检索）

## 智能跳过策略

### 何时完全跳过上下文构建

| 条件 | 处理 |
|------|------|
| 对话首条消息 | 跳过，返回空字符串 |
| 对话 ≤ 2 轮且当前消息自含 | 仅带最近 1 轮，不做摘要不做检索 |

### 引用性检测

```python
def _needs_retrieval(self, message: str) -> bool:
    msg_lower = message.lower()
    if len(message) < 15:
        return True
    return any(marker in msg_lower for marker in self.REFERENTIAL_MARKERS)
```

### 过滤噪音

从 task_events 取数据时，只取 `user`、`result`、`error` 类型，跳过：
- `start`（"开始执行: ..."）
- `step`（中间推理步骤）
- `monitoring`（性能监控数据）
- `result_delta`（流式增量片段）
- `question` / `user_reply`（agent 追问，通常是过程性的）

每条 entry 的 content 截断到 **400 字**，避免单条结果（如长篇研究报告）撑爆预算。

## 注入位置

在 `_execute_task` 中，调用 agent 之前构建上下文：

```python
# runtime/task_runtime.py - _execute_task 中
context_builder = ConversationContextBuilder()
conversation_context = await context_builder.build_context(
    conversation_id=conversation_id,
    current_message=task_text,
    store=self._store,
)

# 传给 agent
result = await decision.executor(
    execution_task,
    on_step,
    user_id=user_id,
    memory_context=memory_context,           # 用户画像（已有）
    conversation_context=conversation_context, # 对话上下文（新增）
)
```

各 agent 在构建 messages 时注入：

```python
messages = [
    SystemMessage(content=system_prompt),
    *([SystemMessage(content=memory_context)] if memory_context else []),
    *([SystemMessage(content=conversation_context)] if conversation_context else []),
    HumanMessage(content=task),
]
```

## 修改文件清单

| 文件 | 变更 |
|------|------|
| `scripts/init.sql` | conversations 表加 context_summary、summary_through_seq；安装 pgvector；task_events 加 embedding 列 |
| `runtime/conversation_context.py` | **新建**，ConversationContextBuilder 核心逻辑 |
| `runtime/task_runtime.py` | `_execute_task` 中调用 context_builder；`finish_task` 后触发异步 embedding 生成 |
| `agents/general_chat.py` | 接收 conversation_context 参数，注入 messages |
| `orchestrator/runner.py` | 接收 conversation_context 参数，传递给 planner 和各 handler |
| `orchestrator/planner.py` | classify_and_plan 接收 conversation_context |
| `requirements.txt` | 添加 pgvector 依赖 |

## 成本估算

| 操作 | 频率 | 成本 |
|------|------|------|
| Embedding 生成 | 每个任务完成后 2-3 条 | ~$0.001/次 |
| 滚动摘要 LLM 调用 | 每 3-5 轮一次（缓存） | ~$0.005/次 |
| 向量检索 | 仅长对话 + 引用性消息 | 数据库查询，无 API 成本 |
| **总计** | 一个 20 轮对话 | ~$0.03（相比对话本身的 LLM 调用可忽略） |

## 后续扩展

1. **跨对话知识关联** — 向量检索范围从单对话扩展到用户所有对话，WHERE 条件改为 user_id
2. **摘要质量评估** — 对比有无摘要时的回答质量，调优摘要 prompt
3. **自适应预算** — 根据当前 agent 路径（chat vs orchestrator）动态调整上下文预算
