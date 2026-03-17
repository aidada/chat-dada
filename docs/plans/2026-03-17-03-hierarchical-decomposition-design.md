# 机制 3：层级任务分解

> 优先级：P1 | 预计影响文件：agents/deep_research.py, orchestrator/planner.py, 新增 research_planner.py

## 1. 概述与目标

当前 deep_research agent 是单一的 planner-tools 循环，没有子任务概念。一个 agent 既要决定搜索什么、又要执行搜索、又要判断信息是否足够、又要写报告——所有职责混在同一个上下文窗口里。目标是引入层级任务分解，将"规划"和"执行"分离，让大任务自动拆分为可独立完成的子研究。

## 2. 行业参考

### Manus
- 每次迭代**只执行一个工具调用**，必须等结果回来再决定下一步
- 早期用 `todo.md` 跟踪任务，后改为专用 Planner Agent 返回结构化 Plan 对象
- 四步循环：分析 → 规划 → 执行 → 观察

### OpenAI Deep Research
- 三阶段管线：意图澄清模型 → 提示扩展模型 → 深度研究模型
- ReAct 范式（Plan-Act-Observe）循环
- 硬性停止机制：20-30 分钟 / 30-60 次搜索 / 150-200 次迭代

### Devin
- 规划器 LLM 将目标展开为逐步计划，并对每一步自我批评
- Planning Mode 子 agent 用只读工具决定应编辑哪些文件，此阶段不做任何编辑
- Devin 2.1：基于置信度的规划，不确定时等待用户批准

## 3. 当前代码诊断

| 位置 | 问题 |
|------|------|
| `research_planner()` (L218-230) | 规划和执行耦合在同一个节点中，LLM 同时承担"决定下一步"和"生成搜索查询"两个职责 |
| `build_research_graph()` (L262-293) | 只有 planner → tools → planner 的平坦循环，无层级结构 |
| `_build_research_messages()` (L344-365) | prompt 中混杂了任务指导、当前笔记、下一步决策——信息密度过高 |
| `research_should_continue()` (L253-259) | 只看 step_count 和是否有 tool_calls，无子任务完成度判断 |

### 核心矛盾
用户问"碳纤维复合材料在深海压力容器中的疲劳寿命预测方法"，agent 需要：
1. 搜索碳纤维复合材料的基本力学性能
2. 搜索深海压力容器的设计标准
3. 搜索疲劳寿命预测的具体方法（S-N 曲线、损伤力学、有限元...）
4. 搜索现有实验数据和案例
5. 交叉验证不同方法的适用范围

但当前 agent 没有这个全局规划能力——它一步步摸索，经常重复搜索或遗漏关键维度。

## 4. 架构设计

### 4.1 两阶段架构

```
用户查询
    ↓
┌─────────────────────────────┐
│  Phase 1: 研究规划           │
│  - 分析用户真正想知道什么      │
│  - 拆分为 3-7 个子研究主题    │
│  - 确定优先级和依赖关系       │
│  - 输出结构化研究计划         │
└─────────────────────────────┘
    ↓
┌─────────────────────────────┐
│  Phase 2: 逐子任务执行        │
│  对每个子主题：               │
│  - 执行 2-4 轮搜索           │
│  - 压缩发现写入外部记忆       │
│  - 判断该子主题是否完成       │
│                              │
│  全部完成后 → 综合报告        │
└─────────────────────────────┘
```

### 4.2 研究计划数据结构

```python
# research_planner.py（新增文件）

from dataclasses import dataclass, field


@dataclass
class ResearchSubtask:
    """单个子研究任务"""
    id: str                        # 如 "subtask_1"
    topic: str                     # 子主题描述
    search_angles: list[str]       # 建议的搜索角度（中文 + 英文关键词）
    depends_on: list[str] = field(default_factory=list)  # 依赖的其他子任务 id
    priority: int = 1              # 1=高, 2=中, 3=低
    max_rounds: int = 3            # 该子任务最多搜索几轮
    status: str = "pending"        # pending / in_progress / completed / skipped
    completion_criteria: str = ""  # 什么条件算完成


@dataclass
class ResearchPlan:
    """结构化研究计划"""
    original_query: str
    clarified_goal: str            # 澄清后的研究目标
    subtasks: list[ResearchSubtask] = field(default_factory=list)
    global_constraints: list[str] = field(default_factory=list)  # 如"必须包含实验数据"


PLAN_GENERATION_SYSTEM = """你是研究规划专家。根据用户的研究问题，生成一个结构化的研究计划。

要求：
1. 将问题拆分为 3-7 个独立的子研究主题
2. 每个子主题要有明确的搜索角度（同时包含中文和英文关键词）
3. 标注子任务之间的依赖关系（哪些必须先完成）
4. 标注优先级：核心问题 > 支撑证据 > 边界条件
5. 每个子任务的完成标准要具体可判断

输出 JSON 格式：
{
  "clarified_goal": "...",
  "subtasks": [
    {
      "id": "subtask_1",
      "topic": "...",
      "search_angles": ["中文关键词1", "english keyword 1", ...],
      "depends_on": [],
      "priority": 1,
      "max_rounds": 3,
      "completion_criteria": "..."
    }
  ],
  "global_constraints": ["..."]
}
"""


async def generate_research_plan(query: str, memory_context: str = "") -> ResearchPlan:
    """用 LLM 生成结构化研究计划"""
    from models import get_llm
    from langchain_core.messages import SystemMessage, HumanMessage
    import json

    llm = get_llm("deep_research")
    prompt = f"研究问题：{query}"
    if memory_context:
        prompt = f"用户背景：{memory_context}\n\n{prompt}"

    response = await llm.ainvoke([
        SystemMessage(content=PLAN_GENERATION_SYSTEM),
        HumanMessage(content=prompt),
    ])

    text = extract_text_content(response)
    # 从回复中提取 JSON
    plan_data = _extract_json(text)

    subtasks = [
        ResearchSubtask(**st) for st in plan_data.get("subtasks", [])
    ]
    return ResearchPlan(
        original_query=query,
        clarified_goal=plan_data.get("clarified_goal", query),
        subtasks=subtasks,
        global_constraints=plan_data.get("global_constraints", []),
    )


def get_next_subtask(plan: ResearchPlan) -> ResearchSubtask | None:
    """获取下一个可执行的子任务（依赖已完成、优先级最高）"""
    completed_ids = {st.id for st in plan.subtasks if st.status == "completed"}
    for st in sorted(plan.subtasks, key=lambda x: x.priority):
        if st.status != "pending":
            continue
        if all(dep in completed_ids for dep in st.depends_on):
            return st
    return None


def is_plan_complete(plan: ResearchPlan) -> bool:
    """判断研究计划是否全部完成"""
    return all(st.status in ("completed", "skipped") for st in plan.subtasks)
```

### 4.3 改造后的 research graph

```python
def build_research_graph():
    """改造后的研究图：plan → subtask loop → synthesize"""

    async def plan_node(state: ResearchState) -> dict:
        """Phase 1：生成研究计划"""
        plan = await generate_research_plan(
            state["query"],
            state.get("memory_context", ""),
        )
        return {
            "research_plan": asdict(plan),
            "messages": [AIMessage(content=f"研究计划已生成，包含 {len(plan.subtasks)} 个子任务")],
        }

    async def subtask_router(state: ResearchState) -> dict:
        """选择下一个子任务"""
        plan = ResearchPlan(**state["research_plan"])
        next_st = get_next_subtask(plan)
        if next_st is None:
            return {"current_subtask": None}
        next_st.status = "in_progress"
        return {
            "current_subtask": asdict(next_st),
            "messages": [AIMessage(content=f"开始子任务：{next_st.topic}")],
        }

    async def subtask_research(state: ResearchState) -> dict:
        """对当前子任务执行搜索"""
        subtask = state["current_subtask"]
        # 复用现有的 planner 逻辑，但 prompt 聚焦在当前子任务
        ...

    async def subtask_judge(state: ResearchState) -> dict:
        """判断当前子任务是否完成"""
        ...

    async def synthesize(state: ResearchState) -> dict:
        """Phase 2 完成后：综合所有子任务的发现，生成最终报告"""
        ...

    g = StateGraph(ResearchState)
    g.add_node("plan", plan_node)
    g.add_node("subtask_router", subtask_router)
    g.add_node("subtask_research", subtask_research)
    g.add_node("subtask_judge", subtask_judge)
    g.add_node("synthesize", synthesize)

    g.set_entry_point("plan")
    g.add_edge("plan", "subtask_router")
    g.add_conditional_edges("subtask_router", _has_next_subtask,
                            {"yes": "subtask_research", "no": "synthesize"})
    g.add_edge("subtask_research", "subtask_judge")
    g.add_conditional_edges("subtask_judge", _subtask_done,
                            {"done": "subtask_router", "continue": "subtask_research"})
    g.add_edge("synthesize", END)
    return g.compile()
```

### 4.4 子任务级 prompt 聚焦

关键变化：每个子任务的 prompt 只包含：
1. 全局研究目标（1 句话）
2. 当前子任务描述和搜索角度
3. 该子任务已有的发现
4. 全局摘要（来自外部记忆）

**不包含**其他子任务的详细发现——这些在外部记忆中，综合阶段再读取。

## 5. 实现步骤

### Step 1：新增 `research_planner.py`
- 实现 `ResearchSubtask`、`ResearchPlan` 数据结构
- 实现 `generate_research_plan()` 函数
- 实现 `get_next_subtask()`、`is_plan_complete()` 辅助函数

### Step 2：扩展 `ResearchState`
```python
class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    research_context: dict          # 来自机制 1
    research_plan: dict             # 新增：结构化研究计划
    current_subtask: dict | None    # 新增：当前执行的子任务
    report_profile: str
```

### Step 3：改造 `build_research_graph()`
- 添加 plan_node 作为入口
- 添加 subtask_router / subtask_research / subtask_judge 循环
- 添加 synthesize 终结节点
- 保留原始 research_should_continue 作为子任务内部的步数限制

### Step 4：实现 `synthesize` 节点
- 从外部记忆加载所有子任务的发现
- 用 LLM 综合为最终报告
- 复用现有的 `_rewrite_final_report()` 逻辑

### Step 5：保持向后兼容
- 简单查询（不需要拆分的）：plan_node 生成只有 1 个子任务的计划
- 保留原始的 step_count 限制作为安全阀

## 6. 测试方案

```python
# tests/test_research_planner.py

async def test_generate_plan_complex_query():
    """测试复杂查询生成 3-7 个子任务"""

async def test_generate_plan_simple_query():
    """测试简单查询生成 1 个子任务"""

def test_get_next_subtask_respects_dependencies():
    """测试依赖关系：A 未完成时 B 不可执行"""

def test_get_next_subtask_respects_priority():
    """测试优先级排序"""

def test_is_plan_complete():
    """测试所有子任务完成时判定为完成"""

def test_subtask_status_transitions():
    """测试状态转换：pending → in_progress → completed"""
```

## 7. 验收标准

- [ ] 复杂查询自动拆分为 3-7 个子研究主题
- [ ] 简单查询不过度拆分（1 个子任务直通）
- [ ] 子任务按依赖关系和优先级顺序执行
- [ ] 每个子任务有独立的完成判断
- [ ] 最终综合阶段能访问所有子任务的发现
- [ ] 总步数不超过 sum(subtask.max_rounds) + 规划步骤
