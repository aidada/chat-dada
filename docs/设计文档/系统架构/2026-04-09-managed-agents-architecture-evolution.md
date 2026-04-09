# chat-dada 架构演进：向 Anthropic Managed Agents 三层解耦看齐（v3）

> 日期：2026-04-09
> 版本：v3（彻底达成三层解耦——Brain 纯编排 / Hands 纯执行 / Session 独立组件）
> 目标：中期架构重构，参考 Anthropic《Scaling Managed Agents》设计哲学

---

## 一、愿景与背景

Anthropic 的 Managed Agents 核心设计哲学：**不设计一个"固定"的代理框架（harness），而是将整个系统拆解成三个高度解耦、接口极简且几乎不相互假设的组件**——**Brain（大脑）**、**Hands（双手）**、**Session（会话）**。

这种解耦的根本出发点是：**Claude 的智能能力正在指数级增长，任何把"当前模型能力限制"硬编码进框架的做法，都会迅速成为瓶颈**。

### 架构映射

| Anthropic 概念 | chat-dada 当前 | 当前状态 | 核心问题 | v3 目标 |
|----------------|---------------|---------|----------|--------|
| **Brain** | TaskService + Coordinator + Domain Workers | ⚠️ 混合编排+持久化+工具调用 | asyncpg 直连、直写 Session、直调工具函数 | 纯编排：只调 SessionManager.emit() + ToolGateway.execute() |
| **Hands** | Tauri 桌面工具 + 服务端工具（web_search 等） | ⚠️ 桌面工具休眠；服务端工具无统一协议 | Brain 直接函数调用，无标准接口 | 统一 ToolProtocol：local/remote 执行器，Brain 零直调 |
| **Session** | task_events + task_runs + LangGraph checkpoint（分散） | ❌ 无独立组件；Brain 直写 DB；MemorySaver 崩溃即丢 | task_runs 可变；事件写入由 Brain 驱动 | 独立 SessionManager：唯一状态写入者，状态机验证 |

---

## 二、当前架构分析

### 2.1 chat-dada 后端（Brain 层）

**主要组件：**

```
agent/runtime/task_execution.py
├── TaskRunStore (asyncpg Pool ← 绕过 Repository 直连 DB)
├── TaskService
│   ├── _store: TaskRunStore
│   ├── _redis: aioredis.Redis
│   ├── _checkpointer: AsyncPostgresSaver
│   ├── _root_graph: CompiledGraph
│   ├── _runner_tasks: dict (内存中运行中任务追踪)
│   └── _background_tasks: set (后台任务集合)
│
agent/runtime/root_graph.py
├── build_root_graph() → 3 个节点：normalize_input → run_coordinator → persist_summary
└── run_coordinator() → 每次执行新建 coordinator_graph（使用 MemorySaver）
│
agent/coordinator/
├── agent.py → build_coordinator_graph() → 9 个节点 + 1 个循环（MemorySaver）
├── executor.py → execute_tasks_node() → asyncio.gather 并行 skill 执行
└── skills.py → 全局 skill_registry 单例

infra/db/repositories/ (已有正确的 Repository 模式)
├── task_repo.py → TaskRunRepository (注入 AsyncSession)
└── task_event_repo.py → TaskEventRepository (注入 AsyncSession)
```

**问题清单：**

| 问题 | 根本原因 | 影响 |
|------|---------|------|
| TaskRunStore 绕过 Repository 直连 asyncpg | 历史遗留，与 SQLAlchemy Repository 并存 | 两套 DB 连接池，维护负担 |
| _runner_tasks 内存 dict 追踪任务 | 无锁并发写，单实例限制 | 阻塞多 Brain 实例 |
| request_payload JSONB 万能口袋 | interrupt_state、pending_question 等全塞进去 | 无结构、无验证、难以迁移 |
| **Coordinator 使用 MemorySaver** | agent.py L319 硬编码 | **崩溃后 coordinator 内部状态全丢** |
| **_recover_interrupted_tasks 标记 failed** | task_execution.py L125 直接 finish_task("failed") | **崩溃恢复粒度为零** |
| Brain↔Hands 无连接 | Tauri 工具完全休眠 | 桌面操作能力不可用 |
| **Brain 直写 Session 状态** | TaskService 直接调用 Repository 写 task_runs/task_events | Brain 与 Session 紧耦合，无法独立演进 |
| **服务端工具直接函数调用** | Domain Workers 直接 `await web_search.run()` | 无统一工具协议，Brain 混入 Hands 职责 |
| **Domain Workers 混合编排+执行** | `orchestrated.py` 既做 LLM 决策又调工具 | Brain/Hands 边界模糊 |
| **无独立 Session 组件** | 事件写入散落在 TaskService、executor、domain workers 中 | 多处直写，无状态机验证，状态不一致风险 |

### 2.2 恢复粒度深度分析

**Root Graph 检查点机制（AsyncPostgresSaver）：**

```
START → [checkpoint] → normalize_input → [checkpoint] → run_coordinator → [checkpoint] → persist_summary → END
```

Root graph 仅 3 个节点，checkpoint 保存的是节点边界的 `RootState` 快照。

**Coordinator Graph（MemorySaver，不持久化）：**

```
understand_goal → [route] → decompose_tasks → assign_skills → execute_tasks → handle_task_result → check_dependencies → [loop / synthesize]
```

9 个节点 + 1 个循环。因为使用 MemorySaver，**所有 coordinator 内部状态在进程崩溃后不可恢复**。

**execute_tasks_node 内部行为：**

```python
# executor.py L381
task_results = await asyncio.gather(*[run_one_task(t) for t in ready_tasks])
# 一轮 gather 并行 3-5 个 skill
# gather 返回后，结果写入 completed_tasks / task_vars
# → 然后返回到 coordinator graph → checkpoint 保存（如果是持久化 checkpointer）
```

**当前崩溃场景（DAG 10 个 skill，分 3 轮执行，第 2 轮中崩溃）：**

| 状态 | 当前行为 | 目标行为 |
|------|---------|---------|
| 第 1 轮完成的 skill | ❌ 重跑 | ✅ 跳过 |
| 第 2 轮正在执行的 skill | ❌ 标记 failed | ✅ 从头重跑该 skill |
| 第 3 轮未开始的 skill | ❌ 标记 failed | ✅ 正常执行 |
| _recover 行为 | 标记 failed，提示重新提交 | 从 checkpoint resume |

### 2.3 chat-dada-front Tauri（Hands 层）

**Phase 2 实现状态（已完成，13 个工具）：**

```
src-tauri/src/tools/
├── mod.rs        ✅ Tool trait + ToolContext + ToolResult + ToolSchema + PermissionLevel
├── registry.rs   ✅ Thread-safe ToolRegistry (RwLock + lazy_static)
├── screenshot.rs ✅ xcap (Safe)
├── mouse.rs      ✅ enigo (Cautious)
├── keyboard.rs   ✅ enigo (Cautious)
├── shell.rs      ✅ portable-pty (Dangerous)
├── clipboard.rs  ✅ arboard read+write (Cautious)
├── sysinfo.rs    ✅ sysinfo (Safe)
└── filesystem.rs ✅ 7 个子工具 (Safe/Cautious/Dangerous)
```

**关键缺口**：Brain 无法触发任何 Tauri 工具。当前调用链 `Frontend → Tauri IPC → Tool` 处于休眠状态，后端与 Tauri 之间零连接。

### 2.4 三层解耦差距分析（v2 遗留）

v2 方案解决了崩溃恢复和 TaskService 内存状态问题，但**三层解耦仍未彻底达成**：

| 层 | v2 达成度 | 未解决的问题 |
|----|----------|------------|
| **Brain** | ~70% | TaskService 仍直接调用 Repository 写 task_runs/task_events（混入 Session 职责）；Domain Workers 直接调用 `web_search.run()` 等工具函数（混入 Hands 职责） |
| **Hands** | Tauri 95% / 服务端 30% | Tauri 工具已完成但未激活；**服务端工具（web_search、code_executor 等）无标准协议**，被 Brain 直接函数调用 |
| **Session** | ~50% | **无独立 Session 组件**；task_runs 由 Brain 直接可变写入；事件散落在 TaskService、executor、domain workers 多处写入；无状态机验证 |

**三个必须弥合的裂缝：**

```
裂缝 1：Brain → Session 直写
──────────────────────────────────
  当前：TaskService._execute_task() 直接调 repo.update_status(), record_event()
  目标：TaskService 只调 session.transition() / session.emit()
  方案：提取独立的 SessionManager 组件

裂缝 2：Brain → Hands 直接函数调用
──────────────────────────────────
  当前：domain workers 直接 await web_search.run(query)
  目标：所有工具调用统一走 ToolGateway.execute(ToolCall)
  方案：统一 ToolProtocol + LocalExecutor / RemoteExecutor

裂缝 3：Session 无独立边界
──────────────────────────────────
  当前：task_runs 被 Brain 多处直接 UPDATE；事件多处 INSERT
  目标：SessionManager 是唯一合法的状态写入者
  方案：SessionManager 封装状态转换 + 事件记录 + 状态机验证
```

---

## 三、四阶段重构方案

### Phase 1：崩溃恢复（最高优先级）

**目标**：进程崩溃后恢复到最后完成的 DAG 循环轮次，而非标记 failed。

> **恢复粒度**：DAG 循环轮次级。一轮 gather 并行 3-5 个 skill，最坏丢失当前这一轮的执行时间。

**Step 1.1：Coordinator checkpointer 持久化**

```python
# 改前：agent/coordinator/agent.py L319
from langgraph.checkpoint.memory import MemorySaver
return graph.compile(checkpointer=MemorySaver(), name="coordinator_graph")

# 改后：接收外部注入的持久化 checkpointer
def build_coordinator_graph(checkpointer):
    graph = StateGraph(CoordinatorState)
    # ... 9 个节点定义不变 ...
    return graph.compile(checkpointer=checkpointer, name="coordinator_graph")
```

```python
# 改前：agent/runtime/root_graph.py run_coordinator()
coordinator_graph = build_coordinator_graph()

# 改后：传入 checkpointer（复用 root graph 的 AsyncPostgresSaver 实例）
# LangGraph 通过 thread_id + checkpoint_ns 自动隔离不同子图
coordinator_graph = build_coordinator_graph(checkpointer=state["_checkpointer"])
```

**效果**：coordinator 的 9 个节点每个边界都持久化。`execute_tasks → handle_task_result` 完成后，已完成 skill 的结果被 checkpoint 保存。

**Step 1.2：_recover_interrupted_tasks 改为 checkpoint resume**

```python
# 改前：task_execution.py L125
message = "任务因服务重启而中断，请重新提交。"
await self.finish_task(task_id, "failed")

# 改后：
async def _recover_interrupted_tasks(self) -> None:
    task_ids = await self._list_interrupted_task_ids()
    for task_id in task_ids:
        snapshot = await self.get_task(task_id)
        status = snapshot.get("status")

        if status == "waiting_for_user":
            continue  # 等待用户回复，不动

        if status == "queued":
            # 还没开始，重新提交
            asyncio.create_task(self._execute_task(task_id))
            continue

        if status == "running":
            # 尝试从 checkpoint resume
            try:
                # 传入 None 作为 input，LangGraph 自动从最后 checkpoint 恢复
                asyncio.create_task(self._execute_task(task_id, resume_from_crash=True))
            except Exception:
                # checkpoint 损坏/不存在，fallback 标记 failed
                await self.set_error_text(task_id, "任务恢复失败，请重新提交。")
                await self.finish_task(task_id, "failed")
```

```python
# _execute_task 新增 resume_from_crash 分支
async def _execute_task(self, task_id: str, *, resume_from_crash: bool = False, ...):
    config = {"configurable": {"thread_id": task_id}}
    if resume_from_crash:
        # LangGraph: 传 None input + 已有 thread_id → 从 checkpoint 继续
        stream_input = None
    else:
        stream_input = initial_state
    async for part in self._root_graph.astream(stream_input, config=config, ...):
        ...
```

**Step 1.3：确认 CoordinatorState 字段持久化**

需验证以下字段都在 `CoordinatorState` TypedDict 中声明（而非局部变量），才能被 checkpointer 持久化：

- `completed_tasks: dict[str, Task]`
- `failed_tasks: dict[str, Task]`
- `task_vars: dict[str, TaskVarEntry]`
- `skill_runs: dict[str, dict]`
- `task_dag: list[Task]`

如有遗漏，需提升到 `CoordinatorState` 中。同时确认 `execute_tasks_node` 在 resume 时能正确读取 `completed_tasks` 并跳过已完成的 skill。

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/coordinator/agent.py` L319 | `MemorySaver()` → 接收注入 checkpointer |
| `agent/runtime/root_graph.py` L18-98 | 传递 checkpointer 给 coordinator |
| `agent/runtime/task_execution.py` L102-138 | _recover 改为 checkpoint resume |
| `agent/coordinator/state.py` | 确认所有恢复所需字段在 state 中 |

---

### Phase 2：Session 层独立 + TaskService 解耦

**目标**：提取独立的 SessionManager 组件作为唯一合法的状态写入者；砍掉 TaskService 错误设计，Brain 彻底不碰 DB 写入。

> **核心原则**：Brain 只做决策和编排，Session 写入全部委托给 SessionManager。Brain 调用 `session.transition()` 和 `session.emit()`，永不直接操作 Repository。

**Step 2.1：提取 SessionManager——独立 Session 组件**

```python
# 新增：agent/session/manager.py
class SessionManager:
    """独立 Session 层组件——task_runs / task_events 的唯一合法写入者。
    
    职责：
    1. task_runs 状态转换（带状态机验证）
    2. task_events 追加（带 EventType 白名单验证）
    3. 事件发布到 Redis PubSub（驱动 SSE 推送链路）
    4. 只读查询接口（给 Brain / API 层使用）
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: aioredis.Redis,
    ):
        self._session_factory = session_factory
        self._redis = redis

    # ── 写入接口（仅此二者可修改 task 状态） ──────────────

    async def transition(
        self,
        task_id: str,
        new_status: str,
        *,
        reason: str = "",
        error_text: str | None = None,
    ) -> None:
        """状态转换：验证合法性 → 写 task_runs → 写事件 → 发布。"""
        async with self._session_factory() as session:
            repo = TaskRunRepository(session)
            task = await repo.get(task_id)
            self._validate_transition(task.status, new_status)

            await repo.update_status(task_id, new_status, error_text=error_text)

            event_repo = TaskEventRepository(session)
            await event_repo.append(
                task_id, f"task_{new_status}",
                {"reason": reason, "from_status": task.status},
            )
            await session.commit()

        await self._publish(task_id, f"task_{new_status}", {"reason": reason})

    async def emit(
        self,
        task_id: str,
        event_type: EventType,
        payload: dict,
    ) -> None:
        """记录事件：写 task_events → 发布到 Redis PubSub。"""
        async with self._session_factory() as session:
            repo = TaskEventRepository(session)
            await repo.append(task_id, event_type.value, payload)
            await session.commit()

        await self._publish(task_id, event_type.value, payload)

    # ── 只读接口 ──────────────────────────────

    async def get_state(self, task_id: str) -> TaskRunRead: ...
    async def get_events(self, task_id: str, *, after_seq: int = 0) -> list[TaskEvent]: ...
    async def create_task(self, task_id: str, **kwargs) -> TaskRunRead: ...

    # ── 内部 ─────────────────────────────────

    async def _publish(self, task_id: str, event_type: str, payload: dict):
        channel = f"task:{task_id}:events"
        await self._redis.publish(channel, json.dumps({
            "event_type": event_type, **payload,
        }))

    VALID_TRANSITIONS = {
        "queued":            {"running", "cancelled"},
        "running":           {"completed", "failed", "waiting_for_user", "cancelled"},
        "waiting_for_user":  {"running", "cancelled"},
    }

    @classmethod
    def _validate_transition(cls, current: str, target: str) -> None:
        allowed = cls.VALID_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise InvalidTransitionError(
                f"task_runs: {current} → {target} 不合法，"
                f"允许: {allowed}"
            )
```

**关键设计决策**：

| 决策 | 理由 |
|------|------|
| SessionManager 自己持有 `session_factory` | Session 层独立拥有 DB 写入权限，不依赖 Brain 注入 session |
| 状态机验证 `_validate_transition` | 防止 Brain 多处直写导致非法状态迁移（如 `completed → running`） |
| `emit()` 同时写 DB + 发布 Redis | 原子性：事件写入和推送绑定，不会出现"写了 DB 但没推送"的不一致 |
| `create_task` 也在 SessionManager 中 | 任务创建也是 Session 状态变更，Brain 不应直接 INSERT |

**Step 2.2：砍掉 TaskRunStore 的 asyncpg 直连**

```python
# 删除：task_execution.py L85-92
class TaskRunStore:  # ← 整个类删除
    def __init__(self, database_url: str) -> None:
        self.pool: asyncpg.Pool | None = None
    async def connect(self):
        self.pool = await asyncpg.create_pool(...)

# 替代：所有 task 状态操作统一通过 SessionManager
await self._session.transition(task_id, "running")
await self._session.emit(task_id, EventType.SKILL_STARTED, {...})
```

**Step 2.3：砍掉 _runner_tasks 内存 dict**

```python
# 删除：
self._runner_tasks: dict[str, asyncio.Task] = {}  # ← 删除

# 任务追踪：task_runs.status = 'running' 就是记录（由 SessionManager 维护）
# 任务取消：Redis PubSub 发取消信号
async def cancel_task(self, task_id: str):
    await self._session.transition(task_id, "cancelled", reason="user_cancel")
    await self._redis.publish(f"task:{task_id}:cancel", "cancel")
```

**Step 2.4：拆解 request_payload JSONB**

| 原 request_payload 字段 | 迁移目标 | 原因 |
|------------------------|---------|------|
| `interrupt_state` / `_dag_resume_state` | 删除，由 coordinator checkpoint 管理 | Phase 1 后不再需要手动序列化 |
| `clarification_history` | `session.emit(task_id, EventType.USER_REPLY, ...)` | 天然 append-only 事件 |
| `latest_checkpoint_id` | `task_runs` 新增列 `latest_checkpoint_id TEXT` | 独立字段，可索引 |
| `file_paths`, `conversation_id` 等 | 保留在 `request_payload` | 不可变元数据，合理 |

**Step 2.5：TaskService 重构为纯编排器**

```python
# 改前：
class TaskService:
    def __init__(self, database_url: str, redis_url: str):
        self._store = TaskRunStore(database_url)     # 自建 asyncpg pool
        self._runner_tasks: dict = {}                 # 内存追踪
        self._background_tasks: set = set()           # 内存追踪
        self._checkpointer = None                     # 自建
        self._root_graph = None                       # 自建

    async def _execute_task(self, task_id, ...):
        await self._store.update_status(task_id, "running")   # ← 直写 DB
        await self.record_event(task_id, "start", {...})       # ← 直写 event
        ...

# 改后：
class TaskService:
    """纯 Brain 编排器：不持有 DB 写入权限，所有状态变更委托 SessionManager。"""

    def __init__(
        self,
        session: SessionManager,             # Session 层
        tool_gateway: ToolGateway,           # Hands 层统一入口（Phase 3）
        checkpointer: AsyncPostgresSaver,    # 注入
        root_graph: CompiledGraph,           # 注入
        redis: aioredis.Redis,               # 仅用于取消信号等编排协调
    ):
        self._session = session              # Session 层唯一依赖
        self._tool_gateway = tool_gateway    # Hands 层唯一依赖
        self._checkpointer = checkpointer
        self._root_graph = root_graph
        self._redis = redis
        # 零内存状态：无 _store、无 _runner_tasks、无 _background_tasks

    async def _execute_task(self, task_id, ...):
        await self._session.transition(task_id, "running")         # ← 委托 Session
        await self._session.emit(task_id, EventType.TASK_STARTED, {...})  # ← 委托 Session
        config = {
            "configurable": {
                "thread_id": task_id,
                "session": self._session,         # 传入 graph，供节点使用
                "tool_gateway": self._tool_gateway,  # 传入 graph，供节点使用
            }
        }
        async for part in self._root_graph.astream(initial_state, config=config, ...):
            ...
        await self._session.transition(task_id, "completed")       # ← 委托 Session
```

```python
# web/runtime.py 初始化：
session_factory = get_session_factory(settings.database_url)
redis = aioredis.from_url(settings.redis_url)
checkpointer = await open_checkpointer(settings.database_url)

session_manager = SessionManager(session_factory, redis)
tool_gateway = ToolGateway(session=session_manager, redis=redis)  # Phase 3
root_graph = build_root_graph(checkpointer=checkpointer)

task_service = TaskService(
    session=session_manager,
    tool_gateway=tool_gateway,
    checkpointer=checkpointer,
    root_graph=root_graph,
    redis=redis,
)
```

**Brain → Session 隔离验证规则**：

```
✅ Brain 可以调用：session.transition(), session.emit(), session.get_state(), session.get_events()
❌ Brain 不可以直接：TaskRunRepository.update_status(), TaskEventRepository.append(), repo.commit()
```

Phase 2 完成后执行代码审计：`grep -rn "TaskRunRepository\|TaskEventRepository" agent/` 结果应为零。

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/session/manager.py` | **新增** SessionManager |
| `agent/session/__init__.py` | **新增** 导出 |
| `agent/runtime/task_execution.py` | 删 TaskRunStore，删 _runner_tasks，删所有直接 repo 调用，改为调 SessionManager |
| `agent/coordinator/executor.py` | `record_event` 改为 `config["session"].emit(...)` |
| `agent/domains/*/orchestrated.py` | `record_event` 改为 `config["session"].emit(...)` |
| `web/runtime.py` | 初始化 SessionManager，注入 TaskService |
| `scripts/init.sql` | task_runs 新增 `latest_checkpoint_id` 列 |

---

### Phase 3：统一 ToolProtocol + Brain↔Hands 完全解耦

**目标**：所有工具调用（服务端 + 桌面端）统一走 ToolProtocol 接口；Domain Workers 拆分为纯编排 + 纯执行；Brain 零直接函数调用工具。

> **核心原则**：Brain 只调 `tool_gateway.execute(ToolCall)`，永不 `await web_search.run()`。工具在哪执行由 ToolGateway 内部路由决定，Brain 不感知。

**Step 3.1：统一 ToolProtocol 接口**

```python
# 新增：agent/tools/protocol.py

@dataclass
class ToolCall:
    """统一工具调用请求——Brain 端和 Hands 端共用。"""
    tool_name: str
    params: dict[str, Any]
    task_id: str
    timeout_ms: int = 30000

@dataclass
class ToolResult:
    """统一工具执行结果。"""
    success: bool
    output: str
    artifacts: list[dict] = field(default_factory=list)  # 截图、文件路径等
    error: str | None = None
    execution_time_ms: int = 0

class ToolExecutor(Protocol):
    """工具执行器协议——local 和 remote 实现此接口。"""
    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult: ...
```

**Step 3.2：LocalExecutor——服务端工具统一封装**

```python
# 新增：agent/tools/local_executor.py
class LocalToolExecutor:
    """Brain 进程内工具执行器——将现有函数调用封装为 ToolProtocol。"""

    def __init__(self):
        self._tools: dict[str, Callable] = {}

    def register(self, name: str, fn: Callable):
        self._tools[name] = fn

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        fn = self._tools.get(call.tool_name)
        if fn is None:
            return ToolResult(success=False, error=f"unknown tool: {call.tool_name}")
        try:
            start = time.monotonic()
            result = await fn(**call.params)
            elapsed = int((time.monotonic() - start) * 1000)
            return ToolResult(success=True, output=str(result), execution_time_ms=elapsed)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

# 注册现有工具（zero migration cost——函数签名不变）：
local_executor = LocalToolExecutor()
local_executor.register("web_search", web_search.run)
local_executor.register("exa_search", exa_search.run)
local_executor.register("code_executor", code_executor.run)
local_executor.register("image_gen", image_gen.run)
local_executor.register("academic_search", academic_search.run)
local_executor.register("summarizer", summarizer.run)
local_executor.register("translator", translator.run)
```

**Step 3.3：RemoteExecutor——桌面工具 SSE+POST 协议**

> **技术决策**：不用 Redis Stream 直连桌面（安全问题：凭证嵌入二进制），不用 WebSocket（低频事件不需全双工）。复用已有 SSE + HTTP POST。

```python
# 新增：agent/tools/remote_executor.py
class RemoteToolExecutor:
    """Hands 端（Tauri 桌面）工具执行器——SSE 推送请求 + POST 回传结果。"""

    def __init__(self, session: SessionManager, redis: aioredis.Redis):
        self._session = session
        self._redis = redis

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        request_id = str(uuid.uuid4())

        # 1. 通过 SessionManager 记录事件（→ Redis PubSub → SSE 推送到 Tauri）
        await self._session.emit(call.task_id, EventType.TOOL_CALL_STARTED, {
            "request_id": request_id,
            "tool_name": call.tool_name,
            "params": call.params,
            "timeout_ms": call.timeout_ms,
        })

        # 2. 等待 Tauri POST /tool_result 回传
        result = await self._wait_for_result(call.task_id, request_id, call.timeout_ms)
        return result

    async def _wait_for_result(self, task_id, request_id, timeout_ms) -> ToolResult:
        channel = f"task:{task_id}:tool_results"
        # asyncio.wait_for + Redis PubSub subscribe + filter by request_id
        ...
```

**协议结构（与 v2 相同）：**

```typescript
// Brain → Hands（通过 SSE 事件推送）
interface ExecuteToolRequest {
    request_id: string;
    tool_name: string;
    params: Record<string, any>;
    task_id: string;
    timeout_ms: number;
}

// Hands → Brain（通过 HTTP POST）
interface ExecuteToolResponse {
    request_id: string;
    success: boolean;
    output: string;
    artifacts: Artifact[];
    error?: string;
    execution_time_ms: number;
}
```

**Step 3.4：ToolGateway——统一路由 + 自动事件记录**

```python
# 新增：agent/gateway/tool_gateway.py
class ToolGateway:
    """统一工具网关——Brain 调用工具的唯一入口。
    
    Brain 不需要知道工具在哪执行。ToolGateway 根据注册信息自动路由：
    - local: 进程内直接执行（web_search, code_executor 等）
    - remote: SSE→Tauri→POST 回传（screenshot, mouse, keyboard 等）
    """

    def __init__(
        self,
        local: LocalToolExecutor,
        remote: RemoteToolExecutor,
        session: SessionManager,
    ):
        self._local = local
        self._remote = remote
        self._session = session
        self._routing: dict[str, str] = {}  # tool_name → "local" | "remote"

    def register_route(self, tool_name: str, target: Literal["local", "remote"]):
        self._routing[tool_name] = target

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        target = self._routing.get(call.tool_name, "local")
        executor = self._local if target == "local" else self._remote

        # 自动记录 tool_call 事件（Brain 不需要手动 emit）
        await self._session.emit(call.task_id, EventType.TOOL_CALL_STARTED, {
            "tool_name": call.tool_name,
            "target": target,
        })

        result = await executor.execute(call, ctx)

        event_type = EventType.TOOL_CALL_FINISHED if result.success else EventType.TOOL_CALL_FAILED
        await self._session.emit(call.task_id, event_type, {
            "tool_name": call.tool_name,
            "execution_time_ms": result.execution_time_ms,
            "error": result.error,
        })
        return result
```

**Brain 调用示例（Domain Worker 内）：**

```python
# 改前：agent/domains/research/orchestrated.py
async def run_research_domain_orchestrated(state, config):
    results = await web_search.run(query)               # ← 直接函数调用
    summary = await summarizer.run(results)              # ← 直接函数调用
    await record_event(task_id, "step", {"msg": "..."})  # ← 直写 Session

# 改后：
async def run_research_domain_orchestrated(state, config):
    gateway: ToolGateway = config["configurable"]["tool_gateway"]
    session: SessionManager = config["configurable"]["session"]
    ctx = ToolContext(user_id=state["user_id"], task_id=state["task_id"], trace_id="...")

    results = await gateway.execute(
        ToolCall("web_search", {"query": query}, state["task_id"]), ctx
    )
    summary = await gateway.execute(
        ToolCall("summarizer", {"text": results.output}, state["task_id"]), ctx
    )
    await session.emit(state["task_id"], EventType.SKILL_FINISHED, {"msg": "..."})
```

**Step 3.5：Domain Workers 拆分——纯编排 vs 纯执行**

当前 `agent/domains/*/orchestrated.py` 混合了 Brain（LLM 决策+编排）和 Hands（工具调用），需拆分：

```
改前（混合）：
agent/domains/research/orchestrated.py
├── LLM 决策（research plan → subtasks）          ← Brain
├── await web_search.run(query)                    ← Hands（直接函数调用）
├── await summarizer.run(text)                     ← Hands（直接函数调用）
├── await record_event(task_id, "step", {...})     ← Session（直接 DB 写入）
└── return final_result                            ← Brain

改后（拆分）：
agent/domains/research/orchestrated.py（纯 Brain）
├── LLM 决策（research plan → subtasks）          ← Brain ✓
├── await gateway.execute(ToolCall("web_search", ...))  ← 通过 ToolGateway
├── await gateway.execute(ToolCall("summarizer", ...))  ← 通过 ToolGateway
├── await session.emit(task_id, EventType.SKILL_FINISHED, {...})  ← 通过 SessionManager
└── return final_result                            ← Brain ✓
```

**验证规则**：

```
✅ Domain Workers 可以调用：gateway.execute(), session.emit(), LLM 调用
❌ Domain Workers 不可以直接：web_search.run(), record_event(), repo.append()
```

Phase 3 完成后执行代码审计：
- `grep -rn "from agent.tools.web_search import\|from agent.tools.exa_search import" agent/domains/` 结果应为零
- `grep -rn "record_event" agent/domains/` 结果应为零

**Step 3.6：标准化事件类型**

```python
class EventType(str, Enum):
    # 任务生命周期
    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    # Skill 执行
    SKILL_STARTED = "skill_started"
    SKILL_FINISHED = "skill_finished"
    SKILL_FAILED = "skill_failed"
    # 工具调用
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_FINISHED = "tool_call_finished"
    TOOL_CALL_FAILED = "tool_call_failed"
    # DAG 编排
    DAG_ITERATION_STARTED = "dag_iteration_started"
    DAG_ITERATION_FINISHED = "dag_iteration_finished"
    # 交互
    USER_QUESTION = "user_question"
    USER_REPLY = "user_reply"
    # 状态
    CHECKPOINT_SAVED = "checkpoint_saved"
    MONITORING = "monitoring"
    FILE = "file"
```

**Step 3.7：Tauri 侧 SSE 订阅 + POST 回传**

```
Tauri 启动时订阅 SSE 流
  ↓
收到 tool_call_started 事件（含 request_id, tool_name, params）
  ↓
调用本地 REGISTRY.execute(tool_name, params)
  ↓
POST /tasks/{task_id}/tool_result 回传 ExecuteToolResponse
  ↓
Brain RemoteToolExecutor 通过 Redis PubSub 收到结果，继续执行
```

```python
# web/routers/tasks.py 新增端点
@router.post("/tasks/{task_id}/tool_result")
async def submit_tool_result(task_id: str, body: ToolResultBody):
    """Hands 回传工具执行结果"""
    await session_manager.emit(task_id, EventType.TOOL_CALL_FINISHED, body.dict())
    await redis.publish(f"task:{task_id}:tool_results", json.dumps(body.dict()))
```

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/tools/protocol.py` | **新增** ToolCall, ToolResult, ToolExecutor, ToolContext |
| `agent/tools/local_executor.py` | **新增** LocalToolExecutor + 注册现有工具 |
| `agent/tools/remote_executor.py` | **新增** RemoteToolExecutor |
| `agent/gateway/tool_gateway.py` | **新增** ToolGateway 统一路由 |
| `agent/domains/*/orchestrated.py` | 砍掉所有直接 tool import，改为 gateway.execute() |
| `infra/db/repositories/task_event_repo.py` | EventType 白名单验证 |
| `web/routers/tasks.py` | 新增 `POST /tasks/{id}/tool_result` |
| `chat-dada-front/src/hooks/useTaskStream.ts` | 适配新事件类型 |
| `chat-dada-front/src-tauri/src/` | SSE 订阅 + tool_call 监听 + POST 回传 |

---

### Phase 4：凭证注入 + 可观测性（按需）

**目标**：统一工具上下文，为多用户凭证隔离做准备。当前单用户阶段优先级低。

**Step 4.1：ToolContext 统一工具上下文**

```python
@dataclass
class ToolContext:
    user_id: str
    task_id: str
    trace_id: str

    def get_secret(self, key: str) -> str | None:
        """当前阶段：读环境变量。未来：接入 VaultService"""
        return os.environ.get(key)
```

所有 Brain 端工具签名加 `ctx: ToolContext` 参数，凭证通过 `ctx.get_secret()` 获取。

**Step 4.2：凭证外部化（多用户时再做）**

- 服务端：`ctx.get_secret()` 后端从 PostgresVault 或 EnvVarVault 获取
- 桌面端：OS Keychain（macOS Keychain / Windows Credential Manager / Linux libsecret）
- 结构性安全：凭证永不进入 Hands 沙箱，敏感操作通过 Brain 代理

---

## 四、演进路线图

```
Phase 1：崩溃恢复（最高优先级）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 1.1 Coordinator checkpointer 持久化（MemorySaver → AsyncPostgresSaver）
  ├─ 1.2 _recover_interrupted_tasks 改为 checkpoint resume
  └─ 1.3 确认 CoordinatorState 字段完整性

Phase 2：Session 层独立 + TaskService 解耦（解决 Session 无独立组件）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 2.1 提取 SessionManager（唯一合法状态写入者 + 状态机验证）
  ├─ 2.2 砍掉 TaskRunStore asyncpg 直连
  ├─ 2.3 砍掉 _runner_tasks 内存 dict
  ├─ 2.4 拆解 request_payload JSONB
  └─ 2.5 TaskService 重构为纯编排器（仅调 SessionManager + ToolGateway）

Phase 3：统一 ToolProtocol + Domain Workers 拆分（解决 Hands 层不完整）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 3.1 定义 ToolCall / ToolResult / ToolExecutor 协议
  ├─ 3.2 LocalExecutor（封装 web_search、code_executor 等服务端工具）
  ├─ 3.3 RemoteExecutor（SSE 推送 + POST 回传，对接 Tauri）
  ├─ 3.4 ToolGateway 统一路由（local / remote 自动决策）
  ├─ 3.5 Domain Workers 改造：砍掉直接工具导入，改为 gateway.execute()
  ├─ 3.6 标准化 EventType 枚举
  └─ 3.7 Tauri 侧 SSE 订阅 + POST 回传
chat-dada-front:
  ├─ Tauri 订阅 SSE 接收 tool_call 事件
  └─ Tauri POST /tool_result 回传结果

Phase 4：凭证注入（按需，不阻塞任何模块）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 4.1 ToolContext 统一工具上下文
  └─ 4.2 VaultService 多用户凭证隔离

长期（随模型进化）
═══════════════════════════════════════════════════════════
- 多 Brain 实例（PG SKIP LOCKED 任务认领）
- Hands 可以是 Tauri（桌面）也可以是 Docker 容器（远程）
- 结构性安全：凭证注入 + 沙箱隔离
```

**依赖关系与并行度：**

```
Phase 1                    Phase 2                              Phase 3
───────────────           ──────────────────────              ───────────────────
1.1 Coordinator持久化 ─┐   2.1 SessionManager ─────┐           3.1 ToolProtocol 定义
1.2 _recover改resume ←─┘   2.2 砍asyncpg           │           3.2 LocalExecutor
1.3 State字段验证           2.3 砍_runner_tasks     ├→ 2.5      3.3 RemoteExecutor
                           2.4 拆request_payload ──┘            3.4 ToolGateway ←─── 依赖 2.1 (SessionManager)
                                                                3.5 Domain Workers 改造 ←── 依赖 3.4
     ↕ 可并行          ← Phase 2 + Phase 3 可并行启动 →          3.6 EventType 标准化
                                                                3.7 Tauri SSE+POST
```

**三层解耦达成检查点：**

| 阶段 | Brain 解耦度 | Hands 解耦度 | Session 解耦度 |
|------|------------|-------------|---------------|
| Phase 1 完成 | ~70%（仍直写 DB） | ~30%（Tauri 未激活） | ~50%（Checkpoint 持久化了，但无独立组件） |
| Phase 2 完成 | **~95%**（不碰 DB，纯调 SessionManager） | ~30% | **~95%**（SessionManager 独立，状态机验证） |
| Phase 3 完成 | **100%**（不碰工具函数，纯调 ToolGateway） | **100%**（统一 ToolProtocol，local+remote） | **100%**（事件标准化，白名单验证） |

---

## 五、关键决策与理由

| 决策 | 结论 | 放弃的方案 | 理由 |
|------|------|-----------|------|
| **崩溃恢复** | LangGraph checkpoint resume | 自研 event replay | Coordinator checkpoint 天然支持 node 级恢复，不重复造轮子 |
| **恢复粒度** | DAG 循环轮次级 | 单 skill 级 / skill 内部级 | 每轮 gather 3-5 个 skill，最坏丢一轮；更细粒度需在 gather 内插 checkpoint，复杂度不值得 |
| **Brain↔Hands 通信** | SSE + HTTP POST | Redis Stream / WebSocket | SSE 复用已有推送链路；Redis 凭证嵌入桌面二进制有安全风险；低频事件不需 WebSocket 全双工 |
| **DB 访问** | 统一 Repository 模式 | 新增 TaskStore 抽象层 | Repository 已经是正确的边界，新增抽象层是多余的一层 |
| **事件溯源** | 仅审计 + 推送 | 驱动状态恢复 | 恢复由 LangGraph checkpoint 负责；事件做审计、调试、前端展示 |
| **Vault 后端** | 先 env var + ToolContext | PostgresVault | 当前单用户阶段不紧急，ToolContext 接口预留扩展点 |
| **Session 独立组件** | SessionManager（唯一写入者） | Brain 直接调 Repository | Brain 多处直写导致状态不一致、无法加状态机验证、Session 无独立边界 |
| **服务端工具协议** | 统一 ToolProtocol（local + remote） | 服务端直接函数调用 | 直接函数调用使 Brain 混入 Hands 职责；统一协议后 Brain 不感知工具在哪执行 |
| **Domain Workers 拆分** | 纯编排（Brain），工具走 ToolGateway | 混合编排+执行 | orchestrated.py 同时 import 工具 + 调 LLM + 写事件，三层全混；拆分后职责清晰 |

---

## 六、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Coordinator checkpoint 数据量增大 | DB 写入增加 | LangGraph 按 channel 增量写入，非全量快照；可定期清理旧 checkpoint |
| Resume 后 DAG 状态不一致 | 恢复行为异常 | Phase 1.3 验证 state 字段完整性 + 集成测试（kill -9 后验证恢复） |
| 砍掉 _runner_tasks 后取消能力 | 需要新的取消机制 | Redis PubSub 取消信号 + 执行侧协作取消 |
| SSE 工具调用延迟 | Hands 端工具响应慢 | 超时机制（timeout_ms）+ fallback 标记 tool_call_failed |
| Tauri 工具需要 macOS 权限 | 用户体验摩擦 | Onboarding 引导授权 |
| SessionManager 成为写入瓶颈 | 高并发下 Session 写入延迟 | SessionManager 内部无锁（每次 new session），可水平扩展；Redis PubSub 本身轻量 |
| LocalExecutor 封装开销 | 间接调用增加约 0.1ms | 工具调用本身耗时 100ms-30s，0.1ms 可忽略；统一接口带来的解耦收益远大于微小开销 |
| Domain Workers 改造范围大 | 需要修改所有 orchestrated.py | 改动模式统一（import 替换 + 加 gateway 参数）；可逐个 domain 迁移，不需一次性全改 |
| 状态机验证误判 | 合法的状态转换被拒绝 | `VALID_TRANSITIONS` 表是白名单，先 audit 现有所有转换路径再上线；单测覆盖所有路径 |

---

## 七、验证计划

**Phase 1 验证：崩溃恢复**
- 启动 DAG 任务（≥3 skill），在第 2 轮 gather 中 `kill -9` 进程
- 重启后确认：第 1 轮完成的 skill 不重跑（task_events 无重复 `skill_started`）；第 2 轮 skill 从头执行
- 最终结果与不崩溃时一致

**Phase 2 验证：Session 层独立**
- `grep -rn "TaskRunRepository\|TaskEventRepository" agent/` → 结果应为零（Brain 层零直接 repo 调用）
- `grep -r "asyncpg" agent/` → 确认零直接导入
- 注入 Mock SessionManager 运行 TaskService，验证 Brain 不依赖真实 DB
- 故意调用非法状态转换（如 `completed → running`），确认 `InvalidTransitionError` 抛出
- 并发提交 + 取消任务测试

**Phase 3 验证：Hands 层统一 ToolProtocol**
- `grep -rn "from agent.tools.web_search import\|from agent.tools.exa_search import" agent/domains/` → 结果应为零
- `grep -rn "record_event" agent/domains/` → 结果应为零
- 注入 Mock ToolGateway 运行 Domain Worker，验证 Brain 不依赖真实工具实现
- Tauri 收到 `tool_call_started` SSE 事件后执行本地工具，Brain 等待并收到 `tool_result`，完整链路 e2e 测试
- 同一个 ToolCall 分别路由到 local 和 remote，验证结果格式一致

**三层完全解耦最终验证：**

```
Brain 可替换性测试：
  Mock SessionManager + Mock ToolGateway → TaskService + Coordinator 全流程通过
  → Brain 不依赖任何真实 Session 或 Hands 实现

Session 可替换性测试：
  替换 SessionManager 后端为 InMemorySessionManager → 全流程通过
  → Session 不假设 PostgreSQL

Hands 可替换性测试：
  替换 LocalExecutor 为 MockExecutor → Domain Workers 全流程通过
  替换 RemoteExecutor 的 SSE/POST 为 直接内存回调 → 全流程通过
  → Hands 不假设具体传输协议
```

---

## 八、v3 三层解耦全景图

```
┌─────────────────────────────────────────────────────────────────────┐
│                           Brain（纯编排）                            │
│                                                                     │
│  TaskService         Coordinator         Domain Workers             │
│  ┌───────────┐      ┌───────────────┐   ┌──────────────────┐       │
│  │ 调度任务   │      │ LLM 决策      │   │ LLM 决策（研究计划）│       │
│  │ 调 session │──┐   │ DAG 编排      │   │ 调 gateway.execute │       │
│  │ 调 gateway │  │   │ Skill 分发    │   │ 调 session.emit    │       │
│  └───────────┘  │   └───────────────┘   └──────────────────┘       │
│                 │                                                    │
│  ❌ 不碰 DB    ❌ 不碰 Repository   ❌ 不直接 import 工具函数        │
├─────────┬───────┴────────────────────────────────┬──────────────────┤
│         │  session.transition()                   │  gateway.execute()
│         │  session.emit()                         │  → ToolCall
│         │  session.get_state()                    │  ← ToolResult
│         ▼                                        ▼                  │
│  ┌──────────────┐                     ┌──────────────────┐         │
│  │SessionManager│                     │   ToolGateway    │         │
│  │              │                     │                  │         │
│  │ 状态机验证   │                     │  route(tool_name)│         │
│  │ task_runs 写 │                     │  ┌─────┐ ┌─────┐│         │
│  │ task_events 写│                    │  │Local│ │Remot││         │
│  │ Redis 发布   │                     │  │Exec │ │eExec││         │
│  └──────────────┘                     │  └──┬──┘ └──┬──┘│         │
│    Session 层                         └─────┼───────┼───┘         │
│    （独立组件，唯一写入者）                    │       │ Hands 层    │
├─────────────────────────────────────────────┼───────┼──────────────┤
│                                             │       │              │
│  ┌──────────┐  ┌──────────┐            ┌────┴───┐ ┌─┴──────────┐  │
│  │PostgreSQL│  │  Redis   │            │服务端   │ │ Tauri 桌面 │  │
│  │task_runs │  │ PubSub   │            │工具     │ │ 13个Rust工具│  │
│  │task_events│ │ SSE 推送 │            │web_srch │ │ screenshot │  │
│  │checkpoint│  │          │            │code_exec│ │ mouse/kbd  │  │
│  └──────────┘  └──────────┘            └────────┘ └────────────┘  │
│                Infrastructure                    Hands 执行层      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 九、参考文档

- Anthropic 官方博客：《Scaling Managed Agents: Decoupling the brain from the hands》
- tauri-computer-use-plan.md — Tauri 桌面应用完整实施计划
- 2026-03-20-hard-task-agent-platform-design.md — 现有架构设计文档
