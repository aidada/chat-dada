# Coordinator Agent PRD

**日期**: 2026-04-06
**状态**: Draft
**目标**: 为 chat-dada 引入跨领域动态团队组建能力

---

## 1. 背景与目标

### 1.1 问题

当前 chat-dada 的多智能体编排存在以下问题：

1. **领域能力封闭**：每个领域（research, patent, ppt）都是独立的工作流，无法跨领域协作
2. **预设模板限制**：依赖预设的领域路由，无法动态组建适合任务的团队
3. **DomainSpec 任务粒度错位**：DomainSpec 是一个完整工作流，不是一个可以被动态调度的"技能"

### 1.2 参考：open-multi-agent 的核心设计

```
用户目标
    │
    ▼
Coordinator Agent (理解目标 + 可用智能体列表)
    │
    ├── LLM 生成任务 DAG
    ├── 动态团队组建（谁来做）
    ├── 并行执行
    └── 汇总结果
```

**核心理念**：
- 所有智能体是**同构的** AgentConfig
- 领域能力作为**技能/工具**暴露给通用 Agent
- Coordinator 负责理解目标并动态决定谁来做

### 1.3 设计目标

1. **保留 DomainSpec 的领域优化能力** - 领域内部工作流不变
2. **对 Coordinator 暴露为工具接口** - 通过 adapter 暴露现有领域能力，避免任务粒度错位
3. **Coordinator Agent 统一规划** - 调用基础工具和领域技能
4. **支持跨领域动态协作** - 领域技能可以作为团队成员参与复杂任务

### 1.4 审查后的修正原则

针对当前项目实现，Coordinator 重构按以下原则修正：

1. **Coordinator 即通用 agent**：Coordinator 不是一个仅处理跨领域任务的额外路径，而是**统一的执行入口**，替代现有 dispatcher + general_chat + 各单领域分支 + composite 路径。LLM 在 `understand_goal` 阶段自行判断执行模式（direct / single_skill / dag），取代关键词路由。
2. **保留现有 DomainSpec / orchestrated graph 作为稳定内核**：现有 `agent/workflows/spec.py`、`agent/workflows/orchestrator.py`、`agent/platform/domain_registry.py` 在 Phase 1-3 继续保留，用作领域执行内核和技能适配基础，而不是立即删除。
3. **领域技能采用 Adapter，而不是降级为简单函数**：`do_research/do_patent/...` 是 Coordinator 视角下的 skill adapter，内部仍可调用现有 LangGraph / deepagents / workflow；skill contract 必须保留 checkpoint、human interrupt、artifact、review、budget、strategy 等结构化语义。
4. **事件与恢复协议完全兼容现有任务系统**：Coordinator 不另起一套中断/SSE 协议，必须复用现有 `stream_nested_graph()`、`translate_stream_part()`、`waiting_for_user`、resume 链路；新增事件只能是 additive，不能替代标准 `question/task/node/checkpoint` 事件。
5. **最终输出保持 RootState 契约**：所有执行模式的最终结果都必须保留 `artifact_refs`、`review`、`budget`、`research_strategy/strategy_trace`、`latest_checkpoint_id` 等字段。direct 模式下这些字段为空。
6. **复杂领域状态不强行扁平化**：`task_vars` 仅用于跨任务共享摘要结果；ResearchMemory、领域内部 checkpoint、revision/budget 状态仍由各领域工作流独立维护。
7. **删除旧实现必须延后到 Phase 4 决策**：是否移除 `workflows/*`、简化 `domain_registry`、彻底收敛内部抽象，必须以真实任务验证、事件兼容性和回归测试通过为前置条件。
8. **single_skill 模式保证单领域零开销**：单领域请求不生成 DAG、不做额外 LLM 调用，`understand_goal` 判定后直接调用对应技能，延迟与现有单领域直连路径持平。

---

## 2. 架构设计

### 2.1 整体架构

Coordinator Agent 是**唯一的执行入口**，统一处理所有类型的用户请求。不再需要 dispatcher 关键词路由和分叉执行路径。

```
                         用户目标
                              │
                              ▼
                    ┌─────────────────────────────────┐
                    │     Coordinator Agent             │
                    │   (LangGraph - 统一入口)         │
                    │                                  │
                    │  ┌───────────────────────────┐  │
                    │  │ understand_goal           │  │
                    │  │   + available_skills       │  │
                    │  │   → 判断执行模式：         │  │
                    │  │     direct / single / dag  │  │
                    │  └───────────────────────────┘  │
                    │        │         │         │    │
                    │        ▼         ▼         ▼    │
                    │    direct    single_skill   dag  │
                    │   (直接回答)  (调用1个技能)      │
                    │                            │    │
                    │              ┌──────────────┘    │
                    │              ▼                   │
                    │  ┌───────────────────────────┐  │
                    │  │ decompose_tasks           │  │
                    │  │   → LLM 生成任务 DAG      │  │
                    │  └───────────────────────────┘  │
                    │              │                  │
                    │              ▼                  │
                    │  ┌───────────────────────────┐  │
                    │  │ assign_skills            │  │
                    │  │   → 分配技能/工具        │  │
                    │  └───────────────────────────┘  │
                    └──────────────┬──────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
          ▼                        ▼                        ▼
   ┌─────────────┐        ┌─────────────┐        ┌─────────────┐
   │ 领域技能    │        │ 领域技能    │        │ 基础工具    │
   │ do_research │        │ do_patent   │        │ web_search  │
   │ (调用现有   │        │ (调用现有   │        │ file_write  │
   │  工作流)    │        │  工作流)    │        │ bash        │
   └─────────────┘        └─────────────┘        └─────────────┘
```

### 2.2 执行模式

Coordinator 在 `understand_goal` 阶段由 LLM 判断用户请求的复杂度，选择三种执行模式之一：

| 模式 | 触发条件 | 执行方式 | 示例 |
|------|---------|---------|------|
| `direct` | 简单问答、闲聊、不需要领域技能 | Coordinator LLM 直接回答，不生成 DAG | "你好"、"解释一下什么是 LangGraph" |
| `single_skill` | 明确属于单一领域 | 跳过 DAG 规划，直接调用 1 个技能 | "帮我研究量子计算的最新进展"、"写一个专利" |
| `dag` | 跨领域、多步骤、需要编排 | LLM 生成任务 DAG → 分配技能 → 执行 | "研究竞品技术方案并撰写专利"、"调研后生成 PPT" |

### 2.3 与现有架构的关系

```
root_graph.py (改造后)
    │
    ├── normalize_input
    ├── coordinator  ← 唯一执行路径
    │       │
    │       ├── direct: LLM 直接回答（替代旧 general_chat）
    │       ├── single_skill: 调用 1 个领域技能（替代旧 run_research/patent/ppt/...）
    │       └── dag: 任务 DAG 编排（替代旧 composite）
    │
    └── persist_summary
```

**关键设计原则**：
- **不再有 dispatcher 路由层**：LLM 在 `understand_goal` 阶段自行判断执行模式，取代关键词匹配
- **single_skill 模式保证单领域零开销**：不生成 DAG，不做多余的 LLM 调用，直接调用技能
- 领域技能作为**工具**被调用，而非作为**会规划的 Agent**
- Phase 1-3：领域技能优先包装现有已注册 domain runner，不要求各领域立即改写内部工作流

---

## 3. 核心组件设计

### 3.1 SkillDescription

每个领域技能需要标准化的描述，供 Coordinator 的 LLM 理解何时调用：

```python
@dataclass
class SkillDescription:
    name: str                           # "do_research"
    version: str = "1.0"              # 技能版本，用于追踪升级
    description: str                    # "执行深度研究，适合复杂多维度问题"
    input_schema: dict                  # {"query": str, "report_profile": str}
    output_schema: dict                 # {"result": str, "artifact_refs": list}
    best_for: list[str]                # ["深度研究", "文献综述", "技术调研"]
    timeout_seconds: int = 300         # 默认超时时间
    retryable: bool = True             # 是否可重试
    nested_depth_limit: int = 0        # 允许的嵌套深度，0表示不允许嵌套调用

# 示例
RESEARCH_SKILL = SkillDescription(
    name="do_research",
    version="1.0",
    description="执行深度研究工作流，适合复杂多维度问题的系统分析",
    input_schema={
        "query": "str: 研究问题或主题",
        "report_profile": "str: 产物类型 (literature_review/paper_guidance/research_proposal)"
    },
    output_schema={
        "result": "str: 研究结论",
        "artifact_refs": "list: 生成的 artifact 文件列表"
    },
    best_for=["深度研究", "文献综述", "技术调研", "对比分析"],
    timeout_seconds=600,
    retryable=True,
    nested_depth_limit=0  # 不允许嵌套调用
)
```

### 3.2 Task 模型

```python
@dataclass
class Task:
    id: str                             # 唯一任务 ID
    title: str                         # 任务标题
    description: str                     # 任务描述

    # 依赖关系 - 通过 task_id 引用，与领域无关
    depends_on: list[str]              # 依赖的 task_id 列表

    # 分配信息
    assigned_skill: str                 # "do_research", "web_search", etc.
    input_data: dict                   # 传递给技能的参数

    # 执行控制
    priority: int = 0                 # 优先级：-1=low, 0=normal, 1=high
    max_retries: int = 2              # 最大重试次数
    timeout_seconds: int = 300         # 超时时间

    # 状态
    status: Literal["pending", "running", "done", "failed"]
    result: Any = None
    error: str | None = None
    retry_count: int = 0              # 当前重试次数
    start_time: float | None = None  # 开始时间
    end_time: float | None = None     # 结束时间
```

**关键设计**：
- 依赖关系与领域无关，只与 task_id 相关
- 每个任务有独立的超时和重试控制
- 优先级用于任务调度排序

### 3.3 SkillContext / SkillResult

注意：这里的 `SkillContext` 是 **Coordinator 与现有领域工作流之间的桥接层**，不是替代领域内部状态机。Research / Patent / PPT / Zero Report 仍然各自维护内部 LangGraph state；Coordinator 只负责传递公共元数据、事件桥接和恢复信息。

```python
@dataclass
class SkillContext:
    """Coordinator -> 领域技能的桥接上下文"""
    coordinator_task_id: str            # 整个用户任务的 task_id
    skill_invocation_id: str            # 本次技能调用 ID
    skill_name: str                     # 技能名称
    trace_id: str                       # 贯穿整个 DAG 的追踪 ID

    # 与现有 task runtime / resume 语义对齐
    request_payload: dict
    clarification_history: list[dict]
    latest_checkpoint_id: str | None = None
    parent_task_id: str | None = None  # 嵌套调用时的父任务 ID

    # 跨任务共享（仅共享摘要/引用，不承接领域内部全部状态）
    task_vars: dict = field(default_factory=dict)
    upstream_artifacts: list[dict] = field(default_factory=list)

    # 事件 / 中断桥接：必须复用现有协议
    emit_stream_event: Callable[[dict[str, Any]], None] | None = None
    request_interrupt_fn: Callable[[dict[str, Any]], str | None] | None = None

    # 恢复 / 执行控制
    resume_metadata: dict[str, Any] = field(default_factory=dict)
    abort_signal: AbortSignal | None = None  # 标准 AbortSignal
    nested_depth: int = 0              # 当前嵌套深度

@dataclass
class SkillResult:
    """技能执行结果"""
    status: Literal["ok", "error", "interrupted", "timeout"]
    result: Any = None

    # RootState / TaskService 需要持久化和透出的结构化结果
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    review: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    strategy: str = ""

    # 恢复 / 调试信息
    latest_checkpoint_id: str | None = None
    checkpoint_data: dict | None = None
    resume_metadata: dict[str, Any] = field(default_factory=dict)
    raw_domain_state: dict[str, Any] | None = None

    error: str | None = None
    execution_time_seconds: float = 0.0  # 执行耗时
    cost_usd: float = 0.0               # 估算成本
```

**约束**：
- Phase 1-3 的 skill adapter 可以包装现有 `run_*_domain_orchestrated()`，不能强制要求各领域立即改写为“只有 query/result 的纯函数”。
- Coordinator 在跨任务汇总时必须保留 `artifact_refs/review/budget/strategy`，不能只抽取 `result` 文本。
- `task_vars` 只用于跨任务共享摘要结果；领域内部复杂状态仍由各自 memory / checkpoint 机制维护。

### 3.4 CoordinatorState

```python
class ExecutionMode(str, Enum):
    DIRECT = "direct"               # 直接回答，不调用技能
    SINGLE_SKILL = "single_skill"   # 调用 1 个技能，不生成 DAG
    DAG = "dag"                     # 生成任务 DAG，编排多技能

class CoordinatorState(TypedDict, total=False):
    # 输入
    original_goal: str
    trace_id: str
    config: CoordinatorConfig
    available_skills: list[SkillDescription]
    skill_summary: str                  # LLM 可读的技能摘要

    # 理解阶段
    goal_understanding: str | None
    execution_mode: ExecutionMode       # direct / single_skill / dag

    # single_skill 模式（仅 execution_mode == SINGLE_SKILL 时使用）
    selected_skill: str | None          # 选中的技能名称
    skill_input: dict | None            # 传递给技能的参数

    # dag 模式（仅 execution_mode == DAG 时使用）
    task_dag: list[Task] | None
    task_vars: dict[str, TaskVarEntry]  # 跨任务结果传递

    # 执行状态（single_skill 和 dag 模式共用）
    pending_tasks: list[str]            # 等待执行的任务 ID
    running_tasks: dict[str, Task]     # 执行中的任务
    completed_tasks: dict[str, Task]    # 已完成的任务
    failed_tasks: dict[str, Task]      # 失败的任务
    skill_runs: dict[str, dict[str, Any]]  # skill invocation 的 checkpoint / resume 状态

    # 结果（所有模式共用）
    final_result: str | None
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]
    strategy_trace: list[str]
    latest_checkpoint_id: str | None

    # 中断
    interrupt_state: dict | None
    pending_question: dict | None
```

### 3.5 Coordinator 节点与执行流

```
START → understand_goal
            │
            ├── direct ──────────────────────── direct_answer ──→ END
            │
            ├── single_skill ─── execute_single_skill ──→ END
            │
            └── dag ─── decompose_tasks ─── assign_skills
                              │
                              ▼
                        execute_tasks ←──┐
                              │          │
                              ▼          │
                     handle_task_result ──┤ (有就绪任务)
                              │          │
                              ▼          │
                     check_dependencies ──┘
                              │
                              ▼ (所有任务完成)
                          synthesize ──→ END
```

| 节点 | 职责 | LLM 调用 | 执行模式 |
|------|------|---------|---------|
| `understand_goal` | 理解用户目标，判断执行模式，提取关键约束 | 是 | 所有 |
| `direct_answer` | 直接生成回答（替代旧 general_chat） | 是 | direct |
| `execute_single_skill` | 调用 1 个技能，返回结果（无 DAG 开销） | 否 | single_skill |
| `decompose_tasks` | LLM 生成任务 DAG | 是 | dag |
| `assign_skills` | 为每个任务分配最合适的技能 | 是 | dag |
| `execute_tasks` | 并行执行就绪任务 | 否 | dag |
| `handle_task_result` | 处理单个任务结果，写入 task_vars | 否 | dag |
| `check_dependencies` | 更新任务状态，标记就绪 | 否 | dag |
| `synthesize` | 汇总结果生成最终输出 | 是 | dag |
| `handle_failure` | 处理失败任务 | 否 | dag |

**关键约束**：
- `understand_goal` 是唯一的 LLM 路由决策点，用 structured output 返回 `ExecutionMode` + 理由
- `single_skill` 模式的 `understand_goal` 同时输出 `selected_skill` 和 `skill_input`，避免额外 LLM 调用
- `direct_answer` 与 `execute_single_skill` 的输出格式与 `synthesize` 一致，均写入 `final_result` + `artifact_refs` 等字段

---

## 4. 跨领域依赖协议

### 4.1 依赖表达

```python
# 示例：跨领域研究 + 专利分析任务
tasks = [
    Task(
        id="t1",
        title="搜索竞品信息",
        assigned_skill="web_search",
        input_data={"query": "竞品技术方案"},
        depends_on=[]
    ),
    Task(
        id="t2",
        title="深度技术研究",
        assigned_skill="do_research",
        input_data={"query": "技术可行性分析"},
        depends_on=["t1"]  # 依赖搜索结果
    ),
    Task(
        id="t3",
        title="专利分析",
        assigned_skill="do_patent",
        input_data={"query": "基于研究结果的专利布局"},
        depends_on=["t2"]  # 依赖研究结论
    ),
]
```

### 4.2 结果传递

任务结果通过 `task_vars` 在依赖链中传递：

```python
# t1 (搜索) 完成后，结果存入 task_vars
task_vars["t1_result"] = search_result

# t2 (研究) 执行时，可以访问 t1 的结果
research_input = task_vars.get("t1_result", "")

# Coordinator 在任务就绪检查时，自动注入依赖结果
def is_task_ready(task: Task, completed: dict[str, Task]) -> bool:
    for dep_id in task.depends_on:
        if dep_id not in completed or completed[dep_id].status != "done":
            return False
    return True
```

### 4.3 TaskVarEntry — 跨任务结果传递契约

`task_vars` 是 Coordinator 在 DAG 任务之间传递摘要结果的唯一通道。为避免下游任务拿到的数据格式不确定，所有写入 `task_vars` 的值必须遵循 `TaskVarEntry` 契约。

```python
@dataclass
class TaskVarEntry:
    """一个已完成任务写入 task_vars 的标准结构"""

    # ── 必填 ──────────────────────────────────────────────
    summary: str
    """
    自然语言摘要，供下游任务 LLM prompt 直接引用。
    长度建议 ≤ 2000 字符；超长时由 adapter 负责截断。
    """

    # ── 可选：结构化发现 ──────────────────────────────────
    key_findings: list[str] = field(default_factory=list)
    """
    要点列表（每条 ≤ 200 字符），便于下游任务快速筛选。
    例：["GPT-4 在 MMLU 上超过人类基线", "专利 US20230001 覆盖了方案 A"]
    """

    # ── 可选：产物引用 ────────────────────────────────────
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    """
    与 RootState.artifact_refs 同构的引用列表。
    下游任务若需要引用上游生成的文件/URL，从此处获取。
    """

    # ── 可选：来源标记 ────────────────────────────────────
    source_task_id: str = ""
    source_skill: str = ""


# ── 写入规则 ──────────────────────────────────────────────
# 1. key 为 task_id（例如 "t1"），value 为 TaskVarEntry 实例。
# 2. Coordinator 在 handle_task_result 节点中，
#    从 SkillResult 提取 summary + key_findings + artifact_refs 写入。
# 3. 下游任务在 execute_tasks 阶段，Coordinator 负责把
#    depends_on 中所有前置任务的 TaskVarEntry 注入到 input_data 中。

def build_task_vars_entry(task: Task, result: SkillResult) -> TaskVarEntry:
    """从 SkillResult 构造标准的 TaskVarEntry"""
    summary = str(result.result or "")
    if len(summary) > 2000:
        summary = summary[:1997] + "..."

    return TaskVarEntry(
        summary=summary,
        key_findings=[],  # 可由 LLM 后处理提取
        artifact_refs=result.artifact_refs,
        source_task_id=task.id,
        source_skill=task.assigned_skill,
    )


def inject_upstream_context(
    task: Task,
    task_vars: dict[str, TaskVarEntry],
) -> dict[str, Any]:
    """
    为即将执行的任务注入上游依赖的摘要结果。
    返回需要合并进 input_data 的字典。
    """
    upstream_summaries: list[str] = []
    upstream_artifacts: list[dict[str, Any]] = []

    for dep_id in task.depends_on:
        entry = task_vars.get(dep_id)
        if entry is None:
            continue
        upstream_summaries.append(
            f"[{entry.source_skill}#{dep_id}] {entry.summary}"
        )
        upstream_artifacts.extend(entry.artifact_refs)

    return {
        "upstream_context": "\n\n".join(upstream_summaries),
        "upstream_artifacts": upstream_artifacts,
    }
```

**约束**：
- `task_vars` 仅存储摘要信息，不承接领域内部完整状态（ResearchMemory、patent claim tree 等）
- 领域技能返回的 `SkillResult.result` 可能很长（完整报告），`TaskVarEntry.summary` 必须截断到 2000 字符以内，避免下游 prompt 过长
- 若下游任务需要上游的详细内容（而非摘要），应通过 `artifact_refs` 引用文件，由下游技能自行读取

---

## 5. 结果汇总策略

### 5.1 汇总逻辑

```python
async def synthesize_node(state: CoordinatorState) -> dict[str, Any]:
    """
    汇总所有已完成任务的结果
    1. 按依赖顺序收集结果
    2. 检测最终任务（无其他任务依赖它）
    3. 生成最终正文
    4. 保留 artifact/review/budget 等结构化字段
    """
    completed = state["completed_tasks"]

    # 找最终任务（没有被其他任务依赖的已完成任务）
    all_deps = set()
    for task in completed.values():
        all_deps.update(task.depends_on)

    final_tasks = [
        task for task_id, task in completed.items()
        if task_id not in all_deps
    ]

    merged_artifacts = merge_artifact_refs(completed.values())
    merged_review = merge_reviews(completed.values())
    merged_budget = merge_budgets(completed.values())

    # 如果有多个最终任务，用 LLM 汇总
    if len(final_tasks) > 1:
        synthesis_prompt = build_synthesis_prompt(completed, final_tasks)
        final_text = await call_llm(synthesis_prompt)
    else:
        final_text = str(final_tasks[0].result or "")

    return {
        "final_result": final_text,
        "artifact_refs": merged_artifacts,
        "review": merged_review,
        "budget": merged_budget,
        "strategy_trace": [task.assigned_skill for task in completed.values()],
    }

def build_synthesis_prompt(completed: dict, final_tasks: list) -> str:
    results = "\n\n".join(
        f"## {t.title}\n{t.result}"
        for t in final_tasks
    )
    return f"""请汇总以下研究结果，生成一份连贯的分析报告：

{results}

要求：
1. 保持各部分的核心发现
2. 消除矛盾和重复
3. 给出综合结论
"""
```

汇总节点必须保持与 `RootState` / `TaskService` 兼容的输出契约，不能只返回正文字符串。

### 5.2 单领域 vs 跨领域汇总

| 场景 | 汇总方式 |
|------|---------|
| 单领域任务 | 领域技能直接返回结果 |
| 跨领域多最终任务 | LLM 综合多任务结果 |
| 跨领域单最终任务 | 直接使用最终任务结果 |

---

## 6. 与现有组件集成

### 6.1 事件流兼容原则

Coordinator 不能只用 `_safe_emit()` 重新定义一套事件名。Phase 1-3 必须兼容现有任务系统的标准事件协议：

- 领域技能内部仍通过 `stream_nested_graph()` 产生 `question`、`task`、`node`、`checkpoint`、`file` 等标准事件。
- Coordinator 可以额外发送 `task_dag`、`coordinator_step` 等新增事件，但它们是附加信息，不能替代标准事件。
- 对 nested domain skill 的事件透传时，必须保留 `nested_graph`、`graph_node`、`checkpoint_id`、`trace_metadata`，以便 `TaskService` 正确进入 `waiting_for_user` 并 resume。

```python
async def run_skill_via_adapter(
    skill_runner: DomainRunner,
    input_data: dict,
    context: SkillContext,
) -> SkillResult:
    """
    Adapter 内部仍调用现有 domain runner / nested graph，
    只补齐 coordinator 需要的上下文，不改写领域内部事件协议。

    关键约束：必须正确设置 interrupt bridge 上下文变量，
    否则领域技能内部的 ask_user() 调用将无法触达用户。
    参见 §6.4 SkillAdapter 中断桥接规范。
    """
    from agent.runtime.interaction import (
        set_graph_interrupt_bridge,
        reset_graph_interrupt_bridge,
    )

    # ── 设置中断桥接 ─────────────────────────────────────
    # 复用 Coordinator 传入的 request_interrupt_fn，
    # 使领域技能内部 ask_user() 能正确触发 LangGraph interrupt。
    token: Token[Any] | None = None
    if context.request_interrupt_fn is not None:
        token = set_graph_interrupt_bridge(context.request_interrupt_fn)

    try:
        result = await skill_runner(
            {
                **input_data,
                "task_id": context.coordinator_task_id,
                "clarification_history": context.clarification_history,
                "report_profile": context.request_payload.get("report_profile", ""),
            }
        )
    finally:
        if token is not None:
            reset_graph_interrupt_bridge(token)

    return result
```

### 6.2 人类介入与恢复

- Coordinator 复用现有 `waiting_for_user -> reply_to_task() -> Command(resume=...)` 链路，不引入另一套阻塞式回调模型。
- 领域 skill 若触发追问，必须产生与现有 task runtime 兼容的 `question` 事件，并把 `nested_graph / checkpoint_id / graph_node` 写回 request payload。
- Coordinator 自身只负责把回答继续传回原 skill invocation，并在必要时把 `clarification_history` 透传给下游 skill。

```python
if skill_result.status == "interrupted":
    return {
        "pending_question": skill_result.resume_metadata.get("interrupt_payload"),
        "latest_checkpoint_id": skill_result.latest_checkpoint_id,
    }
```

### 6.3 Checkpoint 共存策略

Phase 1-3 不强行把各领域 checkpoint 改造成同一套回调协议，而采用“领域内部 checkpoint + Coordinator 记录引用”的共存模式：

- 领域内部仍保留自己的 memory / checkpoint 结构，例如 ResearchMemory 与其阶段性 checkpoint。
- Coordinator 仅维护 DAG 层的任务状态，以及每个 skill invocation 的 `latest_checkpoint_id / resume_metadata`。
- Phase 4 若决定收敛内部抽象，再评估是否统一 checkpoint 接口；在此之前不要求 `research/patent/ppt/zero_report` 改写现有恢复语义。

```python
state["skill_runs"][task.id] = {
    "status": task.status,
    "latest_checkpoint_id": result.latest_checkpoint_id,
    "resume_metadata": result.resume_metadata,
}
```

### 6.4 SkillAdapter 中断桥接规范

当前 `root_graph.py` 的 `make_run_registered_domain()` 在调用领域 runner 前，通过 `set_graph_interrupt_bridge(_interrupt_bridge)` 设置上下文变量，使领域内部 `ask_user()` 能正确触发 LangGraph interrupt。Coordinator 通过 SkillAdapter 调用领域技能时必须复制此语义，否则领域技能的追问能力将静默失败。

```python
# ── SkillAdapter 必须实现的上下文变量桥接 ──────────────────

# 1. 中断桥接（必需）
#    ask_user() 依赖 _graph_interrupt_bridge 上下文变量。
#    Coordinator 节点本身运行在 LangGraph 图内，拥有 interrupt 能力；
#    SkillAdapter 需要把该能力桥接给嵌套的领域 runner。
#
#    做法：Coordinator 的 execute_tasks 节点在启动每个 skill 前，
#    构造一个 bridge 函数并通过 set_graph_interrupt_bridge() 注入。

def _make_skill_interrupt_bridge(
    coordinator_task_id: str,
    skill_invocation_id: str,
) -> GraphInterruptBridge:
    """
    构造中断桥接函数。
    领域技能调用 ask_user(payload) 时，bridge 将 payload 补齐
    Coordinator 追踪信息后，调用 request_interrupt() 暂停整个图。
    """
    def bridge(payload: dict[str, Any]) -> str:
        from agent.platform.interrupts import request_interrupt

        enriched = {
            **payload,
            "interrupt_type": "human_input",
            "coordinator_task_id": coordinator_task_id,
            "skill_invocation_id": skill_invocation_id,
        }
        return request_interrupt(enriched)

    return bridge


# 2. 事件透传（必需）
#    领域技能内部通过 stream_nested_graph() 发送事件；
#    Coordinator 不需要额外包装，因为 LangGraph 的 subgraphs=True
#    已经将嵌套事件透传到顶层 stream writer。
#    但 Coordinator 应在 extra_payload 中注入 trace_id 和 skill_invocation_id，
#    以便前端区分来自不同技能的事件。

# 3. 恢复值传递（必需）
#    当用户回答追问后，resume 值经由 root_graph → coordinator_graph
#    → Command(resume=value) 到达 Coordinator 的 execute_tasks 节点。
#    execute_tasks 需要：
#    a. 从 state["interrupt_state"] 读取 skill_invocation_id
#    b. 找到对应的 pending skill，将 resume 值注入其 SkillContext.resume_metadata
#    c. 重新调用 run_skill_via_adapter()，
#       此时 skill_runner 内部通过 stream_nested_graph() 的
#       nested_resume_value 恢复执行
```

**约束**：
- SkillAdapter **不得**自行实现阻塞式等待用户回复的逻辑，必须通过 LangGraph interrupt 暂停整个图
- Coordinator 图的 `execute_tasks` 节点在被 interrupt 暂停后，必须把 `skill_invocation_id` 和 `latest_checkpoint_id` 写入 `state["interrupt_state"]`，以支持正确恢复
- 并行执行多个技能时，同一时刻只允许一个技能触发 interrupt（先到先得），其余技能需在 interrupt 恢复后继续执行

---

## 7. 目录结构

```
agent/
├── runtime/
│   ├── root_graph.py           # 改造：简化为 normalize_input → coordinator → persist_summary
│   ├── dispatcher.py           # Phase 4 候选删除：路由逻辑由 Coordinator understand_goal 替代
│   └── task_execution.py       # 保留：事件持久化、等待用户、resume
│
├── coordinator/                # 新增：统一执行入口
│   ├── __init__.py
│   ├── agent.py               # Coordinator Agent (LangGraph)，含 direct/single_skill/dag 三模式
│   ├── state.py               # CoordinatorState + ExecutionMode
│   ├── executor.py            # DAG 执行引擎（dag 模式使用）
│   ├── skills.py              # SkillDescription + SkillAdapter 注册表
│   └── prompts.py             # Coordinator 提示词（含技能摘要、执行模式判断指引）
│
├── capabilities/
│   ├── general_chat.py        # Phase 4 候选删除：direct 模式替代
│   └── ...
│
├── tools/                     # 保留
│   └── ...
│
├── domains/
│   ├── research/
│   │   ├── orchestrated.py   # Phase 1-3 保留：由 skill adapter 调用现有入口
│   │   └── ...
│   ├── patent/
│   │   ├── orchestrated.py   # Phase 1-3 保留：由 skill adapter 调用现有入口
│   │   └── ...
│   ├── ppt/
│   │   ├── orchestrated.py   # Phase 1-3 保留：由 skill adapter 调用现有入口
│   │   └── ...
│   └── zero_report/
│       ├── orchestrated.py   # Phase 1-3 保留：由 skill adapter 调用现有入口
│       └── ...
│
├── platform/
│   ├── streaming.py           # 保留
│   ├── interrupts.py          # 保留
│   ├── domain_registry.py     # Phase 1-3 保留：现有域注册，Phase 4 合并到 skills.py
│   └── ...
│
└── workflows/
    ├── orchestrator.py        # Phase 1-3 保留：领域内部工作流继续使用
    ├── strategy_selector.py   # Phase 1-3 保留
    └── spec.py                # Phase 1-3 保留
```

---

## 8. 第 4 阶段候选收敛项

说明：以下组件在 **Phase 1-3 一律不删除**。只有当 Coordinator 在真实跨领域任务上稳定承接对应能力后，Phase 4 才评估是否删除/合并。

### 8.1 收敛对象清单

| # | 组件 | 当前职责 | 收敛目标 | Phase 4 收敛前提 |
|---|------|---------|---------|------------------|
| C1 | `agent/workflows/orchestrator.py` | 单领域 DomainOrchestrator：ANALYZE → SELECT_STRATEGY → EXECUTE → EVALUATE 循环 | 由 Coordinator DAG 执行引擎统一承接 | Coordinator 的 `execute_tasks` 已覆盖 planning / parallel / iterative / sequential 四种策略语义，且单领域路径通过回归测试 |
| C2 | `agent/workflows/strategy_selector.py` | 混合 rule+LLM 策略选择 | 合并到 Coordinator 的 `decompose_tasks + assign_skills` | 策略选择逻辑已迁入 Coordinator prompt，单领域场景退化为 1-task DAG 时策略选择结果与旧选择器一致率 ≥ 90% |
| C3 | `agent/workflows/spec.py` (`DomainSpec` + `SubagentConfig`) | 领域元数据声明：system_prompt, tools, subagents, evaluator, strategy_hints | 合并到 `SkillDescription` + 各领域 orchestrated.py 内部 | 所有 domain 元数据已迁移到 skill registry，且不再由任何路径直接 import `DomainSpec` |
| C4 | `agent/platform/domain_registry.py` (`DomainRegistry`) | 领域 runner 注册与查找 | 合并到 `coordinator/skills.py` 的 skill registry | Coordinator skill registry 完全替代 `DomainRegistry.get()` |
| C5 | `root_graph.py` 中旧节点 | `run_composite()`、`run_general_chat()`、各 `run_{domain}` 节点、`select_path` 分叉逻辑 | 全部删除，由 Coordinator 统一替代 | Phase 1 已完成替代，Phase 4 仅做清理 |
| C6 | `agent/capabilities/general_chat.py` | 直接 LLM 问答 | 删除，由 Coordinator 的 `direct_answer` 节点替代 | direct 模式回答质量与旧 general_chat 持平 |
| C7 | `agent/runtime/dispatcher.py` 路由逻辑 | 关键词匹配 → 执行路径分叉 | 删除路由部分，仅保留 `run_general_chat_task` 等工具函数（若仍被引用） | Coordinator 的 `understand_goal` 路由准确率 ≥ 旧 dispatcher |

### 8.2 收敛分组与执行顺序

收敛按依赖关系分为 3 批，每批完成后运行回归验证再进入下一批：

```
批次 A（低风险，优先执行）
├── C5  删除 root_graph.py 旧节点（Phase 1 已替代）
├── C4  合并 DomainRegistry → skill registry
├── C6  删除 general_chat.py
└── C7  删除 dispatcher.py 路由逻辑
         ↓ 回归验证 A
批次 B（中风险）
├── C3  合并 DomainSpec → SkillDescription
└── C2  合并 strategy_selector → Coordinator prompt
         ↓ 回归验证 B
批次 C（高风险，最后执行）
└── C1  删除 DomainOrchestrator
         ↓ 全量回归验证
```

注意：旧架构中 C6（单领域统一经 Coordinator）不再需要作为独立收敛项，因为 Phase 1 的 Coordinator 从一开始就是唯一执行入口，`single_skill` 模式已替代单领域直连路径。

### 8.3 各收敛项具体操作

#### C5：删除 root_graph.py 旧节点

```
前提：Phase 1 已完成，Coordinator 作为唯一执行路径已稳定运行

操作：
1. 删除 root_graph.py 中所有旧执行节点：
   - run_general_chat()
   - run_composite()
   - 各 make_run_registered_domain() 生成的 run_research/patent/ppt/zero_report
   - select_path() 条件分支函数
   - maybe_clarify() 节点（clarification 由 Coordinator 内部处理）
2. 删除对 agent.platform.task_planner / agent.platform.step_runner 的 import
3. root_graph.py 简化为：normalize_input → coordinator → persist_summary

验证：
- 所有执行模式（direct/single_skill/dag）端到端测试通过
- 无 import 报错
```

#### C4：合并 DomainRegistry → skill registry

```
操作：
1. 将 auto_discover() 逻辑迁移到 coordinator/skills.py 的 discover_skills()
2. 在所有 import domain_registry 的位置改为 import skill registry
3. 保留 domain_registry.py 文件但标记 @deprecated，内部委托给 skill registry
4. 下一个发版周期删除 domain_registry.py

验证：
- grep "domain_registry" 仅在 deprecated 包装和测试中出现
- 所有路径回归通过
```

#### C6：删除 general_chat.py

```
操作：
1. 确认 Coordinator direct 模式的回答质量 ≥ 旧 general_chat
2. 删除 agent/capabilities/general_chat.py
3. 删除 dispatcher.py 中对 run_general_chat 的 import 和引用

验证：
- 20 个样本简单问答，direct 模式回答质量人工评审无退化
- 无 import 报错
```

#### C7：删除 dispatcher.py 路由逻辑

```
操作：
1. 删除 dispatcher.py 中的关键词路由函数：
   - route_task_request()
   - build_route_payload()
   - 所有 KEYWORD 常量
2. 保留 dispatcher.py 中被其他模块引用的工具函数（若有）
3. 若文件无残余引用，整体删除

验证：
- grep "from agent.runtime.dispatcher import" 无路由相关引用
- Coordinator understand_goal 路由准确率 ≥ 旧 dispatcher（20 个样本任务）
```

#### C3：合并 DomainSpec → SkillDescription

```
操作：
1. 将 DomainSpec 中 Coordinator 需要的字段（name, description, tools, 
   strategy_hints）合并到 SkillDescription
2. DomainSpec 中仅领域内部使用的字段（system_prompt, subagents, evaluator）
   下沉到各领域 orchestrated.py 的局部常量
3. SubagentConfig 保留在各领域内部使用，不再从 spec.py 统一 export
4. 删除 agent/workflows/spec.py

验证：
- grep "from agent.workflows.spec import" 返回空
- 所有领域内部工作流正常运行（DomainSpec 字段已内联到各领域）
- SkillDescription 包含足够元数据供 Coordinator LLM 选择技能
```

#### C2：合并 strategy_selector → Coordinator prompt

```
操作：
1. 将 strategy_selector.py 的规则逻辑提取为 Coordinator prompt 中的
   策略选择指引（few-shot examples）
2. single_skill 模式下 Coordinator 直接调用技能，不再需要独立策略选择
3. 删除 agent/workflows/strategy_selector.py
4. DomainOrchestrator 中对 strategy_selector 的调用改为
   接收 Coordinator 传入的 strategy 参数（若 C1 尚未执行）

验证：
- 10 个样本单领域任务，Coordinator 选择的策略与旧选择器一致率 ≥ 90%
- 跨领域任务的策略分配合理性人工评审通过
```

#### C1：删除 DomainOrchestrator

```
前提：C2, C3, C4 已完成

操作：
1. 各领域 orchestrated.py 的 run_*_domain_orchestrated() 改为：
   - 直接构建领域内部 LangGraph 图（不再调用 build_orchestrated_graph(spec)）
   - 或保留 build_orchestrated_graph 但内联到各领域模块
2. 删除 agent/workflows/orchestrator.py
3. 删除 agent/workflows/ 目录（若已无文件）

验证：
- 各领域独立运行测试通过
- Coordinator 调用各领域技能测试通过
- OrchestratorState 中被外部引用的字段已迁移到 CoordinatorState 或领域内部
```

---

## 9. 错误处理与失败策略

### 9.1 DAG 失败策略

```python
class DAGFailureStrategy(Enum):
    STOP_ALL = "stop"                   # 任何一个失败，全部停止
    STOP_DEPENDENTS = "stop_dependents"  # 只停止依赖者
    CONTINUE = "continue"             # 继续执行其他任务

@dataclass
class CoordinatorConfig:
    """Coordinator 配置"""
    # 失败策略
    failure_strategy: DAGFailureStrategy = DAGFailureStrategy.STOP_DEPENDENTS
    max_total_failures: int = 3          # DAG 内最大累计失败任务数

    # 超时策略
    task_timeout_seconds: int = 300      # 单任务超时
    dag_timeout_seconds: int = 3600      # DAG 总超时

    # 预算策略
    max_cost_usd: float = 10.0         # 最大成本
    cost_warning_threshold: float = 0.8  # 警告阈值

    # 执行策略
    max_parallel_tasks: int = 5          # 最大并行任务数
    max_dag_depth: int = 10             # 最大 DAG 深度
```

### 9.2 任务失败处理

```python
async def handle_task_failure(
    task: Task,
    error: Exception,
    state: CoordinatorState
) -> dict[str, Any]:
    """
    处理任务失败
    1. 记录错误信息
    2. 根据 failure_strategy 决定后续动作
    3. 标记依赖任务状态
    """
    task.error = str(error)
    task.status = "failed"
    state["failed_tasks"][task.id] = task

    # 累计失败数（非"连续"语义——DAG 并行场景无严格先后顺序）
    total_failures = len(state["failed_tasks"])
    if total_failures >= state["config"].max_total_failures:
        _safe_emit("error", {
            "type": "max_total_failures",
            "count": total_failures
        })
        return {"action": "stop_all"}

    # 根据策略处理依赖任务
    dependents = find_dependent_tasks(task.id, state["task_dag"])
    if state["config"].failure_strategy == DAGFailureStrategy.STOP_ALL:
        for dep in dependents:
            dep.status = "cancelled"
        return {"action": "stop_all"}

    elif state["config"].failure_strategy == DAGFailureStrategy.STOP_DEPENDENTS:
        for dep in dependents:
            if dep.status == "pending":
                dep.status = "cancelled"
                state["failed_tasks"][dep.id] = dep
        return {"action": "continue"}

    return {"action": "continue"}
```

### 9.3 超时处理

```python
async def execute_task_with_timeout(
    task: Task,
    skill: SkillDescription,
    context: SkillContext,
    timeout_seconds: int | None = None
) -> SkillResult:
    """带超时的任务执行"""
    timeout = timeout_seconds or skill.timeout_seconds

    try:
        result = await asyncio.wait_for(
            execute_skill(task, skill, context),
            timeout=timeout
        )
        return result
    except asyncio.TimeoutError:
        _safe_emit("task_timeout", {
            "task_id": task.id,
            "skill": skill.name,
            "timeout_seconds": timeout
        })
        return SkillResult(
            status="timeout",
            error=f"Task exceeded timeout of {timeout}s"
        )
```

---

## 10. 监控与可观测性

### 10.1 事件增强

所有事件携带 `trace_id` 贯穿整个 DAG 执行：

```python
# Coordinator 事件
_safe_emit("step", {
    "content": "理解目标...",
    "node": "understand_goal",
    "trace_id": state["trace_id"],  # 贯穿整个 DAG
    "timestamp": time.time()
})

_safe_emit("task_dag", {
    "tasks": [t.id for t in dag],
    "status": "generated",
    "trace_id": state["trace_id"]
})

_safe_emit("task_start", {
    "task_id": "t1",
    "skill": "do_research",
    "trace_id": state["trace_id"],
    "start_time": time.time()
})

_safe_emit("task_complete", {
    "task_id": "t1",
    "result": "...",
    "trace_id": state["trace_id"],
    "execution_time": result.execution_time_seconds,
    "cost_usd": result.cost_usd
})

_safe_emit("budget_warning", {
    "task_id": task_id,
    "current_cost": 8.5,
    "limit": 10.0,
    "trace_id": state["trace_id"]
})
```

### 10.2 指标采集

```python
@dataclass
class CoordinatorMetrics:
    """DAG 执行指标"""
    trace_id: str
    total_tasks: int
    completed_tasks: int
    failed_tasks: int
    total_execution_time: float
    total_cost_usd: float
    skill_usage: dict[str, int]  # 每个技能被调用的次数

    # 阶段指标
    goal_understanding_time: float = 0.0
    decomposition_time: float = 0.0
    execution_time: float = 0.0
    synthesis_time: float = 0.0

def collect_metrics(state: CoordinatorState) -> CoordinatorMetrics:
    """从执行状态收集指标"""
    return CoordinatorMetrics(
        trace_id=state.get("trace_id", ""),
        total_tasks=len(state.get("task_dag", [])),
        completed_tasks=len(state.get("completed_tasks", {})),
        failed_tasks=len(state.get("failed_tasks", {})),
        total_execution_time=calculate_total_time(state),
        total_cost_usd=calculate_total_cost(state),
        skill_usage=count_skill_usage(state)
    )
```

### 10.3 循环依赖检测

```python
def validate_dag(task_dag: list[Task]) -> list[str]:
    """
    验证 DAG 合法性：
    1. 依赖目标必须存在（无悬空引用）
    2. 无循环依赖
    3. 最大深度不超限
    返回错误列表，空表示验证通过
    """
    errors: list[str] = []
    task_map = {t.id: t for t in task_dag}

    # ── 1. 悬空引用检查 ──────────────────────────────────
    for task in task_dag:
        for dep_id in task.depends_on:
            if dep_id not in task_map:
                errors.append(
                    f"Task {task.id} depends on non-existent task {dep_id}"
                )

    if errors:
        # 存在悬空引用时后续 DFS 无意义
        return errors

    # ── 2. 循环依赖检查 ──────────────────────────────────
    # 邻接表方向：task → 它所依赖的 task（反向边）。
    # DFS 沿"依赖链"向上游走，若走回自身则存在环。
    adj: dict[str, set[str]] = {
        t.id: set(t.depends_on) for t in task_dag
    }

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in task_map}

    def dfs(node: str, path: list[str]) -> list[str]:
        color[node] = GRAY
        path.append(node)
        for dep in adj[node]:
            if color[dep] == GRAY:
                cycle_start = path.index(dep)
                return path[cycle_start:] + [dep]
            if color[dep] == WHITE:
                cycle = dfs(dep, path[:])
                if cycle:
                    return cycle
        color[node] = BLACK
        return []

    for task_id in task_map:
        if color[task_id] == WHITE:
            cycle = dfs(task_id, [])
            if cycle:
                errors.append(
                    f"Circular dependency detected: {' -> '.join(cycle)}"
                )

    # ── 3. 深度检查 ───────────────────────────────────────
    for task in task_dag:
        depth = calculate_task_depth(task, task_map)
        if depth > MAX_DAG_DEPTH:
            errors.append(
                f"Task {task.id} exceeds max depth: {depth} > {MAX_DAG_DEPTH}"
            )

    return errors
```

---

## 11. 实现计划

本次重构明确分为 **4 个阶段**。每个阶段目标不同，且 **只有第 4 阶段才会决定是否删除旧有实现、彻底收敛内部抽象**。

### Phase 1：Coordinator 作为统一执行入口

**阶段目标**：Coordinator 替代所有现有执行路径，成为唯一入口。三种执行模式（direct / single_skill / dag）全部可用。

1. 新增 `coordinator/skills.py`
   - `SkillDescription` 定义
   - SkillAdapter 注册表
   - 现有 domain runner 的 adapter 封装（research / patent / ppt / zero_report）
   - 基础工具注册（web_search 等）
2. 新增 `coordinator/state.py`
   - `ExecutionMode` 枚举
   - `CoordinatorState`（含 execution_mode、selected_skill、task_dag 等）
   - `Task`、`TaskVarEntry`
   - `CoordinatorConfig`
3. 新增 `coordinator/agent.py`
   - `understand_goal`：LLM 判断执行模式 + 目标理解
   - `direct_answer`：直接回答（替代旧 general_chat）
   - `execute_single_skill`：调用 1 个技能（替代旧单领域路径）
4. 新增 `coordinator/executor.py`
   - `decompose_tasks`、`assign_skills`
   - `execute_tasks`、`handle_task_result`、`check_dependencies`
   - `synthesize`
5. 新增 `coordinator/prompts.py`
   - Coordinator system prompt（含技能摘要、执行模式判断指引、DAG 规划示例）
6. 改造 `root_graph.py`
   - 简化为：`normalize_input → coordinator → persist_summary`
   - 删除 `select_path` 分叉逻辑和所有旧执行节点
   - 旧节点代码暂时保留为注释或移到 `_deprecated/` 目录，Phase 4 清理

**阶段退出标准**：
- direct 模式：简单问答可正确回答，质量 ≥ 旧 general_chat
- single_skill 模式：各单领域任务可正确执行，结果与旧路径一致
- dag 模式：跨领域 happy path 可以执行完成
- `artifact_refs/review/budget` 能从 Coordinator 结果透传到 `TaskService`
- single_skill 模式延迟与旧单领域直连路径持平（understand_goal 额外开销 ≤ 2s）

### Phase 2：补齐技能契约与兼容语义

**阶段目标**：把 Coordinator 与现有领域工作流桥接稳固，确保不破坏事件、中断、恢复与 checkpoint 语义。

1. 扩展 `SkillContext / SkillResult`
   - 对齐 `request_payload`
   - 对齐 `clarification_history`
   - 保留 `latest_checkpoint_id`
   - 保留 `artifact_refs/review/budget/strategy`
2. 逐个接入领域 skill adapter
   - `research`：保留 `ResearchMemory`、三段 checkpoint、checkpoint C 快速恢复
   - `patent`：保留现有 `DomainSpec + build_orchestrated_graph` 内核
   - `ppt`：保留 OfficeCLI 工作流与文件产出约束
   - `zero_report`：保留现有规划/评审/产物持久化链路
3. 集成事件与中断兼容层
   - 复用现有 `stream_nested_graph()` / `translate_stream_part()`
   - 复用 `waiting_for_user -> reply_to_task() -> resume`
4. 集成 checkpoint 共存策略
   - Domain 内部 checkpoint 保留
   - Coordinator 仅记录 DAG 级状态和 checkpoint 引用

**阶段退出标准**：
- 真实或半真实跨领域任务可中断、可恢复
- `question/task/node/checkpoint/file` 事件序列与现有前端兼容
- Research 的 checkpoint / resume 语义未退化

### Phase 3：受控迁移与效果验证

**阶段目标**：在不删除旧内部实现（workflows/orchestrator 等）的前提下，验证 Coordinator 三种执行模式的质量和稳定性，为 Phase 4 清理提供数据支撑。

1. 补齐测试
   - `test_coordinator_state.py`
   - `test_dag_validation.py`
   - `test_task_execution.py`
   - `test_failure_handling.py`
   - 端到端跨领域任务测试
2. 做兼容性与质量验证
   - 对比三种执行模式（direct / single_skill / dag）的结果质量
   - 统计成功率、延迟、成本、人工介入恢复成功率
   - 验证 artifact / review / budget / trace 全链路
3. 确认边界
   - understand_goal 的执行模式判断准确率
   - single_skill 模式与旧单领域路径的延迟对比
   - 哪些内部抽象仍必须保留（Phase 4 收敛决策输入）

**阶段退出标准**：
- 样本任务上的跨领域成功率稳定
- 事件协议、持久化协议、resume 协议无破坏性回归
- 形成明确的 Phase 4 收敛决策输入

### Phase 4：收敛决策与清理

**阶段目标**：基于前 3 个阶段的验证数据，按量化门槛决定是否删除旧实现并统一内部抽象。具体收敛对象、操作步骤和验证方法参见 §8。

#### 4.1 量化入口门槛

Phase 4 的每一批收敛操作必须满足以下所有指标后方可启动：

| # | 指标 | 门槛值 | 数据来源 |
|---|------|--------|---------|
| G1 | **跨领域端到端成功率** | ≥ 80%（10 个以上样本任务） | Phase 3 样本测试日志 |
| G2 | **单领域回归通过率** | 100%（现有 CI 测试套件） | CI pipeline |
| G3 | **事件序列兼容性** | Coordinator 路径产生的 SSE 事件序列与现有前端解析兼容，diff 中仅允许 additive 事件（`task_dag`, `coordinator_step`, `task_start`, `task_complete`） | Phase 3 事件 diff 脚本 |
| G4 | **interrupt/resume 回归** | Research checkpoint C 快速恢复、Patent/Zero Report 追问恢复均正常 | 专项手动测试 + 自动化用例 |
| G5 | **artifact/review/budget 透传** | 所有已完成样本任务的 `RootState` 输出包含完整的 `artifact_refs`, `review`, `budget`, `strategy_trace` | Phase 3 结果校验脚本 |
| G6 | **direct 模式回答质量** | direct 模式回答质量 ≥ 旧 general_chat（20 个样本问答人工评审） | Phase 3 回答质量对比日志（C6 需要） |

#### 4.2 执行流程

```
Phase 3 验证窗口结束
          │
          ▼
    收集指标 G1-G6
          │
          ├── 任一指标不达标 ──────────────────────────────┐
          │                                                │
          ▼                                                ▼
    批次 A（C5 + C4 + C6 + C7）                   暂停收敛
          │                                      记录未达标项
          ▼                                      设定下一轮验证时间
    回归验证 A
          │
          ├── 失败 → 回滚批次 A
          │
          ▼
    批次 B（C3 + C2）
          │
          ▼
    回归验证 B
          │
          ├── 失败 → 回滚批次 B，保留 A 的收敛成果
          │
          ▼
    批次 C（C1）
          │
          ▼
    全量回归验证
          │
          ├── 失败 → 回滚批次 C，保留 A+B 收敛成果
          │
          ▼
    收敛完成，更新架构文档
```

#### 4.3 残留组件边界（若未完全收敛）

若批次 C（删除 DomainOrchestrator）未通过验证，最终架构为：

- Coordinator 仍是唯一执行入口（Phase 1 已完成）
- 批次 A + B 的清理成果保留（旧路由、DomainRegistry、DomainSpec、strategy_selector 已删除）
- `agent/workflows/orchestrator.py` 保留，各领域 skill adapter 内部继续使用 `build_orchestrated_graph()`
- 这是可接受的长期状态：Coordinator 在外层编排，orchestrator 在内层提供领域工作流

#### 4.4 关键约束

- **Phase 4 是清理旧代码**，不是切换架构——架构切换在 Phase 1 已完成
- **Phase 1-3 不以”删除 `workflows/*` / `domain_registry.py`”为交付目标**
- **若验证数据不足或兼容性不达标，则不进入删除步骤**
- **每批收敛操作必须在独立 git branch 上执行，回滚成本 = 1 次 revert merge**

**测试策略**：

```python
# Mock 领域技能用于测试
class MockSkill:
    def __init__(self, result: SkillResult, delay: float = 0):
        self.result = result
        self.delay = delay

    async def execute(self, input_data: dict, context: SkillContext) -> SkillResult:
        await asyncio.sleep(self.delay)
        return self.result

# 测试用例
async def test_simple_dag_execution():
    “””测试简单 DAG 执行”””
    tasks = [
        Task(id=”t1”, title=”搜索”, assigned_skill=”web_search”, depends_on=[]),
        Task(id=”t2”, title=”研究”, assigned_skill=”do_research”, depends_on=[“t1”]),
    ]
    # 验证 DAG 无循环
    errors = validate_dag(tasks)
    assert errors == []

    # 执行并验证结果
    result = await execute_dag(tasks, skills_registry)
    assert result.final_result is not None

async def test_failure_strategy_stop_dependents():
    “””测试 STOP_DEPENDENTS 失败策略”””
    tasks = [
        Task(id=”t1”, title=”搜索”, assigned_skill=”web_search”, depends_on=[]),
        Task(id=”t2”, title=”研究”, assigned_skill=”do_research”, depends_on=[“t1”]),
        Task(id=”t3”, title=”专利”, assigned_skill=”do_patent”, depends_on=[“t2”]),
    ]
    config = CoordinatorConfig(failure_strategy=DAGFailureStrategy.STOP_DEPENDENTS)
    # t1 失败，t2 和 t3 应该被取消（t3 传递依赖 t1）

async def test_interrupt_resume_through_coordinator():
    “””测试 Coordinator 下领域技能的中断/恢复”””
    # 验证 ask_user() 在 Coordinator → SkillAdapter → domain runner 链路中正确工作
    ...

async def test_task_vars_propagation():
    “””测试 TaskVarEntry 在依赖链中的正确传递”””
    # t1 完成 → task_vars[“t1”] 写入 → t2 的 input_data 包含 upstream_context
    ...

async def test_event_sequence_compatibility():
    “””测试事件序列兼容性”””
    # 收集 Coordinator 路径的事件序列
    # 验证只包含 additive 新事件，不缺失标准事件
    ...
```

**总工期**：核心开发约 10-16 天（Phase 1-3），Phase 4 收敛 5-8 天（分 3 批），外加 Phase 3 的验证观察窗口

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM 生成的任务 DAG 质量不稳定 | 任务分配不合理 | 提供示例提示词，限制技能数量 |
| 跨领域依赖状态传递 | 上下文丢失 | `task_vars` 只传摘要；复杂状态继续留在领域 memory / checkpoint 中 |
| 领域技能执行时间不确定 | 超时难以控制 | 独立 timeout_seconds 配置 |
| SSE 事件协议不兼容 | 前端解析失败 | 复用现有 `stream_nested_graph()` / `translate_stream_part()` 协议 |
| **循环依赖** | DAG 死锁 | execute_tasks 前验证 DAG |
| **嵌套调用失控** | 无限递归 | nested_depth_limit 控制 |
| **成本超限** | 费用失控 | max_cost_usd + budget_warning |
| **结果序列化失败** | 跨领域数据传递失败 | `SkillResult` 强制保留 `artifact_refs/review/budget/strategy` |
| **一次性替换旧实现** | 单领域能力回退、恢复链路损坏 | 采用 4 阶段方案，第 4 阶段分 3 批收敛，每批独立 branch + 回归验证 |
| **并行技能的 interrupt 竞争** | 多个技能同时触发 ask_user() 导致状态错乱 | 同一时刻仅允许一个技能触发 interrupt（§6.4） |
| **task_vars 上下文过长** | 下游任务 prompt 超出 token 限制 | TaskVarEntry.summary 强制截断 2000 字符（§4.3） |
| **三层嵌套事件丢失** | root → coordinator → domain 嵌套时事件未透传 | stream_nested_graph 的 subgraphs=True 保证透传；Phase 2 专项验证 |

---

## 13. 成功标准

1. **功能完整**：跨领域任务可以正确执行
2. **事件透明**：所有关键事件通过 SSE 发送，trace_id 贯穿全程
3. **可中断**：人类可以介入任务执行
4. **可恢复**：任务失败后可以从 checkpoint 恢复
5. **向后兼容**：Phase 1-3 单领域任务不受影响
6. **可观测**：执行指标可采集、可追踪
7. **容错**：支持超时、重试、失败策略
8. **可收敛**：只有在 Phase 4 验证通过后才删除旧实现

---

## 14. 参考资料

- [open-multi-agent](https://github.com/JackChen-me/open-multi-agent) - TypeScript 多智能体编排框架
- [LangGraph](https://github.com/langchain-ai/langgraph) - 状态化语言代理
- chat-dada 现有架构文档
