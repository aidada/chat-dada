# chat-dada 架构演进：向 Anthropic Managed Agents 三层解耦看齐

> 日期：2026-04-09
> 目标：中期架构重构，参考 Anthropic《Scaling Managed Agents》设计哲学

---

## 一、愿景与背景

Anthropic 的 Managed Agents 核心设计哲学：**不设计一个"固定"的代理框架（harness），而是将整个系统拆解成三个高度解耦、接口极简且几乎不相互假设的组件**——**Brain（大脑）**、**Hands（双手）**、**Session（会话）**。

这种解耦的根本出发点是：**Claude 的智能能力正在指数级增长，任何把"当前模型能力限制"硬编码进框架的做法，都会迅速成为瓶颈**。

### 你的架构映射

| Anthropic 概念 | 你的中期设计 | 当前状态 |
|----------------|-------------|---------|
| **Brain** | chat-dada 后端（纯编排，无状态） | ⚠️ TaskService 耦合了 DB pool + Redis + checkpointer |
| **Hands** | Tauri 桌面应用（沙箱执行） | ✅ Phase 2 工具骨架已完成 |
| **Session** | chat-dada 后端（append-only 日志） | ⚠️ task_events 是 append-only，但 TaskRun 是可变实体 |

---

## 二、当前架构分析

### 2.1 chat-dada 后端（Brain 层）

**主要组件：**

```
agent/runtime/task_execution.py
├── TaskService
│   ├── _store: TaskRunStore (asyncpg Pool)
│   ├── _redis: aioredis.Redis
│   ├── _checkpointer: AsyncPostgresSaver
│   └── _runner_tasks: dict (内存中运行中任务)
│
agent/coordinator/
├── executor.py (DAG 执行器)
├── agent.py (理解目标节点)
└── skills.py (技能注册与调用)

core/registry.py (能力注册中心)
domain/* (业务服务层)
infra/* (数据库、OAuth、存储)
```

**问题清单：**

| 问题 | 影响 | 迁移工作量 |
|------|------|-----------|
| TaskService 持有 DB pool + Redis + checkpointer | 紧耦合，难以独立重启 Brain | 中等 |
| TaskRun 是可变实体（非 append-only） | 状态变化丢失历史，无法真正恢复 | 大 |
| request_payload JSON 存储所有状态 | 模式无结构，边界不清 | 中等 |
| 凭证是环境变量 | 无法按用户/会话隔离 | 小 |
| 工具是 Python 函数调用，无标准协议 | Hands 无法跨语言/跨环境 | 大 |

### 2.2 chat-dada-front Tauri（Hands 层）

**Phase 2 实现状态（已完成）：**

```
src-tauri/src/
├── lib.rs                    ✅ 插件架构，tools plugin 初始化
├── commands.rs               ✅ invoke_tool, list_tools 桥接
└── tools/
    ├── mod.rs               ✅ Tool trait + ToolContext + ToolResult + ToolSchema + PermissionLevel
    ├── registry.rs          ✅ Thread-safe ToolRegistry (lazy_static)
    ├── screenshot.rs        ✅ xcap crate
    ├── mouse.rs             ✅ enigo crate
    ├── keyboard.rs          ✅ enigo crate
    ├── shell.rs             ✅ portable-pty crate
    ├── clipboard.rs         ✅ arboard crate (read + write)
    ├── sysinfo.rs           ✅ sysinfo crate
    └── filesystem.rs        ✅ 7个子工具：read/write/edit/delete/list_dir/search/grep
```

**Cargo.toml 依赖：**
- `tauri = "2.10.3"`
- `tauri-plugin-shell = "2"`
- `tauri-plugin-log = "2"`
- `enigo = "0.3"` — 鼠标/键盘
- `xcap = "0.9.3"` — 截屏
- `portable-pty = "0.8"` — Shell PTY
- `arboard = "3.4"` — 剪贴板
- `sysinfo = "0.32"` — 系统信息

**未完成：**
- ❌ Phase 3 权限系统（PermissionEngine + ConfirmDialog）
- ❌ Phase 4-6 AI 服务层
- ❌ Phase 7 前端 UI

---

## 三、五大模块重构方案

### 模块 1：Brain 无状态化

**目标：** 让后端编排层完全无状态，支持任意重启/替换 Brain 实例而不影响任务状态。

**阶段 1：提取 TaskStore 接口层**

```python
# 新增 infra/stores/task_store.py
from abc import ABC, abstractmethod

class TaskStore(ABC):
    """Brain 与状态存储之间的接口"""

    @abstractmethod
    async def create_task(self, *, user_id, task_text, mode, ...) -> TaskSnapshot:
        pass

    @abstractmethod
    async def get_task(self, task_id: str) -> TaskSnapshot:
        pass

    @abstractmethod
    async def append_event(self, task_id: str, event_type: str, payload: dict) -> int:
        pass  # 返回 seq

    @abstractmethod
    async def get_events(self, task_id: str, after_seq: int = 0) -> list[TaskEvent]:
        pass

    @abstractmethod
    async def mark_started(self, task_id: str) -> None:
        pass

    @abstractmethod
    async def finish_task(self, task_id: str, status: str) -> None:
        pass

    @abstractmethod
    async def list_interrupted(self) -> list[str]:
        pass

# 实现
class PostgresTaskStore(TaskStore):
    """生产环境：使用 PostgreSQL"""
    ...

class InMemoryTaskStore(TaskStore):
    """测试环境"""
    ...
```

**阶段 2：TaskService 重构为纯调度器**

```python
class TaskService:
    def __init__(
        self,
        store: TaskStore,           # 注入而非自己创建
        redis: aioredis.Redis,
        checkpointer: CheckpointStore,
        root_graph: CompiledGraph,
    ):
        self._store = store
        self._redis = redis
        self._checkpointer = checkpointer
        self._root_graph = root_graph
        # 不再持有 _runner_tasks dict，改用 store + redis 追踪
```

**阶段 3：支持多 Brain 实例**

- 引入 Redis Stream 或 PostgreSQL advisory lock 作为任务队列
- 任意 Brain 实例可以认领并执行任务
- Brain 重启不影响任务状态（状态在 store 中）

**改动量：** 中等。需要重构 `agent/runtime/task_execution.py` 和新增接口定义。

---

### 模块 2：Session 事件溯源

**目标：** 把 TaskRun 从可变实体改为"快照"，所有状态变化来自 append-only 事件日志，支持 wake/resume。

**当前状态：**

```
TaskRun 表（可变）
├── status: 'running' → 'succeeded'  (UPDATE)
├── result_text: '...'               (覆盖)
└── request_payload: JSON             (塞满所有状态)

task_events 表（append-only，但非真相来源）
├── seq: 1, 2, 3, ...
├── event_type: 'step', 'error', ...
└── payload: JSON
```

**目标状态：**

```
TaskSession 表（只存储元数据）
├── session_id: PK
├── task_id: str
├── status: str  (从事件日志计算得出)
├── created_at, updated_at
└── last_seq: int  (最后消费的 event seq)

task_events 表（真相来源，append-only）
├── task_id: str
├── seq: int  (每条递增)
├── event_type: str  (标准化事件类型)
├── payload: JSON
├── is_snapshot: bool  (定期快照标记)
└── created_at: datetime

标准事件类型：
  - TaskCreated { task_text, mode, thinking_level }
  - TaskStarted {}
  - ToolCallStarted { tool_name, params }
  - ToolCallFinished { tool_name, result }
  - ToolCallFailed { tool_name, error }
  - TaskWaitingForUser { question }
  - TaskResumed { user_response }
  - TaskCheckpoint { checkpoint_id, state_summary }
  - TaskCompleted { result }
  - TaskFailed { error }
```

**wake/resume 实现：**

```python
async def wake(session_id: str) -> CoordinatorState:
    """从事件日志重建状态并继续执行"""
    events = await store.get_events(session_id)

    # 找到最后一个快照
    snapshot = find_last_snapshot(events)
    state = snapshot.payload  # 重建状态

    # replay 非快照事件
    for event in events_after(snapshot):
        state = apply_event(state, event)

    return state  # 继续执行
```

**改动量：** 大。需要新增表、修改事件写入逻辑、重构恢复机制。

---

### 模块 3：Hands ↔ Brain 协议

**目标：** 定义标准 `execute(tool_name, params)` 接口，让 Hands 对 Brain 完全黑盒，支持跨语言/跨环境。

**当前状态：**

```
工具是 Python 函数，在 domain graph 里直接调用：
  agent/tools/web_search.py  →  async def run(input_data)
  agent/tools/code_executor.py  →  subprocess.run()

无标准的 execute(tool_name, params) 接口
```

**目标协议：**

```typescript
// Brain → Hands 请求
interface ExecuteToolRequest {
    request_id: string;      // 请求 ID，用于追踪
    tool_name: string;       // 'screenshot', 'shell', ...
    params: Record<string, any>;  // 工具参数
    session_id: string;       // 会话 ID
    timeout_ms: number;      // 超时
}

// Hands → Brain 响应
interface ExecuteToolResponse {
    request_id: string;
    success: boolean;
    output: string;           // 工具输出
    artifacts: Artifact[];    // 产物（截图、文件路径等）
    error?: string;
    execution_time_ms: number;
}

// 标准事件
interface ToolEvent {
    type: 'tool_call' | 'tool_result' | 'tool_error';
    request_id: string;
    session_id: string;
    tool_name: string;
    timestamp: number;
}
```

**Brain 侧改造：**

```python
# 新增 agent/gateway/tool_gateway.py
class ToolGateway:
    """Brain 与 Hands 之间的工具调用网关"""

    def __init__(self, redis: aioredis.Redis, store: TaskStore):
        self._redis = redis
        self._store = store

    async def execute_tool(
        self,
        tool_name: str,
        params: dict,
        session_id: str,
        timeout_ms: int = 30000,
    ) -> ExecuteToolResponse:
        request_id = str(uuid.uuid4())

        # 发送请求到 Hands
        await self._redis.xadd(
            f"hands:{session_id}:commands",
            {"request_id": request_id, "tool_name": tool_name, "params": json.dumps(params)},
        )

        # 等待响应
        result = await self._redis.xread(
            {f"hands:{session_id}:results": request_id},
            timeout=timeout_ms / 1000,
        )

        return parse_response(result)

    async def on_tool_result(self, event: ToolEvent) -> None:
        """从 Hands 接收工具执行结果"""
        await self._store.append_event(
            session_id,
            event.type,
            {"request_id": event.request_id, "tool_name": event.tool_name, "output": event.output}
        )
```

**Tauri 侧改造：**

```rust
// src-tauri/src/commands.rs (已实现部分)
// 新增：订阅 Redis Stream 接收 Brain 请求
// 新增：主动推送结果到 Redis Stream

#[tauri::command]
pub fn invoke_tool(name: String, params: Value) -> Result<ToolResult, String> {
    // 当前：直接执行
    // 目标：通过 Redis Stream 与 Brain 通信
}
```

**改动量：** 大。需要重新设计工具调用链路，新增消息协议。

---

### 模块 4：MCP 凭证管理

**目标：** 凭证外部化，支持按用户/会话隔离，结构性安全（凭证永不进沙箱）。

**当前状态：**

```python
# agent/tools/brave_search.py
api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
```

**目标方案：**

```python
# 新增 core/vault.py
from abc import ABC, abstractmethod

class VaultService(ABC):
    """凭证存储服务"""

    @abstractmethod
    async def get(self, user_id: str, key: str) -> str | None:
        """获取凭证"""
        pass

    @abstractmethod
    async def set(self, user_id: str, key: str, value: str) -> None:
        """存储凭证（加密）"""
        pass

    @abstractmethod
    async def delete(self, user_id: str, key: str) -> None:
        """删除凭证"""
        pass

# 实现
class EnvVarVault(VaultService):
    """向后兼容：仍从环境变量读取"""
    ...

class PostgresVault(VaultService):
    """生产环境：PostgreSQL 加密存储"""
    def __init__(self, encryption_key: bytes):
        ...

class ExternalVault(VaultService):
    """未来支持：HashiCorp Vault"""
    ...
```

**工具凭证获取改造：**

```python
# 改造后的工具签名
async def brave_search(input_data: dict, ctx: ToolContext) -> dict:
    # 不再直接读环境变量
    api_key = await ctx.vault.get(ctx.user_id, "BRAVE_SEARCH_API_KEY")
    if not api_key:
        return {"status": "ok", "result": "(BRAVE_SEARCH_API_KEY not configured)"}
    ...
```

**结构性安全（未来）：**

```
凭证永不进入 Hands（沙箱）：
- Hands 只接收临时的、范围受限的临时 token
- 敏感操作通过 Brain 代理
- 沙箱被攻破也拿不到真实凭证
```

**改动量：** 中等。需要新增 VaultService 接口，修改所有工具的凭证获取方式。

---

### 模块 5：Tauri Hands 实现

**当前状态（Phase 2 已完成）：**

| 工具 | 文件 | 状态 | Permission |
|------|------|------|------------|
| Screenshot | screenshot.rs | ✅ | Safe |
| Mouse | mouse.rs | ✅ | Cautious |
| Keyboard | keyboard.rs | ✅ | Cautious |
| Shell | shell.rs | ✅ | Dangerous |
| Clipboard Read | clipboard.rs | ✅ | Cautious |
| Clipboard Write | clipboard.rs | ✅ | Cautious |
| SysInfo | sysinfo.rs | ✅ | Safe |
| File Read | filesystem.rs | ✅ | Safe |
| File Write | filesystem.rs | ✅ | Cautious |
| File Edit | filesystem.rs | ✅ | Cautious |
| File Delete | filesystem.rs | ✅ | Dangerous |
| List Dir | filesystem.rs | ✅ | Safe |
| File Search | filesystem.rs | ✅ | Safe |
| Grep | filesystem.rs | ✅ | Safe |

**未完成：**

| Phase | 内容 | 状态 |
|-------|------|------|
| Phase 3 | 权限系统（PermissionEngine, ConfirmDialog） | ❌ |
| Phase 4 | AI 服务层（Provider 接口，多供应商） | ❌ |
| Phase 5 | Agent 循环引擎 | ❌ |
| Phase 6 | 前端 UI | ❌ |
| Phase 7 | 全局快捷键、托盘、通知等 | ❌ |

**改动量：** 按 tauri-computer-use-plan.md 分阶段执行即可。

---

## 四、完整演进路线图

```
短期（让项目跑起来）
═══════════════════════════════════════════════════════════
chat-dada-front:
  └─ Phase 3-7 完成 Tauri 工具和 UI
      ├── Phase 3: 权限系统
      ├── Phase 4: AI 服务层
      ├── Phase 5: Agent 循环引擎
      ├── Phase 6: 前端 UI
      └── Phase 7: 高级功能

中期第一阶段：Brain 无状态化
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 提取 TaskStore trait (interface)
  ├─ 重构 TaskService 为纯调度器（依赖注入）
  ├─ 实现 PostgresTaskStore
  └─ 支持多 Brain 实例（Redis Stream 任务队列）

中期第二阶段：Hands ↔ Brain 协议
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 定义 ToolProtocol (ExecuteToolRequest/Response)
  └─ 新增 ToolGateway（Redis Stream 驱动）

chat-dada-front:
  ├─ Tauri 订阅 Redis Stream 接收 Brain 请求
  └─ Tauri 推送结果到 Redis Stream

中期第三阶段：Session 事件溯源
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 新增 TaskSession 表（元数据）
  ├─ 事件类型标准化（TaskCreated, ToolCallStarted...）
  ├─ 实现 wake/resume（从快照 replay）
  └─ 故障恢复（服务重启后自动恢复 running 任务）

中期第四阶段：MCP 凭证管理
═══════════════════════════════════════════════════════════
chat-dada:
  ├─ 新增 VaultService 接口
  ├─ 实现 PostgresVault（加密存储）
  ├─ 迁移所有工具到 vault 获取凭证
  └─ 结构性安全设计

长期（随模型进化）
═══════════════════════════════════════════════════════════
- 多 Brain 实例负载均衡
- Hands 可以是 Tauri（桌面）也可以是 Docker 容器（远程）
- Session 后端可替换（PostgreSQL → etcd）
- 结构性安全：凭证注入 + 沙箱隔离
```

---

## 五、关键决策点

| 决策 | 选项 | 建议 |
|------|------|------|
| **Session 存储** | PostgreSQL vs 专用日志系统 | PostgreSQL（你的 infra 已有） |
| **Brain↔Hands 通信** | Redis Stream vs WebSocket vs gRPC | Redis Stream（你的 infra 已有 Redis） |
| **Vault 后端** | PostgreSQL vs HashiCorp Vault | 先 PostgreSQL，未来可迁移 |
| **工具执行模式** | Tauri 直接执行 vs 远程 Agent 调用 | 双模式：本地 Desktop Agent + 远程 Web Agent |
| **重启恢复策略** | replay 完成 vs 标记人工确认 | 默认 replay，复杂情况人工确认 |

---

## 六、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 事件溯源改动大 | 可能破坏现有功能 | 分阶段，每阶段可独立验证 |
| Session wake/resume 复杂 | 实现难度高 | 利用 LangGraph checkpointer，简化自研 |
| Tauri 工具需要 macOS 权限 | 用户体验摩擦 | Onboarding 引导授权 |
| 多 Brain 实例需要任务队列 | 运维复杂度增加 | 先单机，确有需要再上 Redis Stream |

---

## 七、参考文档

- Anthropic 官方博客：《Scaling Managed Agents: Decoupling the brain from the hands》
- tauri-computer-use-plan.md — Tauri 桌面应用完整实施计划
- 2026-03-20-hard-task-agent-platform-design.md — 现有架构设计文档
