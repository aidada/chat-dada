# 机制 4：Multi-Agent 架构

> 优先级：P1 | 预计影响文件：agents/deep_research.py, 新增 agents/research_worker.py, orchestrator/scheduler.py

## 1. 概述与目标

机制 3（层级任务分解）将大任务拆成子任务，但仍在单一上下文窗口中顺序执行。本机制的目标是让子任务在**独立的上下文窗口**中并行执行，实现真正的 multi-agent 协作。核心价值：上下文隔离 + 并行加速 + 失败隔离。

## 2. 行业参考

### Manus Wide Research
- 顺序处理超过 8-9 个项目时触及"幻觉阈值"
- 部署 n 个并行子 agent 同时处理 n 个项目
- 每个子 agent：独立的 Manus 实例 + 全新空白上下文窗口 + 完整 VM + 工具集
- 子 agent 之间**不互相通信**以防上下文污染
- 主控制器合并结果，本身不承担研究负担

### Claude Code Sub-Agent
- 严格单层嵌套——子 agent 不能再生成子 agent
- 中间工具调用和结果留在子 agent 内部，只有最终消息返回父 agent
- "真正的价值是隔离防止了长会话中累积的上下文腐化"
- 细粒度工具权限：只读 agent 仅获得 Read/Grep/Glob

### Devin 2.0
- 用户可并行启动多个 Devin 实例
- 一个 Devin 可将子任务分派给其他实例并发执行
- 多个沙箱并行运行，不互相干扰

## 3. 当前代码诊断

| 位置 | 问题 |
|------|------|
| `agents/deep_research.py` | 单 agent 全流程，无子 agent 概念 |
| `orchestrator/scheduler.py` | 有 wave-based 并发执行能力，但仅用于 orchestrator 层 |
| `registry.py` | 19 个能力已注册，但 deep_research 内部不使用 registry 的动态工具分配 |

### 核心机会
`orchestrator/scheduler.py` 已经实现了依赖图执行和 wave-based 并发——这正好可以复用来调度并行的研究子 agent。

### 与机制 3 的关系
机制 3 负责"拆分子任务"，机制 4 负责"并行执行子任务"。具体来说：
- 无依赖关系的子任务 → 并行执行（各自独立的上下文窗口）
- 有依赖关系的子任务 → 按依赖顺序执行
- 所有子任务完成后 → 主 agent 综合

## 4. 架构设计

### 4.1 三层架构

```
┌─────────────────────────────────────────────┐
│           Coordinator（协调器）               │
│  - 持有研究计划                               │
│  - 调度子 agent                              │
│  - 合并结果                                   │
│  - 自己不做搜索                               │
│  上下文：只有计划 + 各子 agent 返回的摘要       │
└─────────────────────────────────────────────┘
        ↓ 分派           ↑ 返回摘要
┌───────────┐  ┌───────────┐  ┌───────────┐
│ Worker A  │  │ Worker B  │  │ Worker C  │
│ 子主题 1  │  │ 子主题 2  │  │ 子主题 3  │
│ 独立上下文 │  │ 独立上下文 │  │ 独立上下文 │
│ 独立工具集 │  │ 独立工具集 │  │ 独立工具集 │
└───────────┘  └───────────┘  └───────────┘
        ↓               ↓              ↓
┌─────────────────────────────────────────────┐
│          外部记忆（data/research/{task_id}/） │
│  每个 worker 写入自己的 findings 文件          │
│  coordinator 读取所有 findings 做综合          │
└─────────────────────────────────────────────┘
```

### 4.2 Research Worker

```python
# agents/research_worker.py（新增文件）

"""
Research Worker — 执行单个子研究任务的独立 agent。
每个 worker 有自己的 LangGraph 状态和上下文窗口。
"""

from dataclasses import dataclass
from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, AIMessage
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from models import get_llm
from content_utils import extract_text_content


class WorkerState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    subtask_topic: str
    search_angles: list[str]
    step_count: int
    max_steps: int
    findings: str
    completion_criteria: str


WORKER_SYSTEM = """你是一个聚焦的研究助手。你的任务是围绕一个具体的子主题进行搜索和分析。

要求：
1. 只关注你被分配的子主题，不要扩展到其他领域
2. 每轮调用 1-2 个工具，优先补齐最关键的信息缺口
3. 中英文关键词都要搜索
4. 搜索完成后，输出该子主题的结构化研究笔记

研究笔记格式：
## 子主题：{topic}
### 关键发现
- ...（每条附来源 URL）
### 证据强度评估
- strong/moderate/weak
### 信息缺口
- ...（明确标注还缺什么）
"""


def build_worker_graph(tools: list):
    """构建单个 worker 的执行图"""

    async def worker_planner(state: WorkerState) -> dict:
        llm = get_llm("deep_research").bind_tools(tools)
        messages = [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(content=(
                f"子研究主题：{state['subtask_topic']}\n"
                f"搜索角度：{', '.join(state['search_angles'])}\n"
                f"完成标准：{state['completion_criteria']}\n\n"
                f"当前已有笔记：\n{state.get('findings', '(暂无)')}\n\n"
                "请决定下一步搜索或输出最终研究笔记。"
            )),
        ]
        response = await llm.ainvoke(messages)
        return {"messages": [response], "step_count": state["step_count"] + 1}

    async def worker_tools(state: WorkerState) -> dict:
        return await ToolNode(tools).ainvoke(state)

    def worker_finish(state: WorkerState) -> dict:
        for msg in reversed(state["messages"]):
            if isinstance(msg, AIMessage):
                text = extract_text_content(msg)
                if text:
                    return {"findings": text}
        return {"findings": state.get("findings", "")}

    def should_continue(state: WorkerState) -> str:
        if state["step_count"] >= state.get("max_steps", 4):
            return "finish"
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools"
        return "finish"

    g = StateGraph(WorkerState)
    g.add_node("planner", worker_planner)
    g.add_node("tools", worker_tools)
    g.add_node("finish", worker_finish)
    g.set_entry_point("planner")
    g.add_conditional_edges("planner", should_continue,
                            {"tools": "tools", "finish": "finish"})
    g.add_edge("tools", "planner")
    g.add_edge("finish", END)
    return g.compile()


async def run_worker(subtask: dict, tools: list, memory=None) -> dict:
    """执行单个 worker，返回研究笔记"""
    graph = build_worker_graph(tools)
    state = {
        "messages": [HumanMessage(content=f"开始研究：{subtask['topic']}")],
        "subtask_topic": subtask["topic"],
        "search_angles": subtask.get("search_angles", []),
        "step_count": 0,
        "max_steps": subtask.get("max_rounds", 3),
        "findings": "",
        "completion_criteria": subtask.get("completion_criteria", ""),
    }
    result = await graph.ainvoke(state)
    findings = result.get("findings", "")

    # 写入外部记忆
    if memory:
        memory.save_finding(
            step=subtask.get("priority", 0),
            tool_name=f"worker_{subtask['id']}",
            query=subtask["topic"],
            content=findings,
        )

    return {"subtask_id": subtask["id"], "findings": findings}
```

### 4.3 Coordinator（协调器改造）

```python
# 在 agents/deep_research.py 中改造

import asyncio
from agents.research_worker import run_worker


async def coordinate_research(plan: ResearchPlan, tools: list, memory) -> list[dict]:
    """协调多个 worker 并行执行研究计划"""
    results = []

    # 按依赖关系分波执行（复用 scheduler 的思想）
    while not is_plan_complete(plan):
        # 收集当前可执行的子任务（依赖已满足）
        executable = []
        completed_ids = {st.id for st in plan.subtasks if st.status == "completed"}
        for st in plan.subtasks:
            if st.status != "pending":
                continue
            if all(dep in completed_ids for dep in st.depends_on):
                executable.append(st)

        if not executable:
            break  # 防止死循环

        # 并行执行这一波
        tasks = [
            run_worker(asdict(st), tools, memory)
            for st in executable
        ]
        wave_results = await asyncio.gather(*tasks, return_exceptions=True)

        # 处理结果
        for st, result in zip(executable, wave_results):
            if isinstance(result, Exception):
                st.status = "skipped"
                log.error(f"Worker {st.id} failed: {result}")
            else:
                st.status = "completed"
                results.append(result)

    return results
```

### 4.4 并行度控制

```python
# 配置项

MAX_CONCURRENT_WORKERS = 3  # 最多同时运行 3 个 worker
# 原因：
# 1. LLM API 有并发限制
# 2. 太多并行 worker 会导致搜索 API 限流
# 3. 3 个并行 worker 已经能覆盖大多数场景

async def coordinate_research_with_limit(plan, tools, memory):
    """带并发限制的协调"""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_WORKERS)

    async def limited_worker(st):
        async with semaphore:
            return await run_worker(asdict(st), tools, memory)

    # ... 其余逻辑同上
```

### 4.5 Worker 之间的隔离保证

| 隔离维度 | 实现方式 |
|---------|---------|
| **上下文隔离** | 每个 worker 有独立的 `WorkerState`，独立的消息历史 |
| **工具隔离** | 所有 worker 共享相同工具集（web_search 等），但各自独立调用 |
| **结果隔离** | worker 之间不互相通信，只通过外部记忆间接共享 |
| **失败隔离** | 一个 worker 失败不影响其他 worker（`return_exceptions=True`） |
| **单层嵌套** | worker 不能再生成 worker |

## 5. 实现步骤

### Step 1：新增 `agents/research_worker.py`
- 实现 `WorkerState`、`build_worker_graph()`、`run_worker()`
- 实现 `WORKER_SYSTEM` prompt

### Step 2：实现 `coordinate_research()`
- 在 `agents/deep_research.py` 中添加协调器函数
- 实现 wave-based 并行执行
- 实现并发限制（semaphore）

### Step 3：改造 `build_research_graph()` 的 synthesize 节点
- plan 阶段生成研究计划
- 调用 `coordinate_research()` 并行执行
- synthesize 阶段从外部记忆加载所有 worker 结果，综合报告

### Step 4：改造 `run()` 入口函数
- 判断是否需要 multi-agent（子任务 >= 2 个且有可并行的）
- 简单查询走单 agent 快速路径
- 复杂查询走 multi-agent 路径

### Step 5：注册 worker 到 registry
```python
# registry.py 中注册
register("research_worker", {
    "fn_path": "agents.research_worker:run_worker",
    "type": "agent",
    "description": "独立的研究子任务执行器",
})
```

## 6. 测试方案

```python
# tests/test_research_worker.py

async def test_worker_runs_to_completion():
    """测试 worker 能正常完成搜索并返回 findings"""

async def test_worker_respects_max_steps():
    """测试 worker 不超过 max_steps"""

async def test_worker_writes_to_memory():
    """测试 worker 结果写入外部记忆"""

async def test_coordinate_parallel_execution():
    """测试多个 worker 并行执行"""

async def test_coordinate_respects_dependencies():
    """测试有依赖关系的子任务按顺序执行"""

async def test_coordinate_handles_worker_failure():
    """测试单个 worker 失败不阻塞其他 worker"""

async def test_concurrent_limit():
    """测试并发限制（最多 3 个同时运行）"""
```

## 7. 验收标准

- [ ] 无依赖的子任务真正并行执行（可从日志时间戳验证）
- [ ] 有依赖的子任务按顺序执行
- [ ] 每个 worker 有独立的上下文窗口（消息历史不互相污染）
- [ ] 单个 worker 失败时其他 worker 正常完成
- [ ] 并发数不超过 MAX_CONCURRENT_WORKERS
- [ ] Coordinator 的上下文中不包含 worker 的详细搜索过程
- [ ] 最终报告能综合所有 worker 的发现
