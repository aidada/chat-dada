# chat-dada 架构演进：向 Anthropic Managed Agents 三层解耦看齐（v3.2）

> 日期：2026-04-10
> 版本：v3.2（基于代码审计修正现状描述 + 补齐隐藏工作项）
> 基线：v3.1（以职责拆分重构为主线，补齐稳定接口设计）
> 目标：继续推进 Brain 纯编排 / Hands 纯执行 / Session 独立组件，同时对齐 Anthropic 原文强调的 Session / Harness / Hands 稳定接口模型

> **v3.2 相对 v3.1 的主要修正**：
> 1. Section 3 组件图和问题清单全面基于代码审计修正（TaskRunStore 实际使用 SessionFactory、工具导入仅在 research 域的 worker.py/tools.py）
> 2. 识别并补充**双事件流问题**（`_safe_emit` 仅写 stream vs `record_event` 写 DB+Redis，20+ 处散装副本）
> 3. Phase 1 新增 Step 1.5：`_dag_resume_state` 手动恢复与 checkpoint 恢复的冲突清理
> 4. Phase 2 新增 Step 2.3b（9 个 Conversation 方法独立安置）+ Step 2.8（`_safe_emit` → `emit_progress` 统一迁移）
> 5. Phase 3 改动范围从“只修 `research/worker.py` + `tools.py`”扩展为“research 直调工具 + patent/zero_report/ppt 的 deepagents tool 绑定 + Tauri 双端 transport”

---

## 一、愿景与背景

Anthropic 在《Scaling Managed Agents: Decoupling the brain from the hands》里的核心思想，不只是把系统拆成 Brain、Hands、Session 三层，而是进一步强调：

1. 不要把当前 harness 的假设硬编码进系统。
2. 要让 Session、Harness、Hands 之间只通过极简稳定接口通信。
3. 要让未来的模型、未来的 harness、未来的执行环境都能独立替换。

为了保持文档可读性，本文仍继续使用 **Brain（大脑）/ Hands（双手）/ Session（会话）** 这组更直观的术语；但在实现语义上，需要明确：

- **Brain**：更准确地说，是 **Claude/LLM + Harness**，即 TaskService、Coordinator、DAG 选择的 domain skill runners 这一整层纯编排逻辑。
- **Hands**：执行环境与工具执行器，负责真正落地动作。
- **Session**：唯一的 durable state boundary，负责保存可恢复历史，而不是保存某一版 harness 的临时内存。

### 1.1 为什么 v3.1 仍以“职责拆分重构”为主线

这份文档的主线仍然是职责拆分，因为 chat-dada 当前最现实的问题，仍然是三类职责混在一起：

- Brain 混入了 Session 写入职责
- Brain 混入了 Hands 工具执行职责
- Session 还没有成为真正独立的状态边界

但 v3.1 不再只谈“谁负责什么”，还会补进 Anthropic 原文更关键的一层：

- 这些职责拆开以后，**层与层之间到底通过什么稳定接口通信**
- 哪些东西是 **canonical state**
- 哪些东西只是 **projection / cache / transport**

### 1.2 Anthropic 原文与 chat-dada 的映射

| Anthropic 原文 | 本文易读术语 | chat-dada v3.1 对应目标 |
|----------------|-------------|-------------------------|
| Claude + Harness | Brain | 无状态纯编排运行时 |
| Hands / Sandbox | Hands | 可替换执行端：本地桌面 Hands + 未来远程 ephemeral Hands |
| Session | Session | durable event log + checkpoint + projection + wake/recovery |

### 1.3 本文的一个有意偏离

Anthropic 原文里的 Hands 更偏向 **ephemeral sandbox**：临时 provision、用完即弃、失败可重建。

chat-dada 当前必须支持 **Tauri 本地桌面能力**，这意味着：

- 我们会保留一个 **Local Desktop Hands** 变体
- 它不是 Anthropic 原文中 cattle-style sandbox 的同构实现
- 但它仍需要遵守相同的稳定 hand contract

换言之，**chat-dada 要对齐的是 Anthropic 的接口哲学，不是逐字复制其基础设施形态**。

---

## 二、v3.1 的四条对齐原则

### 2.1 Brain / Harness 必须无状态

Brain 负责思考、规划、编排、决定下一步调用什么工具；但它不应该：

- 直接写数据库
- 直接持有某个任务的真实状态
- 直接 import 某个具体工具函数并依赖其实现位置
- 把 Session 历史和模型上下文窗口混为一谈

这里要特别强调一个 Anthropic 原文中的关键点：

> **Session 不是 Claude 的 context window。**

Session 保存的是可恢复、可审计、可重建的历史；而模型真正看到哪些内容、如何裁剪、如何压缩、如何利用 prompt cache，这属于 Harness 的职责，而不是 Session 的职责。

### 2.2 Session 是唯一 durable state boundary

Session 不是“一个负责写 task_runs 的类”，而是系统里唯一真正有状态的边界。它需要提供四类稳定能力：

- `emitEvent()`：追加事件
- `getEvents()`：按顺序读取历史
- `wake(session_id)`：在 harness 崩溃、服务重启、长期暂停后恢复执行
- `getProjection()`：返回面向查询/UI 的派生视图

这意味着：

- `task_events` 是 canonical history
- checkpoint 是恢复优化资产
- `task_runs` 是 projection，不是真相源
- Redis PubSub / SSE 是投递通道，不是真相源

### 2.3 Hands 是可替换执行端，而不是 Brain 的实现细节

Hands 要对 Brain 保持黑盒。Brain 只知道：

- 需要执行什么工具
- 需要什么参数
- 最后拿回什么结果

Brain 不应该关心：

- 这个工具在本进程执行还是在桌面端执行
- 背后是 Tauri、Docker、Firecracker 还是别的执行环境
- 某个 hand 失败后怎么重建

对 chat-dada 来说，这一层会体现为统一的 hand contract：

- `execute(tool_name, input) -> ToolResult`
- `provision(spec)` 或至少 `prepare(ctx)` 的生命周期语义

### 2.4 凭证永远不应该被“推给 Hands”

Anthropic 原文里一个非常关键的安全边界是：**credentials never reach the hand**。

对 chat-dada 来说，这条原则需要落成：

- 服务端 API 凭证只在 Session/Harness 所在侧解析
- LocalExecutor 可以通过服务端安全上下文拿到 secret
- Remote/Desktop Hands 只拿到非敏感参数、短期 capability token，或者走代理接口
- 如果某个能力必须使用桌面本地凭证，也必须由桌面端从 OS-native store 自己读取，而不是由 Brain 把密钥下发过去

---

## 三、当前架构分析

### 3.1 当前主要组件

```
agent/runtime/task_execution.py
├── TaskRunStore
│   ├── 20+ 方法已使用 SessionFactory + Repository（非直连 asyncpg）
│   ├── asyncpg.Pool 仅残留于 _recover_interrupted_tasks() + ConversationContextBuilder
│   ├── 承载了 9 个 Conversation 方法（create/list/update/get/delete/entries/summary 等）
│   └── 职责过载：Task 管理 + Conversation 管理混合在同一类中
├── TaskService
│   ├── _store: TaskRunStore
│   ├── _redis: aioredis.Redis
│   ├── _checkpointer: AsyncPostgresSaver
│   ├── _root_graph: CompiledGraph
│   ├── _runner_tasks: dict           ← 内存任务所有权
│   ├── _background_tasks: set
│   ├── record_event()                ← 写 DB + Redis PubSub（业务事件）
│   └── _publish()                    ← 写 Redis channel
│
agent/runtime/root_graph.py
├── build_root_graph() → normalize_input → run_coordinator → persist_summary
├── run_coordinator() → 每次执行新建 coordinator_graph（当前仍有 MemorySaver 遗留）
└── 手动从 interrupt_state["_dag_resume_state"] 反序列化 DAG 状态注入 coordinator_input
│
agent/coordinator/
├── agent.py → build_coordinator_graph()（无参数，硬编码 MemorySaver）
│   ├── _safe_emit() + direct_answer token writer → 仅写 LangGraph stream
│   └── 不入 DB/Redis
├── executor.py → execute_tasks_node()
│   ├── _safe_emit() → 仅写 LangGraph stream（get_stream_writer），不入 DB/Redis
│   └── GraphInterrupt 时手动序列化 _dag_resume_state（6 个字段）
└── skills.py → 全局 skill_registry
│
agent/domains/
├── */orchestrated.py                 ← 当前真正暴露给 Coordinator 的 domain skill runner 入口
│   └── 由 skill_registry.discover_skills() 自动注册为 do_<domain>
├── research/worker.py + tools.py     ← 唯一直接 import agent.tools 的位置
│   └── 导入 brave_search, academic_search, exa_search, research_notes
├── patent/agent.py + zero_report/agent.py + ppt/workflow.py
│   └── 域内 nested agent / subagent / workflow 仍直接绑定 get_*_tools() 返回值
├── patent/agent.py + zero_report/agent.py
│   └── 有 `_safe_emit` 私有副本（仅写 stream）
└── ppt/workflow.py + ppt/agent.py
    └── 有 `_safe_emit` 私有副本（其中 `ppt/agent.py` 为 legacy 兼容入口）
│
infra/db/repositories/
├── task_repo.py
├── task_event_repo.py
└── conversation_repo.py
```

> **关键发现**：当前存在两套并行事件系统——业务层散落的 `_safe_emit()` / ad-hoc `get_stream_writer()`（20+ 处，仅写 LangGraph stream，不入 DB/Redis）和 `record_event()`（写 DB + Redis PubSub）。Session 统一化时必须处理这一分裂，同时不能误删 `stream_nested_graph()` 的嵌套流透传能力。

### 3.2 当前问题清单

| 问题 | 根本原因 | 影响 |
|------|---------|------|
| TaskRunStore 职责过载（Task + 9 个 Conversation 方法） | 缺乏领域拆分 | 删除 TaskRunStore 时 Conversation 方法无处安放 |
| asyncpg.Pool 残留于恢复路径 + ConversationContextBuilder | 历史遗留（主路径已用 SessionFactory） | Pool 字段无法直接删除 |
| `_safe_emit` / ad-hoc `get_stream_writer` 与 `record_event` 两套并行事件系统 | 业务层私有 writer 仅写 LangGraph stream，`record_event` 写 DB+Redis | 20+ 处散布在 coordinator + domain agent/workflow/legacy 入口中，Session 统一化的最大隐藏工作项 |
| `emit_progress` 若不入 DB 却继续复用现有 SSE `seq/Last-Event-ID` 语义 | 当前断线重连、`after_seq` 回放、前端去重都绑定 `task_events.seq` | 会出现进度事件丢失后跳过后续业务事件、刷新后 UI 状态缺失或错序 |
| 前端 `runEvents` 的 replay contract 未显式定义 | `chat-dada-front` 直接用 `/tasks/{id}/replay` + SSE 重建执行面板、计划模块和阶段文件面板 | 如果不明确哪些事件必须 canonical，后端很容易把前端正在依赖的事件误降级为 transient progress |
| `_runner_tasks` / `_background_tasks` 内存追踪任务 | TaskService 承担了 runtime ownership，且当前 `/cancel` 直接依赖 `runner.cancel()` | 多实例扩展困难；若过早删除会让“取消”退化成只改状态、不真正停止执行 |
| Brain 直接写 `task_runs` / `task_events` | Session 没有被单独建模 | Brain 与 Session 强耦合 |
| Research 域 worker.py / tools.py 直接 import 工具函数 | Tools 没有统一 hand contract | 这是最显眼的一类耦合，但不是 Hands 解耦的全部工作量 |
| `patent` / `zero_report` / `ppt` 的 deepagents agent/subagent 直接绑定 `get_*_tools()` 返回值 | Domain builder 仍假设工具就在本进程且可直接注入 | Phase 3 不能只改 research import；还要补 deepagents-compatible hand adapter |
| Coordinator 仍使用 MemorySaver（build_coordinator_graph 无参数硬编码） | 恢复边界停留在某个 graph 内部 | 崩溃恢复粒度粗 |
| executor.py 手动序列化 `_dag_resume_state` + root_graph.py 手动反序列化恢复 | 绕过 LangGraph checkpoint 的自制恢复机制 | 与 Phase 1 持久化 checkpoint 可能冲突 |
| `_recover_interrupted_tasks` 直接标记 failed | 没有 `wake(task_id)` 语义 | 服务重启后无法精细恢复 |
| `task_runs.status` 被当成真相源 | 事件只被视作审计附属物 | 状态无法从历史重建 |
| `clarification_history` 被当作普通 JSON 历史字段 | 当前 reply/resume、nested interrupt replay、research checkpoint C fast-forward 都直接依赖其结构 | 若只说“迁移到 task_events”而没有派生 helper / projection，恢复链会断 |
| `request_payload` 成为 JSONB 万能口袋 | 缺少清晰数据边界 | 难以验证、迁移、索引 |
| 桌面端当前只有 `invoke_tool` / `list_tools` 命令桥 | 缺少服务端下发请求、在线探测、结果回传的 transport contract | `RemoteDesktopExecutor` 无法真正落地，桌面能力仍处于休眠状态 |
| 凭证边界尚未前置 | 安全模型仍是“后续补” | 未来多用户阶段风险高 |

### 3.3 与 Anthropic 原文相比仍有五个关键差距

| 维度 | 当前状态 | v3.1 要补齐的点 |
|------|---------|-----------------|
| Session 真相源 | `task_runs.status` 中心 | event-log-first，`task_runs` 降级为 projection |
| 恢复入口 | executor.py 手动 `_dag_resume_state` + TaskService 标记 failed | 统一提升为 `Session.wake(task_id)`，清理双重恢复路径 |
| 事件流统一 | `_safe_emit`（仅 stream）与 `record_event`（DB+Redis）并行，且尚未区分 canonical `seq` 与 transient progress | SessionRuntime 区分 `emit_event` / `emit_progress`，并明确 replay cursor 只属于 canonical event |
| Harness 建模 | 还主要被看成“TaskService + 图执行代码” | 明确建模为可重启、无状态、可替换的编排层 |
| Hands 生命周期 | 只有 execute，没有明确 prepare/provision 语义 | 手续理顺后支持 lazy provision 与 hand replacement |
| 安全边界 | 凭证外部化放在后置阶段 | 明确前置为基础架构约束 |

---

## 四、v3.1 的重构目标

### 4.1 继续以职责拆分为主线

v3.1 仍然首先回答三件最直接、最容易读懂的问题：

1. Brain 应该只负责什么？
2. Hands 应该只负责什么？
3. Session 应该只负责什么？

对应答案是：

- **Brain**：思考、规划、编排、上下文整理、决定下一步工具调用
- **Hands**：执行动作、返回结果、暴露统一执行协议
- **Session**：记录历史、恢复任务、输出投影视图、驱动事件分发

### 4.2 在职责拆分之上，补齐稳定接口

Anthropic 原文真正想解决的，不只是“职责不清”，而是“当前 harness 的假设会过时”。所以 v3.1 还要补上三个稳定接口：

| 层 | 主要职责 | 唯一合法接口 | 禁止行为 |
|----|---------|-------------|---------|
| Brain / Harness | 编排、规划、上下文整理、决定 tool call | `session.get_events()` / `session.get_projection()` / `session.wake()` / `gateway.execute()` / `gateway.bind_deepagents_tools()` | 直接 `repo.update_status()`、直接 import 工具函数、直接持有真实状态 |
| Hands | 执行工具、可选 prepare/provision、返回结果 | `execute()` / `prepare()` | 直接写 DB、定义业务状态、长期持有服务端凭证 |
| Session | durable log、恢复、投影、分发 | `emit_event()` / `get_events()` / `wake()` / `get_projection()` | 做 LLM 决策、做工具路由 |

### 4.3 三类数据的职责边界

| 数据/组件 | 定位 | 是否真相源 |
|-----------|------|-----------|
| `task_events` | append-only 历史事件流 | 是 |
| `checkpoints` | 恢复加速资产 | 否 |
| `task_runs` | UI / 查询 / 过滤 projection | 否 |
| Redis PubSub / SSE | 分发与通知通道 | 否 |

### 4.4 Session 不等于上下文窗口

为了防止后续实现再次耦合，v3.1 明确规定：

- Session 负责“保存历史”
- Harness 负责“决定当前 prompt 该看历史中的哪一部分”
- prompt cache、历史压缩、摘要、裁剪，都属于 Harness 优化
- Session 不对某一代模型的上下文策略做假设

---

## 五、四阶段重构方案

### Phase 1：恢复能力归位到 Session

**目标**：服务重启或 harness 崩溃后，不再由 TaskService 私有逻辑决定怎么恢复，而是统一通过 `Session.wake(task_id)` 语义恢复。

> 实现上仍然可以复用 LangGraph checkpoint；但语义归属必须从“graph 的内部能力”提升为“Session 的恢复能力”。

#### Step 1.1：Coordinator checkpointer 持久化

```python
# 改前：agent/coordinator/agent.py
from langgraph.checkpoint.memory import MemorySaver
return graph.compile(checkpointer=MemorySaver(), name="coordinator_graph")

# 改后：接收外部注入的持久化 checkpointer
def build_coordinator_graph(checkpointer):
    graph = StateGraph(CoordinatorState)
    return graph.compile(checkpointer=checkpointer, name="coordinator_graph")
```

```python
# 改前：agent/runtime/root_graph.py
coordinator_graph = build_coordinator_graph()

# 改后：checkpointer 由 Session/Harness 初始化时注入
coordinator_graph = build_coordinator_graph(checkpointer=state["_checkpointer"])
```

#### Step 1.2：引入 `Session.wake(task_id)` 语义

```python
@dataclass
class ResumeHandle:
    task_id: str
    thread_id: str
    stream_input: dict | None
    checkpoint_ns: str | None = None
    resume_context: dict[str, Any] | None = None


class SessionRuntime:
    async def wake(self, task_id: str) -> ResumeHandle:
        projection = await self.get_projection(task_id)
        return ResumeHandle(
            task_id=task_id,
            thread_id=task_id,
            stream_input=None,  # 由底层 checkpoint 决定是否 resume
            checkpoint_ns=projection.latest_checkpoint_ns,
            resume_context={
                # 这些不是 prompt 历史本身，而是恢复所需的结构化资产
                "clarification_history": await self.get_clarification_history(task_id),
                "pending_question": projection.pending_question,
            },
        )
```

```python
# 改前：TaskService 自己决定如何恢复
asyncio.create_task(self._execute_task(task_id, resume_from_crash=True))

# 改后：TaskService 委托 Session 生成恢复句柄
resume = await self._session.wake(task_id)
asyncio.create_task(self._execute_task(task_id, resume=resume))
```

#### Step 1.3：确认 CoordinatorState 字段完整性

以下字段必须在 `CoordinatorState` 中声明，才能成为恢复资产的一部分：

- `completed_tasks`
- `failed_tasks`
- `task_vars`
- `skill_runs`
- `task_dag`

同时确认 `execute_tasks_node` 在 resume 时能够正确：

- 读取 `completed_tasks`
- 跳过已完成 skill
- 仅重跑未完成或中断中的那一轮

#### Step 1.4：明确 checkpoint 的归属

v3.1 明确规定：

- checkpoint 属于 Session 恢复资产
- Brain 只消费 `wake()` 结果
- Brain 不对 checkpoint 存储结构做假设

这样将来即使把 LangGraph checkpoint 替换成别的恢复机制，也不需要改 Brain 层代码。

#### Step 1.5：清理 `_dag_resume_state` 手动恢复机制

> **背景**：当前 `executor.py` 在 `GraphInterrupt` 时手动序列化 6 个字段（`task_dag`, `completed_tasks`, `failed_tasks`, `skill_runs`, `task_vars`, `pending_tasks`）到 `_dag_resume_state`（L365-421），然后 `root_graph.py` 在恢复时手动反序列化并注入 `coordinator_input`（L79-91）。这是一套**绕过 LangGraph checkpoint 的自制恢复机制**。

Phase 1 让 Coordinator 使用持久化 checkpointer 后，两个恢复路径会同时存在：

1. LangGraph checkpoint 自动恢复 CoordinatorState
2. `_dag_resume_state` 手动注入恢复

**必须验证并二选一：**

- 如果持久化 checkpoint 能完整恢复上述 6 个字段 → 删除 `_dag_resume_state` 机制，简化恢复路径
- 如果 checkpoint 因 `MemorySaver → AsyncPostgresSaver` 切换后仍有部分字段无法恢复 → 明确两者的分工边界，避免冲突

```python
# 验证：checkpoint 恢复后 CoordinatorState 是否完整
state = await coordinator_graph.aget_state({"configurable": {"thread_id": task_id}})
assert "completed_tasks" in state.values
assert "task_dag" in state.values
# 如果上述断言通过，可安全删除 _dag_resume_state
```

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/coordinator/agent.py` | `MemorySaver()` 改为外部注入 checkpointer |
| `agent/runtime/root_graph.py` | 传递持久化 checkpointer |
| `agent/session/runtime.py` | 新增 `wake()` / `ResumeHandle` |
| `agent/runtime/task_execution.py` | `_recover_interrupted_tasks` 改为先调 `session.wake()` |
| `agent/coordinator/state.py` | 确认恢复字段完整 |
| `agent/coordinator/executor.py` | 验证 checkpoint 恢复后，考虑删除 `_dag_resume_state` 手动序列化 |
| `agent/runtime/root_graph.py` | 验证后考虑删除 `_dag_resume_state` 手动反序列化（L79-91） |

---

### Phase 2：Session 层独立 + 真相源归位

**目标**：把 Session 从“写 task_runs 的组件”重构成“唯一 durable state boundary”，明确 event log first，projection second。

#### Step 2.1：提取 `SessionRuntime`

```python
class SessionRuntime:
    """独立 Session 层组件。

    职责：
    1. 追加 canonical 事件
    2. 暴露历史读取接口
    3. 提供 wake/recovery 能力
    4. 维护 projection（task_runs）
    5. 向分发通道发布事件
    6. 提供不入 DB 的进度流接口
    """

    async def emit_event(
        self,
        task_id: str,
        event_type: EventType,
        payload: dict,
    ) -> TaskEvent:
        """业务事件：入 DB（canonical history）+ Redis PubSub。"""
        ...

    async def emit_progress(
        self,
        task_id: str,
        event_type: str,
    payload: dict,
    ) -> None:
        """进度提示：仅走 LangGraph stream / Redis PubSub，不入 DB。
        不能占用 canonical event seq，也不能推进 SSE Last-Event-ID。
        替代业务层散落的 _safe_emit / ad-hoc writer；
        不替代 stream_nested_graph() 这种 transport 级嵌套流透传。"""
        ...

    async def get_events(
        self,
        task_id: str,
        *,
        after_seq: int = 0,
    ) -> list[TaskEvent]:
        ...

    async def get_projection(self, task_id: str) -> TaskRunRead:
        ...

    async def wake(self, task_id: str) -> ResumeHandle:
        ...

    async def record_transition(
        self,
        task_id: str,
        new_status: str,
        *,
        reason: str = "",
        error_text: str | None = None,
    ) -> None:
        # helper：本质仍然是 emit canonical event，然后刷新 projection
        ...
```

这里有一个很重要的语义变化：

- `record_transition()` 只是 helper
- 真正的状态真相来自 `task_events`
- `task_runs` 是从事件流投影出来的当前视图

#### Step 2.2：`task_events` 成为 canonical history，`task_runs` 降级为 projection

| 表/组件 | 作用 | 更新方式 |
|---------|------|---------|
| `task_events` | 事件真相源 | append-only |
| `task_runs` | 当前视图、列表查询、过滤 | 由 SessionRuntime 投影更新 |
| `checkpoints` | 恢复优化 | 由 SessionRuntime 管理关联 |

这一步完成后，文档和代码都必须避免下面这种说法：

- “任务状态保存在 `task_runs.status`”

应该改成：

- “当前任务状态由 `task_events` 投影得到，并缓存在 `task_runs` 便于查询”

> **前端恢复契约需要在这一节一起定清楚**：当前 `chat-dada-front` 的 `useTaskStream.hydrateRun()` 会先调用
> `/tasks/{id}/replay` 恢复 `runEvents`，然后再用 `task.last_seq` 续接 SSE。
> 所以只要某个 UI 模块直接从 `runEvents` 重建状态，它依赖的事件就必须是 canonical replayable event，
> 不能偷偷降级为 transient progress。

按当前前端实现，至少有三类直接依赖：

1. 执行面板：`taskPanelSteps(runEvents)` 读取 `start / step / task / node / checkpoint / plan / question / user_reply / file / error`
2. 计划与阶段文件面板：`extractPlanModules(runEvents)` 读取 `plan`，`currentStageArtifactPanel(runEvents)` 读取 `stage_artifacts`
3. 人机交互恢复：`question / user_reply` 事件与 `task.pending_question` projection 共同恢复“等待补充”状态

因此 Phase 2 需要明确三层契约：

| 类别 | 当前前端依赖 | 契约 |
|------|-------------|------|
| **必须 replay 的 canonical events** | `start`, `step`, `task`, `node`, `checkpoint`, `brief`, `plan`, `question`, `user_reply`, `file`, `error`, `result`, `stage_artifacts` | 进入 `task_events`，参与 `/replay`、`after_seq`、`Last-Event-ID` 恢复 |
| **可由 projection/API 补齐的状态** | `status`, `pending_question`, `artifact_refs`, `review`, `budget`, `result`, `last_seq` | 可以从 `task_runs` / review API / artifact API 恢复，但字段语义必须稳定 |
| **允许丢失的 transient progress** | `token`, `streaming_content`, `thinking`, `dag_progress`, `monitoring`, `result_delta`, `custom` | 仅用于实时观感，不参与 `/replay`，刷新后允许消失 |

额外约束：

- 如果 `review` 继续作为流事件存在，它也必须是 canonical；否则就应从前端订阅名单里移除，统一只走 projection/API
- 如果未来希望把 `step` 降级为 transient，就必须先修改前端，不再让 `taskPanelSteps(runEvents)` 直接依赖它
- 文档里以后不能只写“`plan/brief/stage_artifacts` 要回放”，而要写成“凡是当前 front 从 `runEvents` 直接重建的事件，都必须回放”

#### Step 2.3：清理 TaskRunStore 的 asyncpg.Pool 残留

> **代码现状**：TaskRunStore 的 20+ 主要方法已经使用 `SessionFactory` + `Repository`，asyncpg.Pool 仅残留于两处：
> 1. `_recover_interrupted_tasks()` 中的 `self.pool.fetch(...)` — L109
> 2. `ConversationContextBuilder` 接收 `self._store.pool` — L752
>
> 因此这一步的实际工作量比"砍掉全部直连"小得多，核心是：

```python
# 改前：_recover_interrupted_tasks 中残留的 asyncpg 直连
rows = await self.pool.fetch("SELECT task_id FROM task_runs WHERE status IN (...)")

# 改后：统一走 SessionFactory + Repository
async with SessionFactory() as session:
    repo = TaskRunRepository(session)
    task_ids = await repo.list_interrupted()
```

```python
# 改前：ConversationContextBuilder 接收 raw pool
ctx = await ConversationContextBuilder(self._store.pool).build(...)

# 改后：注入 SessionFactory 或 SessionRuntime
ctx = await ConversationContextBuilder(session_factory).build(...)
```

清理完成后即可安全删除 `TaskRunStore.pool` 字段和 `connect()`/`close()` 中的 pool 管理逻辑。

#### Step 2.3b：Conversation 方法独立安置

> **代码现状**：`TaskRunStore` 中混合了 9 个 Conversation 方法（L331-L417），包括：
> `create_conversation`, `list_conversations`, `update_conversation`, `get_conversation`,
> `delete_conversation`, `get_conversation_entries`, `get_conversation_summary`,
> `update_conversation_summary`, `get_conversation_primary_events`
>
> Phase 2 删除 TaskRunStore 时，这些方法需要明确去处。

迁移方案：

```python
# 新增或复用：domain/conversations/service.py
class ConversationService:
    """Conversation 领域的独立服务，与 Task 管理彻底解耦。"""

    async def create(self, *, conversation_id, user_id, title) -> dict: ...
    async def list_for_user(self, user_id) -> list[dict]: ...
    async def update(self, conversation_id, **fields) -> dict | None: ...
    async def get(self, conversation_id) -> dict | None: ...
    async def delete(self, conversation_id) -> bool: ...
    async def get_entries(self, conversation_id) -> list[dict]: ...
    async def get_summary(self, conversation_id) -> tuple[str, int]: ...
    async def update_summary(self, conversation_id, summary, through_seq) -> None: ...
    async def get_primary_events(self, conversation_id, after_seq=0) -> list[dict]: ...
```

对应调整 `web/routers/` 中的 Conversation 路由，从 `task_service.store.xxx` 改为 `conversation_service.xxx`。

#### Step 2.4：`_runner_tasks` 降级为本地执行句柄，而不是立即删除

> **背景**：当前 `cancel_running_task()` 的真实取消动作，依赖 `self._runner_tasks[task_id]` 取到本进程里的 asyncio runner，再执行 `runner.cancel()`。这说明 `_runner_tasks` 现在不只是“状态所有权”，还是**唯一可立即生效的本地取消句柄**。如果在 cooperative cancel 协议落地前就删掉它，`/tasks/{id}/cancel` 很容易退化成“projection 已显示 cancelled，但 graph / tool 仍在后台继续跑”。

```python
# Phase 2 先做的不是“删掉”，而是“降级语义”：
# 1. 真实任务状态仍由 Session projection 维护
# 2. _runner_tasks 仅保留为 process-local cancellation handle
# 3. 多实例取消通过 Session cancel signal + Redis 传播补上
#
# 只有当跨实例 cooperative cancel 已稳定后，
# _runner_tasks 才能从关键路径退出
```

建议拆成两个子阶段：

**Phase 2 必做：**

- `TaskService` 不再“拥有任务状态”，但可以暂时保留 process-local runner handle
- 新增 `Session.request_cancel(task_id)` / `Session.is_cancel_requested(task_id)` 之类的取消语义
- `cancel_running_task()` 先写 canonical cancel 事件，再尽力取消本地 runner
- 如果当前进程不是 owner，不能直接伪造 terminal `cancelled`，而应进入 `cancel_requested` / `cancelling` 之类的过渡语义，等待 owner 确认停止

```python
async def cancel_running_task(self, task_id: str) -> dict[str, Any]:
    await self._session.emit_event(task_id, EventType.TASK_CANCEL_REQUESTED, {})
    await self._session.request_cancel(task_id)

    runner = self._runner_tasks.get(task_id)
    if runner is not None:
        runner.cancel()
        await runner

    return await self._session.get_projection(task_id)
```

**Phase 3/后续才能做：**

- Root graph / ToolGateway / domain runner 在长耗时路径中周期性检查 `is_cancel_requested(task_id)`
- 多实例 owner 能通过 Session/Redis 接收到取消信号并协作停止
- 只有这条链验证通过后，`_runner_tasks` 才能从关键路径移除

因此，这一步的目标应改写为：

- **删除 `_runner_tasks` 作为状态真相源**
- **保留 `_runner_tasks` 作为短期的本地取消执行句柄**
- **把跨实例取消协议前置为删除 `_runner_tasks` 的门槛条件**

#### Step 2.5：拆解 `request_payload` JSONB 万能口袋

> **关键修正**：`clarification_history` 不是普通审计字段，而是当前恢复机制的一部分。它至少承载了三种活跃语义：
>
> 1. reply 后的结构化问答历史（question/context/answer/checkpoint_id/graph_node/nested_graph）
> 2. nested interrupt resume 时的预加载 user replies
> 3. research 域 checkpoint C accept fast-forward 的判定依据
>
> 因此，这一步不能简单写成“把 `clarification_history` 挪到 `task_events`”，而必须同时定义：
>
> - canonical source 放哪
> - 恢复时如何按原顺序、原结构读回
> - 哪些字段继续作为 projection/helper 暴露给 Harness

| 原字段 | 迁移目标 |
|--------|---------|
| `interrupt_state` / `_dag_resume_state` | 删除，由 Session + checkpoint 管理 |
| `clarification_history` | canonical source 迁移到 `task_events`，但 Session 必须继续提供结构化 `get_clarification_history(task_id)` / `resume_context` 派生接口 |
| `pending_question` / `nested_interrupt_pending` | 从 JSONB 万能口袋中拆出为明确 projection 或 Session helper |
| `latest_checkpoint_id` | `task_runs.latest_checkpoint_id` projection 字段 |
| `conversation_id` / `file_paths` 等不可变元数据 | 保留在 `request_payload` |

建议的事件化方式：

```python
class EventType(str, Enum):
    USER_QUESTION = "user_question"
    USER_REPLY = "user_reply"
```

```python
await session.emit_event(task_id, EventType.USER_QUESTION, {
    "content": question,
    "context": why,
    "placeholder": placeholder,
    "checkpoint_id": checkpoint_id,
    "graph_node": graph_node,
    "nested_graph": nested_graph,
})

await session.emit_event(task_id, EventType.USER_REPLY, {
    "content": answer,
    "checkpoint_id": checkpoint_id,
    "graph_node": graph_node,
    "nested_graph": nested_graph,
})
```

然后由 SessionRuntime 暴露派生 helper：

```python
class SessionRuntime:
    async def get_clarification_history(self, task_id: str) -> list[dict[str, Any]]:
        """从 task_events 中按 seq 重建结构化 clarification_history。

        返回格式需保持与当前 harness / research resume 兼容。
        """
        ...
```

约束：

1. Harness 不再从 `request_payload["clarification_history"]` 直接取值
2. research checkpoint C fast-forward 读取的也是 Session 派生结果，而不是 JSONB 残留字段
3. `pending_question` 是 projection，不是历史真相源
4. event log 中必须保留 `checkpoint_id` / `graph_node` / `nested_graph`，否则无法等价替代当前恢复逻辑

#### Step 2.6：TaskService 重构为纯 Harness Runtime

```python
class TaskService:
    """无状态编排层：只依赖 Session 和 Hands 的稳定接口。"""

    def __init__(
        self,
        session: SessionRuntime,
        tool_gateway: ToolGateway,
        root_graph: CompiledGraph,
        redis: aioredis.Redis,
    ):
        self._session = session
        self._tool_gateway = tool_gateway
        self._root_graph = root_graph
        self._redis = redis

    async def _execute_task(self, task_id: str, *, resume: ResumeHandle | None = None):
        await self._session.record_transition(task_id, "running")
        clarification_history = []
        if resume and resume.resume_context:
            clarification_history = list(resume.resume_context.get("clarification_history") or [])
        config = {
            "configurable": {
                "thread_id": task_id,
                "session": self._session,
                "tool_gateway": self._tool_gateway,
            }
        }
        initial_state["clarification_history"] = clarification_history
        stream_input = resume.stream_input if resume else initial_state
        async for _ in self._root_graph.astream(stream_input, config=config):
            ...
        await self._session.record_transition(task_id, "completed")
```

这里的“无状态”需要精确定义：

- **不拥有 durable task state**
- **不直接写 repo**
- **可以短期保留 process-local runtime handle（如 runner cancel handle）**

也就是说，Phase 2 的 TaskService 应该是 **state-light harness runtime**，而不是一上来就彻底没有任何本地执行句柄。

#### Step 2.7：明确 Brain 与 Session 的边界

```
Brain / Harness 可以调用：
  session.emit_event()
  session.get_events()
  session.get_projection()
  session.wake()
  session.record_transition()

Brain / Harness 不可以直接调用：
  TaskRunRepository.update_status()
  TaskEventRepository.append()
  repo.commit()
```

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/session/runtime.py` | 新增 SessionRuntime（含 `emit_event` + `emit_progress` 双接口） |
| `agent/session/__init__.py` | 导出 SessionRuntime |
| `agent/runtime/task_execution.py` | 删除 TaskRunStore 直写逻辑；`_runner_tasks` 降级为 process-local cancel handle；Conversation 方法迁出 |
| `domain/conversations/service.py` | 新增 ConversationService（承接 9 个 Conversation 方法） |
| `agent/coordinator/agent.py` | `_safe_emit` + `direct_answer` 的 ad-hoc token writer 统一迁到 SessionRuntime |
| `agent/coordinator/executor.py` | `_safe_emit` → `session.emit_progress()`（进度类）或 `session.emit_event()`（业务类） |
| `agent/domains/zero_report/agent.py` | 同上 |
| `agent/domains/patent/agent.py` | 同上 |
| `agent/domains/ppt/agent.py` | 清理遗留兼容入口里的 `_safe_emit` 死代码 |
| `agent/domains/ppt/workflow.py` | 同上 |
| `agent/platform/streaming.py` | 保留 `stream_nested_graph()` 的 transport 级透传；明确它不是业务事件写入点 |
| `web/runtime.py` | 初始化 SessionRuntime + ConversationService 并注入 |
| `web/routers/` | Conversation 路由从 `task_service.store.xxx` 改为 `conversation_service.xxx` |
| `scripts/init.sql` | 为 projection 字段补充 `latest_checkpoint_id` 等列 |

#### Step 2.8：`_safe_emit` 进度流迁移

> **背景修正**：当前需要迁移的并不只是 `executor.py` 和“各域 workflow”。
> 业务层 writer 还散落在：
>
> - `agent/coordinator/agent.py` 的 `_safe_emit()`
> - `agent/coordinator/agent.py` 的 `direct_answer` token 流式 writer
> - `agent/coordinator/executor.py` 的 `_safe_emit()`
> - `agent/domains/patent/agent.py` 的 `_safe_emit()`
> - `agent/domains/zero_report/agent.py` 的 `_safe_emit()`
> - `agent/domains/ppt/workflow.py` 的 `_safe_emit()`
> - `agent/domains/ppt/agent.py` 的遗留 `_safe_emit()`
>
> 这些调用都只写 LangGraph `get_stream_writer()` 流，不入 DB/Redis。v3.1 的 `session.emit_event()` 写 DB+Redis。如果把所有 writer 都改为 `session.emit_event()`，会导致大量中间进度事件（streaming_content、dag_progress 等）涌入 DB，不合理。

> **边界修正**：不是所有 `get_stream_writer()` 都应该归零。`agent/platform/streaming.py::stream_nested_graph()` 当前承担 nested graph 事件透传，它属于 transport bridge，不应被与业务层 `_safe_emit` 一起一刀切删除。

> **新增约束**：不能把 “不入 DB” 简化成 “仍沿用现有 `seq` / `Last-Event-ID` / `after_seq` 语义”。当前 chat-dada 的断线重连、`/tasks/{id}/replay`、前端去重，都建立在 `task_events.seq` 是唯一回放游标之上。只要某类事件会参与刷新恢复、任务卡片恢复、阶段面板恢复，它就不是 transient progress，而必须进入 canonical event log。

**解决方案**：SessionRuntime 区分两类事件接口：

```python
class SessionRuntime:
    async def emit_event(
        self, task_id: str, event_type: EventType, payload: dict
    ) -> TaskEvent:
        """业务事件：入 DB（canonical history）+ Redis PubSub。
        用于 task_started, skill_finished, tool_call_started 等。"""
        ...

    async def emit_progress(
        self, task_id: str, event_type: str, payload: dict
    ) -> None:
        """进度提示：仅走 LangGraph stream / Redis PubSub，不入 DB。
        用于 token, dag_progress, thinking 等高频临时事件。
        注意：这类事件不分配 canonical seq，不参与 replay。"""
        ...
```

**SSE / replay 约束必须同时成立：**

1. `emit_event()` 产出的事件才有 canonical `seq`
2. `/tasks/{id}/replay`、`task.last_seq`、`after_seq`、`Last-Event-ID` 只认 canonical `seq`
3. `emit_progress()` 如果复用同一 SSE 通道，必须是 **best-effort non-resumable**
4. `emit_progress()` 不能推进 `task.last_seq`
5. `emit_progress()` 不能让服务端在重连后“以为客户端已经看过了某个 canonical event”
6. 如果某类 UI 事件在刷新后仍需恢复，就应升级为 `emit_event()`，而不是继续留在 `emit_progress()`

**迁移规则**：

| 当前调用 | 迁移目标 | 判断标准 |
|---------|---------|---------|
| `_safe_emit("token", ...)` / `_safe_emit("streaming_content", ...)` / `thinking` / `monitoring` / `result_delta` | `session.emit_progress()` | 高频流式片段或观测信息，允许断线丢失，不参与刷新恢复 |
| `_safe_emit("dag_progress", ...)` | `session.emit_progress()` | 纯瞬时进度，不驱动任务状态或卡片结构 |
| `_safe_emit("start", ...)` / `_safe_emit("step", ...)` / `_safe_emit("task", ...)` / `_safe_emit("node", ...)` / `_safe_emit("checkpoint", ...)` | `session.emit_event()` | 当前前端直接用 `runEvents` 重建执行面板，刷新后必须保留 |
| `_safe_emit("skill_started", ...)` / `_safe_emit("task_start", ...)` / `_safe_emit("task_complete", ...)` | `session.emit_event()` | 业务里程碑，需要恢复/审计 |
| `_safe_emit("brief", ...)` / `_safe_emit("plan", ...)` / `_safe_emit("question", ...)` / `_safe_emit("user_reply", ...)` / `_safe_emit("file", ...)` / `_safe_emit("error", ...)` / `_safe_emit("result", ...)` / `_safe_emit("stage_artifacts", ...)` | `session.emit_event()` | 当前前端会直接或间接用它们恢复等待补充、产物、结果和阶段面板 |
| `_safe_emit("review", ...)` | `session.emit_event()` 或移除该流事件、统一改走 projection/API | 不能保持“前端订阅了 review 事件，但刷新恢复时却不保证它存在”的灰色状态 |
| `record_event(task_id, "step", ...)` | `session.emit_event()` | 已有 DB 写入，保持不变 |

**实现提示**：

- 最保守做法：`emit_progress()` 走 SSE 但不写 `id`，前端也不把它纳入 `seq` 去重
- 如果后续确实需要对 progress 做去重，可引入独立 `transport_seq`
- 但 `transport_seq` 绝不能与 `task_events.seq` 共用一套恢复语义
- `stream_nested_graph()` 可以继续保留底层 writer 作为 nested transport bridge；但 business code 不再直接写 `get_stream_writer()`

**迁移完成后验证**：

- `task.last_seq` 只随 `emit_event()` 增长
- 浏览器断线重连后，`after_seq` 只能补发 canonical events
- 页面刷新后，`taskPanelSteps(runEvents)`、`extractPlanModules(runEvents)`、`currentStageArtifactPanel(runEvents)` 仍能恢复到刷新前的结构化状态
- transient progress 丢失只影响实时观感，不影响最终状态、任务卡片和阶段面板
- `direct_answer` 的 token 流仍能实时显示，但业务层不再直接调 `get_stream_writer()`
- nested graph 的 step/token/interrupt 透传仍成立，证明 `stream_nested_graph()` 的 bridge 没被误删

---

### Phase 3：Harness 无状态化 + Brain↔Hands 完全解耦

**目标**：在职责拆分层面，Brain 不再直调工具；在接口层面，Harness 只依赖 Hand Contract，而不依赖具体执行环境。

> 这里仍沿用工程命名 `ToolProtocol`，但在架构语义上，它代表的是 `Hand Contract`。

#### Step 3.1：明确 Harness 的范围

在 chat-dada 里，以下组件合在一起就是 Harness：

- `TaskService`
- `root_graph`
- `Coordinator`
- `skill_registry` 暴露的 domain skill runner（主要来自 `agent/domains/*/orchestrated.py`）

> **实现口径修正**：`agent/domains/*` 在当前系统里更准确的角色不是“顶层直接运行的 domain agent”，
> 而是“被 Coordinator/DAG 选择并调用的 domain skill runner”。
> 也就是说：
>
> - 顶层编排入口是 `Coordinator`
> - `Coordinator` 通过 `skill_registry` 选择 `do_research` / `do_patent` / `do_ppt` / `do_zero_report`
> - 这些 skill 的实际 runner 来自 `agent/domains/*/orchestrated.py`
> - domain 内部仍然可以再创建 nested graph、deepagents agent/subagent，这属于 skill 内部实现，不是顶层调度模型

它们共同负责：

- 从 Session 读取历史与 projection
- 组装当前一步执行所需的上下文
- 决定下一步调用什么工具
- 解释工具结果并继续编排

它们共同不负责：

- 落盘真实状态
- 决定状态真相源
- 持有具体 hand 的实现细节

#### Step 3.2：统一 ToolProtocol，并补上 prepare/provision 语义

```python
@dataclass
class ToolCall:
    tool_name: str
    params: dict[str, Any]
    task_id: str
    timeout_ms: int = 30000


@dataclass
class ToolResult:
    success: bool
    output: str
    artifacts: list[dict] = field(default_factory=list)
    error: str | None = None
    execution_time_ms: int = 0


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult: ...


class ToolProvisioner(Protocol):
    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None: ...
```

`prepare()` 的意义不是为了把系统做复杂，而是为了补上 Anthropic 原文里 hand lifecycle 的最小语义：

- 某些 hand 可能需要懒初始化
- 某些 hand 可能需要权限检查或环境探测
- 某些 hand 未来可能需要真正的 `provision()`

#### Step 3.3：LocalExecutor 统一封装服务端工具

```python
class LocalToolExecutor:
    def __init__(self):
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable):
        self._tools[name] = fn

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        return None

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        fn = self._tools.get(call.tool_name)
        if fn is None:
            return ToolResult(success=False, output="", error=f"unknown tool: {call.tool_name}")
        result = await fn(**call.params)
        return ToolResult(success=True, output=str(result))
```

#### Step 3.4：RemoteDesktopExecutor + Tauri transport 对接

> **关键修正**：当前 `chat-dada-front/src-tauri/src/commands.rs` 只有本地 `invoke_tool()` / `list_tools()` 命令桥。
> 这还不是“服务端可调度的 Desktop Hand”。
> 因此这里不是只新增一个 server-side `RemoteDesktopExecutor` 类，而是要补齐一条双端 transport 链：
>
> - 服务端能够按 `task_id/request_id` 主动下发 tool request
> - 桌面端能够声明在线状态与 capability
> - 桌面端执行后能够按 `request_id` 回传结果 / 错误 / 超时
> - transport 断线时不重复写 started/finished 业务事件

```python
class RemoteDesktopExecutor:
    """通过 SSE 下发请求、通过 HTTP POST 收回结果。

    这里只负责 transport，不负责定义业务事件语义。
    """

    async def prepare(self, call: ToolCall, ctx: ToolContext) -> None:
        # 检查桌面端在线、权限、能力声明
        ...

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        request_id = str(uuid.uuid4())
        await self._push_request(request_id, call)
        return await self._wait_for_result(request_id, timeout_ms=call.timeout_ms)
```

最小落地范围应明确包括：

1. chat-dada 服务端：desktop request push、result callback、在线会话管理
2. `chat-dada-front` Tauri：订阅待执行请求、执行本地工具、回传结果
3. `request_id` / `task_id` / `tool_name` / `timeout_ms` 的稳定协议字段
4. 取消、超时、掉线后的 transport 级状态机

如果前端仍只有本地命令桥，而没有订阅/回传链路，则这一节只能算接口设计，不算 Phase 3 完成。

#### Step 3.5：ToolGateway 成为 tool-call 事件的唯一编排入口

```python
class ToolGateway:
    def __init__(self, local, remote, session):
        self._local = local
        self._remote = remote
        self._session = session
        self._routing: dict[str, str] = {}

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        target = self._routing.get(call.tool_name, "local")
        executor = self._local if target == "local" else self._remote

        await executor.prepare(call, ctx)

        await self._session.emit_event(call.task_id, EventType.TOOL_CALL_STARTED, {
            "tool_name": call.tool_name,
            "target": target,
        })

        result = await executor.execute(call, ctx)

        await self._session.emit_event(
            call.task_id,
            EventType.TOOL_CALL_FINISHED if result.success else EventType.TOOL_CALL_FAILED,
            {
                "tool_name": call.tool_name,
                "target": target,
                "execution_time_ms": result.execution_time_ms,
                "error": result.error,
            },
        )
        return result
```

对于必须向 deepagents 传入 `tools=[...]` 的域，ToolGateway 还需要提供类似
`bind_deepagents_tools(domain, task_id)` 的 adapter factory；它生成的 tool objects
内部仍然要回到同一条 `gateway.execute()` / route / event 记录路径。

v3.1 这里还新增一条强约束：

- **ToolGateway 是 tool call 业务事件的唯一权威写入点**
- Remote executor 和 `/tool_result` 回调只负责 transport，不重复写业务事件

这样可以避免 started/finished 事件重复。

#### Step 3.6：Domain Skill Runner / 域内编排层改造成纯编排节点

> **代码现状修正**：
>
> - `agent/domains/*/orchestrated.py` 才是当前被 `skill_registry` 注册、供 DAG 选择调用的 domain skill runner 入口
> - `research/worker.py` + `research/tools.py` 确实是唯一直接 `import agent.tools` 的位置
> - 但 `patent/agent.py`、`zero_report/agent.py`、`ppt/workflow.py` 在构造 deepagents agent/subagent 时，也直接把 `get_*_tools()` 的结果绑定进 `tools` 字段
> - `research/orchestrated.py` 本身调用的是 `stream_nested_graph(_graph, ...)`，不是直接工具耦合点
>
> 所以这一步要拆成两类工作：
>
> 1. 把 research 域从“直接 import 工具函数”迁到 `gateway.execute()`
> 2. 把 deepagents 域从“直接绑定本地 tool list”迁到“绑定 gateway 生成的 deepagents-compatible tool adapters”

```python
# 改前（agent/domains/research/worker.py）
from agent.tools.brave_search import run as run_brave_search
from agent.tools.academic_search import run as search_academic
results = await run_brave_search(query)

# 改前（agent/domains/research/tools.py）
from agent.tools.exa_search import run as search_exa
from agent.tools.research_notes import save_research_note, recall_research_notes

# 改后：统一通过 gateway
gateway: ToolGateway = config["configurable"]["tool_gateway"]
session: SessionRuntime = config["configurable"]["session"]

results = await gateway.execute(
    ToolCall("brave_search", {"query": query}, state["task_id"]),
    ctx,
)
summary = await gateway.execute(
    ToolCall("summarizer", {"text": results.output}, state["task_id"]),
    ctx,
)
await session.emit_event(state["task_id"], EventType.SKILL_FINISHED, {...})
```

```python
# 改前（agent/domains/patent/agent.py）
tools = get_patent_tools()
return create_deep_agent(
    ...,
    tools=tools,
    subagents=[{"name": "prior_art_researcher", "tools": tools}],
)

# 改后：由 gateway / adapter layer 提供 deepagents-compatible toolset
toolset = gateway.bind_deepagents_tools(domain="patent", task_id=task_id)
return create_deep_agent(
    ...,
    tools=toolset,
    subagents=[{"name": "prior_art_researcher", "tools": toolset}],
)
```

验证规则：

```
Domain skill runner / 域内编排层 可以调用：
  gateway.execute()
  session.emit_event()
  session.emit_progress()
  LLM 调用

Domain skill runner / 域内编排层 不可以直接调用：
  from agent.tools.xxx import ...
  get_patent_tools() / get_zero_report_tools() / get_ppt_tools()
  record_event()
  repo.append()
  _safe_emit()
```

#### Step 3.7：明确 Hands 的两种形态

| Hands 类型 | 当前/未来 | 生命周期特征 |
|-----------|-----------|-------------|
| Local Desktop Hands（Tauri） | 当前重点 | 常驻客户端、强交互、需权限授权 |
| Ephemeral Remote Hands（容器/沙箱） | 未来扩展 | 按需 provision、失败可替换、cattle-style |

二者都必须遵守同一 hand contract；不同的只是生命周期，而不是 Brain 所看到的接口。

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/tools/protocol.py` | 补充 `prepare()` / provision 语义 |
| `agent/tools/local_executor.py` | 服务端工具统一适配 |
| `agent/tools/remote_executor.py` | 桌面端 transport executor |
| `agent/gateway/tool_gateway.py` | 统一路由与唯一事件权威点 |
| `agent/domains/research/worker.py` | 砍掉 `from agent.tools.brave_search/academic_search import ...`，改用 gateway |
| `agent/domains/research/tools.py` | 砍掉 `from agent.tools.exa_search/research_notes import ...`，改用 gateway |
| `agent/domains/patent/agent.py` | deepagents toolset 不再直接取 `get_patent_tools()`，改接 gateway adapter |
| `agent/domains/zero_report/agent.py` | deepagents toolset 不再直接取 `get_zero_report_tools()`，改接 gateway adapter |
| `agent/domains/ppt/workflow.py` | `PPT_SUBAGENTS` 不再直接绑定 `get_ppt_tools()` 返回值 |
| `web/routers/tasks.py` | `POST /tool_result` 只做 transport 回传 |
| `chat-dada-front/src/hooks/useTaskStream.ts` | 适配新事件流 |
| `chat-dada-front/src-tauri/src/commands.rs` + transport 模块 | 从本地命令桥扩展为 SSE/HTTP 双向 transport |

---

### Phase 4：安全边界 + 可观测性

**目标**：把“凭证永不进入 Hands”前置成架构约束，并补充事件流、链路和上下文工程的可观测性。

#### Step 4.1：凭证永不进入 Hands

```python
@dataclass
class ToolContext:
    user_id: str
    task_id: str
    trace_id: str

    async def get_secret(self, key: str) -> str | None:
        """仅在 Harness / LocalExecutor 侧解析 secret。

        Remote/Desktop Hands 不直接拿到真实 secret。
        """
        ...
```

约束分为三类：

1. **服务端凭证**
   - 只能由 Harness / LocalExecutor 侧解析
   - 后续从 env var 迁移到 VaultService

2. **桌面端本地权限**
   - 由 Tauri + OS 权限系统处理
   - 不通过 Brain 下发 secret

3. **必须跨边界的敏感能力**
   - 走代理 API 或短期 capability token
   - token 只表达一次性能力，不暴露底层真实凭证

#### Step 4.2：事件语义与 transport 语义分离

为了让 event log 可以同时用于恢复、审计和前端展示，v3.1 要求：

- `task_events` 记录的是业务语义事件
- Redis / SSE / HTTP 回传属于 transport
- transport 消息可以丢、可重试、可重复
- business event 只能有一个权威写入点

建议的事件类型：

```python
class EventType(str, Enum):
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    SKILL_STARTED = "skill_started"
    SKILL_FINISHED = "skill_finished"
    SKILL_FAILED = "skill_failed"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    TOOL_CALL_FAILED = "tool_call_failed"

    USER_QUESTION = "user_question"
    USER_REPLY = "user_reply"

    CHECKPOINT_SAVED = "checkpoint_saved"
```

#### Step 4.3：可观测性不仅是日志，更包括上下文工程

需要新增至少三类可观测信息：

- `last_event_seq`
- `latest_checkpoint_id`
- `harness_context_strategy`（例如 full / summarized / cached）

原因是：

- Session 历史是稳定的
- 但 Harness 如何把历史变成 prompt，会随模型和策略持续演进
- 只有把这层也观测出来，后续才不会把“Session”与“当前上下文窗口”重新混淆

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/tools/context.py` | ToolContext / secret 访问接口 |
| `agent/session/runtime.py` | event publish 与 projection 刷新逻辑 |
| `infra/db/repositories/task_event_repo.py` | EventType 白名单验证 |
| `web/routers/tasks.py` | transport 与 business event 分离 |
| `chat-dada-front/src/` | 前端事件展示与状态恢复体验优化 |

---

## 六、演进路线图

```
Phase 1：恢复能力归位到 Session
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 1.1 Coordinator checkpointer 持久化
  ├─ 1.2 引入 Session.wake(task_id)
  ├─ 1.3 校验 CoordinatorState 恢复字段
  ├─ 1.4 明确 checkpoint 属于 Session 资产
  └─ 1.5 清理 _dag_resume_state 与 checkpoint 恢复冲突

Phase 2：Session 层独立 + 真相源归位
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 2.1 提取 SessionRuntime（含 emit_event + emit_progress 双接口）
  ├─ 2.2 task_events 成为 canonical history
  ├─ 2.3 清理 TaskRunStore asyncpg.Pool 残留
  ├─ 2.3b Conversation 方法独立安置到 ConversationService
  ├─ 2.4 _runner_tasks 降级为 process-local cancel handle
  ├─ 2.5 拆解 request_payload
  ├─ 2.6 TaskService 重构为纯 Harness Runtime
  └─ 2.8 _safe_emit 进度流统一迁移（20+ 处散装副本 → session.emit_progress）

Phase 3：Harness 无状态化 + Brain↔Hands 解耦
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 3.1 明确 Harness 边界
  ├─ 3.2 ToolProtocol 补充 prepare/provision 语义
  ├─ 3.3 LocalExecutor 封装服务端工具
  ├─ 3.4 RemoteDesktopExecutor + Tauri transport
  ├─ 3.5 ToolGateway 成为唯一 tool-call 事件权威点
  ├─ 3.6 Domain Workers / Deepagents 域改造成纯编排
  └─ 3.7 Hands taxonomy：Desktop vs Ephemeral

Phase 4：安全边界 + 可观测性
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 4.1 凭证永不进入 Hands
  ├─ 4.2 business event 与 transport 分离
  └─ 4.3 补充 context strategy 可观测性
```

### 6.1 依赖关系与并行度

```
Phase 1                    Phase 2                                     Phase 3
───────────────           ──────────────────────────────              ───────────────────
1.1 持久化 checkpoint ─┐   2.1 SessionRuntime ─────┐                  3.1 Harness 边界澄清
1.2 wake() 语义      ─┼→  2.2 event log first     ├→ 2.6             3.2 ToolProtocol 补 prepare
1.3 state 字段校验   ─┤   2.3 清理 asyncpg 残留    │                  3.3 LocalExecutor
1.5 _dag_resume      ─┘   2.3b Conversation 方法拆出│                  3.4 RemoteDesktopExecutor + Tauri transport
    冲突验证                2.4 runner_tasks 降级   │                  3.5 ToolGateway 唯一事件点
                           2.5 拆 request_payload ─┘                  3.6 Domain Workers / Deepagents 改造
                           2.8 _safe_emit 迁移                        3.7 Hands taxonomy
```

### 6.2 三层解耦达成检查点

| 阶段 | Brain / Harness | Hands | Session |
|------|-----------------|-------|---------|
| Phase 1 完成 | 恢复能力开始脱离私有 graph 逻辑 | 未完全激活 | 具备 wake 语义雏形 |
| Phase 2 完成 | 不再直写 DB，durable state 基本无状态化；本地取消句柄可暂存 | 未完全统一 | 成为 durable state boundary |
| Phase 3 完成 | 只依赖 Session 与 Hand Contract | 统一协议、可替换执行 | 与 Harness、Hands 边界清晰 |
| Phase 4 完成 | 安全和上下文工程边界清晰 | 不直接持有服务端凭证 | 事件、恢复、观测闭环 |

---

## 七、关键决策与理由

| 决策 | 结论 | 放弃的方案 | 理由 |
|------|------|-----------|------|
| 恢复入口 | `Session.wake(task_id)` | Brain 直接写死 checkpoint resume | 恢复能力应属于 Session，而非某个 graph 私有能力 |
| 恢复实现 | 继续复用 LangGraph checkpoint | 自研 event replay-only | 现阶段工程成本最低，但语义上仍归属于 Session |
| Session 真相源 | `task_events` first，`task_runs` second | 继续以 `task_runs.status` 为中心 | 只有 event log 才能支持恢复、审计、重建 |
| Brain↔Hands 通信 | `ToolGateway + Executor` | 直接函数调用工具 | 消除 Brain 对工具位置和传输方式的假设 |
| Hands 生命周期 | 增加 `prepare/provision` 语义 | 只有 execute，无生命周期概念 | 对齐 Anthropic 原文的 hand replaceability 与 lazy provision |
| 本地桌面能力 | 保留 Tauri Desktop Hands | 强行伪装成 ephemeral sandbox | 产品能力真实存在，应显式建模为变体 |
| 安全边界 | 凭证永不进入 Hands | Phase 4 再考虑 | 这是架构前提，不是后续优化 |
| 上下文工程 | Harness 负责裁剪/压缩历史 | Session 直接等同模型上下文 | 让 Session 对未来模型能力保持中立 |

---

## 八、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| event log 设计升级带来改造面 | 多个模块需要同时调整 | 先保留 helper API，再逐步把实现切到 event-first |
| `task_runs` 降级为 projection 后历史代码不兼容 | 旧逻辑仍假设 status 是真相源 | 增加审计 grep 和投影一致性测试 |
| wake 语义引入后实现复杂度上升 | 恢复路径更长 | 用 `ResumeHandle` 收敛实现细节 |
| `_dag_resume_state` 与 checkpoint 双重恢复冲突 | Phase 1 持久化 checkpoint 后可能重复恢复 | Step 1.5 强制验证并二选一 |
| transient progress 与 SSE replay cursor 混用 | 断线重连可能跳过业务事件，前端刷新后状态不一致 | 明确 canonical `seq` 只属于 `emit_event()`，并把 `plan/brief/stage_artifacts/review` 归入 canonical event |
| 前端恢复依赖没有被写成显式 contract | 后端可能把 `start/step/task/node/checkpoint` 之类 UI 关键事件误降级为 transient | 以 `useTaskStream.hydrateRun()` + `taskPanelSteps` / `extractPlanModules` / `currentStageArtifactPanel` 为准，明确 replay-required event 集合 |
| `_safe_emit` 迁移覆盖面大（20+ 处跨 coordinator + domain internals + legacy 入口） | 散装进度事件未被 v3.1 原始版本识别 | 用 `emit_progress` 统一收口，不入 DB，并单独清理 `direct_answer` 的 ad-hoc writer |
| `_safe_emit` 清理时把 nested transport bridge 一起删掉 | nested graph 的 step/token/interrupt 透传中断 | 明确 `stream_nested_graph()` 属于 transport bridge，可保留底层 writer，不与业务层 `_safe_emit` 混为一谈 |
| 过早删除 `_runner_tasks` | `/cancel` 只改 projection，不真正停止 graph/tool 执行 | 先引入 Session cancel signal；本地 runner handle 延后删除 |
| `clarification_history` 只做“事件落盘”不做结构化派生 | nested resume、checkpoint C fast-forward、pending question 恢复失效 | SessionRuntime 提供 `get_clarification_history()` / `resume_context`，保留现有结构语义 |
| Conversation 方法无处安置 | TaskRunStore 删除后 9 个方法需要新家 | 提前规划 ConversationService 迁移 |
| 把 Phase 3 误判为“只改 research import” | `patent` / `zero_report` / `ppt` 仍绑定本地 toolset，desktop hand 仍缺 transport | 把 deepagents tool binding 与 Tauri 双端改造纳入范围、文件清单与验收 |
| ToolGateway 成为唯一事件权威点后 transport 层需要收敛 | 旧回调逻辑需要改 | 明确 transport 不写业务事件 |
| Desktop Hands 与 Ephemeral Hands 生命周期不同 | 容易再次在文档中混淆 | 单独维护 Hands taxonomy 章节 |
| 凭证边界收紧后部分工具实现受限 | 开发初期体验变差 | 用代理 API / capability token 过渡 |

---

## 九、验证计划

### 9.1 Phase 1：恢复验证

- 启动 DAG 任务（至少 3 个 skill），在第 2 轮执行中 `kill -9`
- 重启后由 `Session.wake(task_id)` 触发恢复
- 验证第 1 轮已完成 skill 不重跑，第 2 轮未完成 skill 重新执行
- 最终结果与无崩溃场景一致
- 对 nested interrupt 任务执行一次“提问 → 回复 → kill -9 → 恢复”，验证 `clarification_history` 能从 Session 派生结果重建并继续执行

### 9.2 Phase 2：Session 独立验证

- `grep -rn "TaskRunRepository\|TaskEventRepository" agent/`，确认 Brain/Harness 层零直接 repo 调用
- `grep -rn "asyncpg" agent/`，确认业务层零直连
- 构造事件流，验证可以重建 `task_runs` projection
- 故意制造 projection 损坏，确认可从 `task_events` 重建
- 断开 SSE 后重连，验证 `Last-Event-ID/after_seq` 只回放 canonical events
- 验证 `emit_progress()` 不推进 `task.last_seq`
- `rg -n "^def _safe_emit|_safe_emit\\(" agent/` 结果应为零，确认业务层私有 stream writer 已清空
- `rg -n "get_stream_writer\\(" agent/` 结果只允许出现在 `agent/platform/streaming.py` 和 SessionRuntime 内部桥接层，不再出现在 coordinator/domain 业务代码
- 用当前 `chat-dada-front` 的 `hydrateRun()` 跑一次“刷新恢复”验收，验证 `runEvents` 经 `/tasks/{id}/replay` 恢复后，`taskPanelSteps`、`extractPlanModules`、`currentStageArtifactPanel` 输出与刷新前一致
- 刷新页面后，允许丢失的仅是 token / thinking / dag_progress / monitoring / result_delta 这类瞬时事件；不允许丢失 `start/step/task/node/checkpoint/brief/plan/question/user_reply/file/error/result/stage_artifacts`
- 若 `review` 继续保留为流事件，验证它也会 replay；若不 replay，则前端必须移除对 `review` 流事件的订阅并只走 projection/API
- `direct_answer` 模式下仍可看到 token 流式输出，但其实现已走 `emit_progress()`
- 同进程运行中的任务调用 `/cancel`，验证 graph 会真正停止，而不是仅写出 `cancelled` projection
- 无本地 runner handle 的场景下，验证状态进入 `cancel_requested` / `cancelling` 过渡语义，而不是误报 terminal `cancelled`
- 构造 `user_question` / `user_reply` 事件流，验证可按 `seq` 重建当前格式的 `clarification_history`
- 研究任务在 checkpoint C accept 后恢复，验证 fast-forward 判定仍成立，不再依赖 `request_payload["clarification_history"]`

### 9.3 Phase 3：Harness 与 Hands 解耦验证

- `grep -rn "from agent\.tools\." agent/domains/` 结果应为零（当前应有 6 处在 research/worker.py + tools.py）
- `rg -n "get_(patent|zero_report|ppt)_tools\(|\"tools\": tools|tools=\\[t for t in get_ppt_tools" agent/domains/` 结果只能出现在 gateway/adapter 层，不再出现在 domain agent/workflow
- 注入 Mock SessionRuntime + Mock ToolGateway，TaskService / Coordinator 全流程通过
- 同一 ToolCall 分别路由到 local 和 desktop hand，返回结构一致
- 桌面端完成一次“服务端下发 request_id → Tauri 执行 → `POST /tool_result` 回传”的闭环；只有 `invoke_tool/list_tools` 本地调用不算通过
- desktop hand 下线后，Session 仍完整可读，恢复后可继续执行

### 9.4 Phase 4：安全边界验证

- remote/desktop hand 端日志中不出现服务端真实 API key
- 需要敏感能力的远程调用只出现 capability token 或代理调用痕迹
- harness context strategy 变化时，Session 历史无需改表结构

### 9.5 架构级最终验证

```
Harness crash test:
  杀掉当前 harness 进程
  新进程通过 Session.wake(task_id) 恢复
  → 证明 Harness 可重启、Session 独立

Hand crash test:
  杀掉 desktop hand / remote hand
  Session 历史仍完整
  hand 恢复后继续执行
  → 证明 Hands 可替换

Truth source test:
  删除 projection 后从 event log 重建
  → 证明 task_events 才是真相源

Security boundary test:
  任意 hand 都无法直接读到服务端真实凭证
  → 证明 credentials never reach the hand
```

---

## 十、v3.1 全景图

```
┌────────────────────────────────────────────────────────────────────────────┐
│                        Brain / Harness（纯编排、无状态）                    │
│                                                                            │
│  TaskService        Root Graph / Coordinator        Domain Workers         │
│  ┌──────────────┐   ┌──────────────────────────┐   ┌───────────────────┐  │
│  │ 调 session   │   │ 规划 / DAG 编排 / 恢复   │   │ 领域编排 / LLM 决策 │  │
│  │ 调 gateway   │   │ 上下文整理 / prompt 策略 │   │ 调 gateway.execute │  │
│  └──────┬───────┘   └─────────────┬────────────┘   └──────────┬────────┘  │
│         │                         │                           │           │
│         └───────────────┬─────────┴───────────────┬───────────┘           │
│                         │                         │                       │
│                 session.get/emit/wake      gateway.execute()              │
├─────────────────────────┼─────────────────────────┼───────────────────────┤
│                         ▼                         ▼                       │
│                 ┌──────────────┐         ┌──────────────────┐             │
│                 │ SessionRuntime│         │   ToolGateway    │             │
│                 │               │         │                  │             │
│                 │ emit_event    │         │ route + prepare  │             │
│                 │ get_events    │         │ 唯一 tool event点 │             │
│                 │ get_projection│         └───────┬──────────┘             │
│                 │ wake          │                 │                        │
│                 └──────┬────────┘           ┌─────┴─────┐                  │
│                        │                    │           │                  │
├────────────────────────┼────────────────────┼───────────┼──────────────────┤
│                        │                    │           │                  │
│             ┌──────────┴──────────┐   ┌────┴────┐ ┌────┴────────────┐     │
│             │ task_events         │   │ Local   │ │ Desktop / Future │     │
│             │ canonical history   │   │ Hands   │ │ Remote Hands     │     │
│             ├─────────────────────┤   │ 服务端工具│ │ Tauri / Ephemeral│     │
│             │ checkpoints         │   │         │ │ Sandbox          │     │
│             │ recovery asset      │   └─────────┘ └─────────────────┘     │
│             ├─────────────────────┤                                          │
│             │ task_runs           │                                          │
│             │ projection          │                                          │
│             └─────────────────────┘                                          │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## 十一、参考文档

- Anthropic 官方博客：[Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/engineering/managed-agents)
- tauri-computer-use-plan.md
- 2026-03-20-hard-task-agent-platform-design.md
