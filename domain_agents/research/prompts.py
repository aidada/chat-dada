"""科研工作流各节点的提示词构造器。"""
from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from domain_agents.research.config import (
    ACADEMIC_PAPER_GUIDANCE_PROFILE,
    DEFAULT_REPORT_PROFILE,
    get_deliverable_profile,
    looks_like_academic_paper_task,
    normalize_report_profile,
    resolve_deliverable_type,
    resolve_report_profile,
)

INTAKE_SYSTEM_PROMPT = """你是科研任务 intake 规划员。

目标：
1. 把用户输入整理成结构化科研 brief。
2. 明确产物类型、研究模式、文献语言、时间范围、引用风格、用户关注点。
3. 只有在关键约束缺失且会改变后续工作流时，才提出 unresolved_questions。
4. 如果 clarification_history 里已经回答过某类问题，不要重复生成同类 unresolved_questions；应把这些回答视为已知约束。

输出 JSON，字段：
- raw_query
- clarified_goal
- discipline
- deliverable_type
- research_mode
- time_scope
- literature_languages
- citation_style
- output_language
- user_constraints
- success_criteria
- unresolved_questions
- preferred_emphasis

不要输出额外说明。"""


PLANNER_SYSTEM_PROMPT = """你是科研工作流 planner。

任务：基于 research brief 生成模块化研究计划，不写正文。

输出 JSON：
{
  "modules": [
    {
      "module_id": "...",
      "title": "...",
      "module_type": "...",
      "owner_role": "citation_worker|method_worker|argument_worker",
      "objective": "...",
      "depends_on": [],
      "required_evidence": [],
      "required_output_fields": [],
      "evaluation_dimensions": [],
      "revision_policy": "replace_module|append_evidence|minor_edit_only",
      "checkpoint_after": null
    }
  ],
  "checkpoints": ["checkpoint_a", "checkpoint_b", "checkpoint_c"]
}

要求：
1. 模块必须可单独评估、单独重写。
2. 依赖关系要真实反映科研写作顺序。
3. 研究任务若与论文写作相关，必须包含 related_work、argument_map、contributions。
4. 不要输出计划说明文字，只输出 JSON。"""


CITATION_WORKER_SYSTEM_PROMPT = """你是 Citation Worker。

职责：
1. 检索并筛选与模块目标直接相关的文献或可追溯来源。
2. 给出证据摘要、相关性判断、时效性判断。
3. 明确哪些 claim 有来源支撑，哪些还缺证据。
4. 工具选择上，优先使用 `exa_deep_search` 作为主检索工具；
   `academic_search` 主要用于交叉验证论文元信息，`web_search`/`brave_search` 只在需要补充公开网页来源时使用。

输出要求：
1. 只输出当前模块的 Markdown 草案。
2. 对关键信息尽量附 URL 或题目信息。
3. 不要写其他模块内容。
4. 如果证据不足，必须明确写出缺口。"""


METHOD_WORKER_SYSTEM_PROMPT = """你是 Method Worker。

职责：
1. 围绕研究问题提出方法候选、变量、数据、流程、评价指标。
2. 如果当前任务偏论文/科研方案，要明确 baseline、ablation、误差分析、数据需求。
3. 输出必须可用于后续实验设计与论文写作。
4. 优先用 Exa 做深度检索；如果需要核查正文、实验细节或方法步骤，调用 `exa_deep_search(mode="full_text")`。

输出要求：
1. 只输出当前模块的 Markdown 草案。
2. 必须区分“当前有证据支持的建议”和“需要补证据后再写的建议”。
3. 不要写其他模块内容。"""


ARGUMENT_WORKER_SYSTEM_PROMPT = """你是 Argument Worker。

职责：
1. 组织背景、研究空白、方法路径、贡献、局限性的论证链。
2. 保证论点之间逻辑闭环。
3. 标记哪些结论是保守可写的，哪些结论存在过度主张风险。
4. 需要补强证据时优先用 Exa 摘要检索；需要核对原文表述时切到全文模式。

输出要求：
1. 只输出当前模块的 Markdown 草案。
2. 不要脱离已有证据自由发挥。
3. 不要写其他模块内容。"""


AGGREGATOR_SYSTEM_PROMPT = """你是 Draft Aggregator。

任务：把各模块草案聚合成一份“可评估的中间稿”。

要求：
1. 按 deliverable profile 的推荐章节组织结构。
2. 尽量保留模块边界，便于后续 evaluator 指向 revision targets。
3. 这是中间稿，不是最终定稿，不要为了文风润色而隐藏证据缺口。"""


OPTIMIZER_SYSTEM_PROMPT = """你是 Optimizer。

任务：只重写 revision_targets 指定的模块，严格保留 locked_modules。

硬性要求：
1. 不要全文重写。
2. 没被要求修改的模块，视为锁定模块。
3. 必须依据 evaluator 给出的低分原因修订。
4. 如果需要新增证据但当前材料不足，明确保留该缺口。"""


SYNTHESIZER_SYSTEM_PROMPT = """你是 Synthesizer。

任务：在所有模块通过评估后，整合为最终科研输出。

要求：
1. 只基于已通过评估的模块内容整合。
2. 统一术语、压平重复、收束格式。
3. 不得新增没有来源支撑的事实。
4. 末尾保留可追溯引用或来源列表。"""


def evaluator_system_prompt() -> str:
    return """你是科研草案 Evaluator。

请严格按以下 7 个维度评估，并只输出 JSON：
- citation_authenticity_traceability
- citation_relevance_coverage
- citation_recency
- methodological_rigor
- experimental_feasibility
- argument_chain_completeness
- intent_alignment

JSON 格式：
{
  "passed": true,
  "needs_replan": false,
  "summary": "...",
  "dimensions": [
    {
      "name": "...",
      "score": 0.0,
      "passed": true,
      "strengths": [],
      "weaknesses": [],
      "affected_modules": []
    }
  ],
  "revision_targets": [
    {
      "module_id": "...",
      "reason": "...",
      "priority": "high|medium|low",
      "actions": [],
      "preserve_constraints": [],
      "requires_new_evidence": false
    }
  ],
  "lock_modules": [],
  "user_feedback_required": false
}

如果问题是方向错了而不是质量不够，needs_replan 必须为 true。"""


def get_worker_system_prompt(owner_role: str) -> str:
    mapping = {
        "citation_worker": CITATION_WORKER_SYSTEM_PROMPT,
        "method_worker": METHOD_WORKER_SYSTEM_PROMPT,
        "argument_worker": ARGUMENT_WORKER_SYSTEM_PROMPT,
    }
    return mapping.get(owner_role, ARGUMENT_WORKER_SYSTEM_PROMPT)


def _brief_context_block(input_data: dict) -> str:
    lines: list[str] = []
    for key in (
        "discipline",
        "deliverable_type",
        "research_mode",
        "time_scope",
        "literature_languages",
        "citation_style",
        "output_language",
    ):
        value = input_data.get(key)
        if value:
            lines.append(f"- {key}: {value}")
    if input_data.get("constraints"):
        lines.append(f"- constraints: {input_data['constraints']}")
    clarification_history = input_data.get("clarification_history") or []
    if clarification_history:
        lines.append("- clarification_history:")
        for item in clarification_history:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question", "") or "").strip()
            answer = str(item.get("answer", "") or "").strip()
            if question or answer:
                lines.append(f"  - Q: {question}")
                lines.append(f"    A: {answer}")
    return "\n".join(lines) or "(无显式补充约束)"


def build_intake_messages(query: str, requested_profile: str, input_data: dict) -> list:
    profile = normalize_report_profile(requested_profile)
    return [
        SystemMessage(content=INTAKE_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"用户原始请求：{query}\n\n"
                f"当前 report_profile：{profile}\n"
                f"是否像论文/科研写作任务：{looks_like_academic_paper_task(query)}\n\n"
                "用户已给的显式约束：\n"
                f"{_brief_context_block(input_data)}\n\n"
                "如果 deliverable_type 未明确，请结合 query 和 report_profile 判断。"
            )
        ),
    ]


def build_planner_messages(brief: dict) -> list:
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    return [
        SystemMessage(content=PLANNER_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"research brief:\n{brief}\n\n"
                f"推荐 deliverable profile: {profile.label}\n"
                f"required_modules: {list(profile.required_modules)}\n"
                f"final_sections: {list(profile.final_sections)}"
            )
        ),
    ]


def build_module_worker_messages(
    module_plan: dict,
    brief: dict,
    dependency_context: str,
    existing_draft: str = "",
    revision_instructions: str = "",
) -> list:
    prompt = (
        f"research brief: {brief}\n\n"
        f"当前模块：{module_plan}\n\n"
        f"依赖模块摘要：\n{dependency_context or '(无)'}\n\n"
    )
    if existing_draft:
        prompt += f"当前旧草案：\n{existing_draft}\n\n"
    if revision_instructions:
        prompt += f"修订要求：\n{revision_instructions}\n\n"
    prompt += "请完成当前模块。"
    return [
        SystemMessage(content=get_worker_system_prompt(module_plan.get("owner_role", ""))),
        HumanMessage(content=prompt),
    ]


def build_aggregator_messages(brief: dict, module_outputs: dict[str, dict]) -> list:
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    return [
        SystemMessage(content=AGGREGATOR_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"research brief: {brief}\n\n"
                f"目标产物章节：{list(profile.final_sections)}\n\n"
                f"模块草案：{module_outputs}"
            )
        ),
    ]


def build_evaluator_messages(
    brief: dict,
    aggregated_draft: str,
    module_outputs: dict[str, dict],
    evidence_bank: list[dict],
) -> list:
    return [
        SystemMessage(content=evaluator_system_prompt()),
        HumanMessage(
            content=(
                f"research brief: {brief}\n\n"
                f"aggregated_draft:\n{aggregated_draft}\n\n"
                f"module_outputs:\n{module_outputs}\n\n"
                f"evidence_bank:\n{evidence_bank}"
            )
        ),
    ]


def build_optimizer_messages(
    brief: dict,
    revision_targets: list[dict],
    locked_modules: dict[str, str],
    module_outputs: dict[str, dict],
    feedback_history: list[dict],
) -> list:
    return [
        SystemMessage(content=OPTIMIZER_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"research brief: {brief}\n\n"
                f"revision_targets: {revision_targets}\n\n"
                f"locked_modules: {locked_modules}\n\n"
                f"module_outputs: {module_outputs}\n\n"
                f"recent_feedback: {feedback_history[-3:]}"
            )
        ),
    ]


def build_synthesizer_messages(
    brief: dict,
    module_outputs: dict[str, dict],
    evaluation: dict,
) -> list:
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    return [
        SystemMessage(content=SYNTHESIZER_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"research brief: {brief}\n\n"
                f"final_sections: {list(profile.final_sections)}\n\n"
                f"module_outputs: {module_outputs}\n\n"
                f"latest_evaluation: {evaluation}"
            )
        ),
    ]


__all__ = [
    "ACADEMIC_PAPER_GUIDANCE_PROFILE",
    "DEFAULT_REPORT_PROFILE",
    "build_aggregator_messages",
    "build_evaluator_messages",
    "build_intake_messages",
    "build_module_worker_messages",
    "build_optimizer_messages",
    "build_planner_messages",
    "build_synthesizer_messages",
    "get_worker_system_prompt",
    "looks_like_academic_paper_task",
    "normalize_report_profile",
    "resolve_deliverable_type",
    "resolve_report_profile",
]
