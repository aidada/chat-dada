"""
Coordinator Prompt Builders.

This module provides prompt construction functions for:
- understand_goal: Determine execution mode and goal understanding
- direct_answer: Direct LLM response without skill invocation
- decompose_tasks: Generate task DAG for complex requests
- synthesis: Combine results from multiple tasks
"""
from __future__ import annotations

from typing import Any


def build_understand_goal_prompt(
    goal: str,
    skill_summary: str,
    capability_summary: str = "",
    source_files: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build prompt for understanding user goal and determining execution mode.

    Returns a list of messages (system + user) for LLM invocation.

    The LLM should return a JSON with:
    - execution_mode: "direct" | "single_skill" | "dag"
    - goal_understanding: str
    - reasoning: str
    - (if single_skill) selected_skill: str, skill_input: dict
    - (if dag) dag_strategy: str (optional, for domain strategy guidance)
    """
    system_prompt = """你是 Coordinator Agent 的目标理解模块。你的任务是分析用户请求，判断最适合的执行模式。

## 执行模式

1. **direct** - 简单问答、闲聊、不需要领域技能
   - 示例："你好"、"解释一下什么是 LangGraph"、"帮我翻译这段话"
   - 直接用 LLM 回答，不调用任何技能

2. **single_skill** - 明确属于单一领域
   - 示例："帮我研究量子计算的最新进展"、"写一个专利"
   - 跳过 DAG 规划，直接调用 1 个技能

3. **dag** - 跨领域、多步骤、需要编排
   - 示例："研究竞品技术方案并撰写专利"、"调研后生成 PPT"
   - LLM 生成任务 DAG → 分配技能 → 执行

## 领域执行策略（仅 dag 模式需要）

当任务涉及多步骤领域执行时，需为每个领域任务选择执行策略：

- **sequential**: 顺序执行，适合线性任务或单一子任务
- **parallel**: 并行执行，适合多个独立子任务同时进行
- **iterative**: 迭代优化，适合上次输出质量不足需要改进
- **planning**: 动态规划，适合复杂任务需要先分解再执行

### 策略选择规则（优先级从高到低）

1. **评审反馈触发迭代**: 上次评审未通过 → iterative（根据反馈改进）
2. **多子任务并行**: 多个独立子任务待执行 → parallel（提高效率）
3. **复杂目标分解**: 目标复杂且未分解 → planning（先规划再执行）
4. **简单任务**: 目标简短或单一子任务 → sequential（直接执行）

### 策略选择示例

示例 1: 单一专利撰写任务
```json
{
  "execution_mode": "single_skill",
  "reasoning": "明确的专利撰写请求，单领域任务",
  "goal_understanding": "用户需要撰写一份专利草案",
  "selected_skill": "do_patent",
  "skill_input": {"query": "根据以下技术描述撰写专利..."}
}
```
→ single_skill 模式无需策略参数，领域内部默认 sequential

示例 2: 竞品研究后撰写专利
```json
{
  "execution_mode": "dag",
  "reasoning": "涉及研究和专利两个领域，需先研究再撰写",
  "goal_understanding": "竞品技术分析并撰写专利布局",
  "dag_strategy": "sequential",
  "tasks": [
    {"id": "t1", "assigned_skill": "do_research", "strategy": "planning"},
    {"id": "t2", "assigned_skill": "do_patent", "depends_on": ["t1"], "strategy": "sequential"}
  ]
}
```
→ dag 模式：研究阶段用 planning 策略分解，专利阶段用 sequential 策略撰写

示例 3: 多个子任务并行研究
```json
{
  "execution_mode": "dag",
  "reasoning": "研究多个独立技术方向，可并行",
  "goal_understanding": "并行研究量子计算、AI芯片、云计算三个方向",
  "dag_strategy": "parallel",
  "tasks": [
    {"id": "t1", "assigned_skill": "do_research", "strategy": "planning"},
    {"id": "t2", "assigned_skill": "do_research", "strategy": "planning"},
    {"id": "t3", "assigned_skill": "do_research", "strategy": "planning"}
  ]
}
```
→ dag 模式：三个研究方向可并行，各用 planning 策略内部分解

示例 4: 简单问答
```json
{
  "execution_mode": "direct",
  "reasoning": "简单问候，无需领域技能",
  "goal_understanding": "用户打招呼"
}
```
→ direct 模式无需策略

## 输出格式

请返回 JSON 格式（使用 markdown 代码块）：

```json
{
  "execution_mode": "direct|single_skill|dag",
  "reasoning": "判断理由",
  "goal_understanding": "对用户目标的精炼理解",
  "selected_skill": "技能名称（仅 single_skill 模式）",
  "skill_input": {"query": "传入技能的参数（仅 single_skill 模式）"},
  "dag_strategy": "dag 执行策略（仅 dag 模式，可选）",
  "model_hints": {
    "role_name": {
      "model": "模型名（可选）",
      "provider": "provider 名（可选）"
    }
  }
}
```

`model_hints` 仅在默认模型明显不适合时输出，用于提示下游角色选择更合适的模型；如果没有明确偏好，请省略该字段。

## Office 任务补充规则

- 对 Office / PPT / Word / Excel 请求，优先让模型基于完整语义判断，不要把词面上的“写/重写”机械映射成 create。
- 如果用户提到现有文件名或路径，并要求重写、修改、更新、润色、扩充、检查、转换，通常应选择 `single_skill` + `do_office`。
- 对这类已有文件任务，在 `skill_input` 中尽量补充：
  - `operation_hint`: `edit` / `inspect` / `transform`
  - `file_hint`: 用户提到的原始文件名或路径
  - `source_files`: 若运行时已提供上传文件，直接透传
- 如果用户只提到 bare filename（例如 `deck.pptx`）但意图明显是修改已有文件，也不要因为缺少绝对路径就改判为 create；应保持 edit/inspect/transform 意图，让下游继续解析或澄清。
- 只有当用户明确要新建文档，或完全没有引用现有文件时，才选择 create 导向的 Office 处理。
- 对 Office create 任务，`skill_input` 应尽量给出 `file_hint`，文件名必须贴合 goal，用简洁的英文 kebab-case 表达主题；避免使用 `ai.pptx`、`deck.pptx`、`presentation.pptx` 这类过于泛化的名称。
- 如果用户已经明确给出了现有文件名或目标文件名，保留用户原意，不要擅自改名。

## 决策原则

- 如果用户请求是简单问答或闲聊 → direct
- 如果明确属于单一领域且有对应技能 → single_skill
- 如果涉及多个步骤或跨领域协作 → dag
- 优先选择 single_skill（比 dag 开销更小）"""

    capability_section = ""
    if capability_summary.strip():
        capability_section = f"\n\n## Runtime Capabilities\n\n{capability_summary.strip()}"

    source_files_section = ""
    if source_files:
        source_lines = "\n".join(f"- {str(item).strip()}" for item in source_files if str(item).strip())
        if source_lines:
            source_files_section = f"\n\n已提供的源文件：\n{source_lines}"

    user_prompt = f"""{skill_summary}{capability_section}{source_files_section}

---

用户请求：
{goal}

请分析并返回 JSON 格式的执行模式判断。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_direct_answer_prompt(
    goal: str,
    conversation_context: str,
    capability_summary: str = "",
) -> list[dict[str, str]]:
    """Build prompt for direct LLM response without skill invocation.

    This replaces the old general_chat capability.
    """
    system_prompt = """你是 ChatDada 的 AI 助手。请直接回答用户的问题，保持友好、专业、有帮助。

回答原则：
1. 简洁明了，避免冗长
2. 如果涉及技术问题，提供准确信息
3. 如果用户询问不确定的内容，诚实告知
4. 可以使用适当的格式化（列表、代码块等）提高可读性
5. 如果用户只是在确认你能不能做某类交付，不要假装已经开始执行，也不要声称已经生成文件
6. 如果用户想做 PPT、文档、报告等交付，但没有给出主题、受众、页数或关键要点，先明确告知还需要这些信息，再继续下一步
7. 如果上下文说明当前可用桌面本机工具，请优先使用这些工具处理本地文件或本机环境请求，而不是直接声称无法访问
8. 对有副作用的本机工具保持克制，仅在确有必要时调用；若权限被拒绝或工具离线，再向用户解释限制
9. 对只读本地文件任务，优先使用 list_dir、file_read、file_search、grep 等专用工具，不要用 shell 代替
10. 只有当专用工具无法满足需求时，才考虑 shell 这类高风险工具"""

    context_section = ""
    if conversation_context:
        context_section = f"\n\n上下文信息：\n{conversation_context}\n\n---\n"

    capability_section = ""
    if capability_summary.strip():
        capability_section = f"\n\n运行时能力：\n{capability_summary.strip()}\n\n---\n"

    user_prompt = f"""{context_section}{capability_section}用户问题：
{goal}

请直接回答。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_decompose_tasks_prompt(goal: str, skill_summary: str) -> list[dict[str, str]]:
    """Build prompt for decomposing a complex goal into a task DAG.

    Returns a list of messages for LLM invocation.
    The LLM should return JSON with a list of tasks, each having:
    - id: unique task ID
    - title: brief title
    - description: detailed description
    - depends_on: list of task IDs this task depends on
    - assigned_skill: skill name to execute this task
    - input_data: parameters for the skill
    """
    system_prompt = """你是 Coordinator Agent 的任务分解模块。你的任务是将复杂的用户请求分解为有序的任务 DAG（有向无环图）。

## 任务 DAG 结构

每个任务包含：
- id: 唯一标识（如 t1, t2, t3）
- title: 简短标题
- description: 详细描述
- depends_on: 依赖的前置任务 ID 列表（可为空）
- assigned_skill: 执行该任务的技能名称
- input_data: 传递给技能的参数

## 输出格式

返回 JSON 格式（使用 markdown 代码块）：

```json
{
  "tasks": [
    {
      "id": "t1",
      "title": "任务标题",
      "description": "任务描述",
      "depends_on": [],
      "assigned_skill": "技能名称",
      "input_data": {"query": "参数"}
    }
  ],
  "reasoning": "分解理由"
}
```

## 设计原则

1. 任务数量合理（通常 2-5 个）
2. 每个任务职责单一、可执行
3. 依赖关系形成 DAG（无循环）
4. 技能分配合理（匹配任务类型）
5. 依赖任务的结果可通过 "upstream_context" 传递给下游"""

    user_prompt = f"""{skill_summary}

---

用户请求：
{goal}

请分解为任务 DAG 并返回 JSON 格式。"""

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_synthesis_prompt(
    completed_tasks: dict[str, Any],
    final_tasks: list[Any],
) -> str:
    """Build prompt for synthesizing results from multiple completed tasks.

    Takes completed tasks dict and final tasks list (tasks with no dependents).
    Returns a single prompt string for LLM to generate final summary.
    """
    results_section = ""
    for task in final_tasks:
        title = task.title if hasattr(task, "title") else str(task.get("title", ""))
        result_text = ""
        if hasattr(task, "result"):
            if isinstance(task.result, dict):
                result_text = str(task.result.get("result", "") or "")
            else:
                result_text = str(task.result or "")
        else:
            result_text = str(task.get("result", "") or "")

        results_section += f"\n## {title}\n\n{result_text}\n\n"

    prompt = f"""请汇总以下研究结果，生成一份连贯的分析报告：

{results_section}

要求：
1. 保持各部分的核心发现
2. 消除矛盾和重复
3. 给出综合结论
4. 格式清晰，便于阅读"""

    return prompt


__all__ = [
    "build_understand_goal_prompt",
    "build_direct_answer_prompt",
    "build_decompose_tasks_prompt",
    "build_synthesis_prompt",
]
