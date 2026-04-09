# chat-dada 架构演进：向 Anthropic Managed Agents 三层解耦看齐（v2）

> 日期：2026-04-09
> 版本：v2（基于代码深度审查后修正）
> 目标：中期架构重构，参考 Anthropic《Scaling Managed Agents》设计哲学

---

## 一、愿景与背景

Anthropic 的 Managed Agents 核心设计哲学：**不设计一个"固定"的代理框架（harness），而是将整个系统拆解成三个高度解耦、接口极简且几乎不相互假设的组件**——**Brain（大脑）**、**Hands（双手）**、**Session（会话）**。

这种解耦的根本出发点是：**Claude 的智能能力正在指数级增长，任何把"当前模型能力限制"硬编码进框架的做法，都会迅速成为瓶颈**。

### 架构映射

| Anthropic 概念 | chat-dada 设计 | 当前状态 | 核心问题 |
|----------------|---------------|---------|---------|
| **Brain** | chat-dada 后端（纯编排） | ⚠️ TaskService 混合了编排+持久化+内存状态 | asyncpg 直连、_runner_tasks 内存 dict |
| **Hands** | Tauri 桌面应用（沙箱执行） | ✅ Phase 2 工具骨架已完成 | Brain 无法触发 Hands 工具执行 |
| **Session** | task_events append-only 日志 + LangGraph checkpoint | ⚠️ Coordinator 用 MemorySaver（崩溃即丢） | 崩溃恢复粒度为零 |

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

### Phase 2：TaskService 解耦（为多实例打基础）

**目标**：砍掉 TaskService 错误设计，拆分编排与持久化，消除内存状态对多实例的阻碍。

**Step 2.1：砍掉 TaskRunStore 的 asyncpg 直连**

```python
# 删除：task_execution.py L85-92
class TaskRunStore:  # ← 整个类删除
    def __init__(self, database_url: str) -> None:
        self.pool: asyncpg.Pool | None = None
    async def connect(self):
        self.pool = await asyncpg.create_pool(...)

# 替代：所有 DB 操作统一通过 SessionFactory + Repository
async with SessionFactory() as session:
    repo = TaskRunRepository(session)
    await repo.create(task_id=..., ...)
    await session.commit()
```

Repository 已经是正确的边界，无需新建抽象层。

**Step 2.2：砍掉 _runner_tasks 内存 dict**

```python
# 删除：
self._runner_tasks: dict[str, asyncio.Task] = {}  # ← 删除

# 任务追踪：task_runs.status = 'running' 就是记录
# 任务取消：Redis PubSub 发取消信号
async def cancel_task(self, task_id: str):
    await self._redis.publish(f"task:{task_id}:cancel", "cancel")
    # 执行侧在 streaming 循环中监听此 channel，协作取消
```

**Step 2.3：拆解 request_payload JSONB**

当前 `request_payload` 塞了过多状态。Phase 1 完成后，拆分方案：

| 原 request_payload 字段 | 迁移目标 | 原因 |
|------------------------|---------|------|
| `interrupt_state` / `_dag_resume_state` | 删除，由 coordinator checkpoint 管理 | Phase 1 后不再需要手动序列化 |
| `clarification_history` | 写入 `task_events`（类型 `user_reply`） | 天然 append-only |
| `latest_checkpoint_id` | `task_runs` 新增列 `latest_checkpoint_id TEXT` | 独立字段，可索引 |
| `file_paths`, `conversation_id` 等 | 保留在 `request_payload` | 不可变元数据，合理 |

`request_payload` 退化为仅存储原始请求参数。

**Step 2.4：TaskService 重构为纯编排器**

```python
# 改前：
class TaskService:
    def __init__(self, database_url: str, redis_url: str):
        self._store = TaskRunStore(database_url)     # 自建 asyncpg pool
        self._runner_tasks: dict = {}                 # 内存追踪
        self._background_tasks: set = set()           # 内存追踪
        self._checkpointer = None                     # 自建
        self._root_graph = None                       # 自建

# 改后：
class TaskService:
    def __init__(
        self,
        redis: aioredis.Redis,           # 注入
        checkpointer: AsyncPostgresSaver, # 注入
        root_graph: CompiledGraph,        # 注入
    ):
        self._redis = redis
        self._checkpointer = checkpointer
        self._root_graph = root_graph
        # 无内存状态追踪——DB 是唯一 truth
        # 所有 DB 操作通过 SessionFactory + Repository

# web/runtime.py 初始化：
checkpointer = await open_checkpointer(settings.database_url)
root_graph = build_root_graph(checkpointer=checkpointer)
redis = aioredis.from_url(settings.redis_url)
task_service = TaskService(redis=redis, checkpointer=checkpointer, root_graph=root_graph)
```

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `agent/runtime/task_execution.py` | 删 TaskRunStore 类，删 _runner_tasks/\_background_tasks，重构 __init__ |
| `web/runtime.py` | TaskService 初始化方式重构 |
| `scripts/init.sql` | task_runs 新增 `latest_checkpoint_id` 列 |

---

### Phase 3：事件标准化 + Brain↔Hands 协议

**目标**：标准化事件类型用于审计和可观测性；复用 SSE + HTTP POST 连通桌面端工具。

**Step 3.1：标准化事件类型**

当前 `event_type` 是自由文本（`start`、`step`、`error`、`file`、`result` 等），改为标准枚举：

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

- `TaskEventRepository.append()` 添加白名单验证
- 前端 `useTaskStream.ts` 适配新类型（向后兼容：未知类型 fallback 到 `step` 处理逻辑）

**Step 3.2：Brain↔Hands 协议——复用 SSE + HTTP POST**

> **技术决策**：不用 Redis Stream 直连桌面（安全问题：凭证嵌入二进制），不用 WebSocket（当前低频事件不需要全双工）。复用已有 SSE 推送 + HTTP POST 回传模式，零新基础设施。

**协议结构：**

```typescript
// Brain → Hands（通过 SSE 事件推送）
interface ExecuteToolRequest {
    request_id: string;
    tool_name: string;       // 'screenshot', 'shell', 'mouse', ...
    params: Record<string, any>;
    task_id: string;
    timeout_ms: number;
}

// Hands → Brain（通过 HTTP POST 回传）
interface ExecuteToolResponse {
    request_id: string;
    success: boolean;
    output: string;
    artifacts: Artifact[];   // 截图、文件路径等
    error?: string;
    execution_time_ms: number;
}
```

**调用流程：**

```
Brain 执行到需要桌面工具
  ↓
发出 tool_call_started 事件（进入 task_events → Redis PubSub → SSE 推送）
  ↓
Tauri 订阅 SSE 流，收到 tool_call_started 事件
  ↓
Tauri 调用本地 REGISTRY.execute(tool_name, params)
  ↓
POST /tasks/{task_id}/tool_result 回传结果
  ↓
Brain ToolGateway 通过 Redis PubSub 监听 task:{task_id}:tool_results
  ↓
收到结果，继续执行
```

这与当前 `question → user_reply` 的交互模式完全对称。

**Brain 侧新增：**

```python
# agent/gateway/tool_gateway.py
class ToolGateway:
    """Brain 与 Hands 之间的工具调用网关"""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis

    async def execute_remote_tool(
        self,
        tool_name: str,
        params: dict,
        task_id: str,
        timeout_ms: int = 30000,
    ) -> ExecuteToolResponse:
        request_id = str(uuid.uuid4())

        # 发出 tool_call 事件（通过已有的 record_event → SSE 推送链路）
        await record_event(task_id, "tool_call_started", {
            "request_id": request_id,
            "tool_name": tool_name,
            "params": params,
            "timeout_ms": timeout_ms,
        })

        # 等待 Hands POST 回传结果
        result = await self._wait_for_tool_result(
            task_id, request_id, timeout_ms
        )
        return result

    async def _wait_for_tool_result(self, task_id, request_id, timeout_ms):
        """监听 Redis PubSub channel 等待 tool_result"""
        channel = f"task:{task_id}:tool_results"
        # ... asyncio.wait_for with timeout ...
```

```python
# web/routers/tasks.py 新增端点
@router.post("/tasks/{task_id}/tool_result")
async def submit_tool_result(task_id: str, body: ToolResultBody):
    """Hands 回传工具执行结果"""
    await record_event(task_id, "tool_call_finished", body.dict())
    await redis.publish(f"task:{task_id}:tool_results", json.dumps(body.dict()))
```

**Step 3.3：区分 Brain 端工具 vs Hands 端工具**

| 工具类型 | 执行位置 | 示例 | 调用方式 |
|---------|---------|------|---------|
| Brain 端（服务端 API） | Brain 进程内直接执行 | web_search, code_executor | 直接函数调用（现有方式） |
| Hands 端（桌面操作） | Tauri 桌面应用执行 | screenshot, mouse, keyboard, shell, filesystem | 通过 ToolGateway → SSE → POST |

`ToolGateway` 内部路由：根据工具注册的 `execution_target`（`brain` / `hands`）决定走哪条路径。

**改动文件清单：**

| 文件 | 改动 |
|------|------|
| `infra/db/repositories/task_event_repo.py` | 事件类型白名单验证 |
| `agent/gateway/tool_gateway.py` | 新增 ToolGateway |
| `web/routers/tasks.py` | 新增 `POST /tasks/{id}/tool_result` 端点 |
| `chat-dada-front/src/hooks/useTaskStream.ts` | 适配新事件类型 |
| `chat-dada-front/src-tauri/src/` | 新增 SSE 订阅 + tool_call 监听 + POST 回传 |

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

Phase 2：TaskService 解耦（可与 Phase 1 并行）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 2.1 砍掉 TaskRunStore asyncpg 直连（统一 Repository）
  ├─ 2.2 砍掉 _runner_tasks 内存 dict（改 DB + Redis PubSub）
  ├─ 2.3 拆解 request_payload JSONB
  └─ 2.4 TaskService 重构为纯编排器（依赖 2.1-2.3）

Phase 3：事件标准化 + Brain↔Hands 协议（可与 Phase 1-2 并行）
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 3.1 标准化事件类型枚举
  ├─ 3.2 ToolGateway + SSE/POST 协议
  └─ 3.3 Brain/Hands 工具路由
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
Phase 1                    Phase 2                   Phase 3
───────────────           ─────────────────         ────────────────
1.1 Coordinator持久化 ─┐   2.1 砍asyncpg ──────┐     3.1 事件标准化
1.2 _recover改resume ←─┘   2.2 砍_runner_tasks  ├→ 2.4 TaskService重构
1.3 State字段验证           2.3 拆request_payload┘    3.2 ToolGateway
                                                     3.3 工具路由
     ↕ 可并行                    ↕ 可并行                ↕ 可并行
```

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

---

## 六、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Coordinator checkpoint 数据量增大 | DB 写入增加 | LangGraph 按 channel 增量写入，非全量快照；可定期清理旧 checkpoint |
| Resume 后 DAG 状态不一致 | 恢复行为异常 | Phase 1.3 验证 state 字段完整性 + 集成测试（kill -9 后验证恢复） |
| 砍掉 _runner_tasks 后取消能力 | 需要新的取消机制 | Redis PubSub 取消信号 + 执行侧协作取消 |
| SSE 工具调用延迟 | Hands 端工具响应慢 | 超时机制（timeout_ms）+ fallback 标记 tool_call_failed |
| Tauri 工具需要 macOS 权限 | 用户体验摩擦 | Onboarding 引导授权 |

---

## 七、验证计划

- **Phase 1 验证**：启动 DAG 任务（≥3 skill），在第 2 轮 gather 中 `kill -9` 进程，重启后确认：第 1 轮完成的 skill 不重跑（task_events 无重复 `skill_started`）；第 2 轮 skill 从头执行；最终结果与不崩溃时一致
- **Phase 2 验证**：`grep -r "asyncpg" agent/` 确认零直接导入；并发提交 + 取消任务测试
- **Phase 3 验证**：Tauri 收到 `tool_call_started` SSE 事件后执行本地工具，Brain 等待并收到 `tool_result`，完整链路 e2e 测试

---

## 八、参考文档

- Anthropic 官方博客：《Scaling Managed Agents: Decoupling the brain from the hands》
- tauri-computer-use-plan.md — Tauri 桌面应用完整实施计划
- 2026-03-20-hard-task-agent-platform-design.md — 现有架构设计文档
