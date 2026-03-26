"""科研工作流配置与产物模板定义。"""
from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_REPORT_PROFILE = "default"
ACADEMIC_PAPER_GUIDANCE_PROFILE = "academic_paper_guidance"

DEFAULT_DELIVERABLE_TYPE = "literature_review"
ACADEMIC_DELIVERABLE_TYPE = "paper_guidance"

DEFAULT_RESEARCH_MODE = "review"

REPORT_PROFILE_ALIASES = {
    "default": DEFAULT_REPORT_PROFILE,
    "general": DEFAULT_REPORT_PROFILE,
    "academic": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_intro": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper_guidance": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
}

REPORT_PROFILE_TO_DELIVERABLE = {
    DEFAULT_REPORT_PROFILE: DEFAULT_DELIVERABLE_TYPE,
    ACADEMIC_PAPER_GUIDANCE_PROFILE: ACADEMIC_DELIVERABLE_TYPE,
}

ACADEMIC_PROFILE_KEYWORDS = (
    "论文",
    "paper",
    "引言",
    "introduction",
    "绪论",
    "related work",
    "文献综述",
    "literature review",
    "综述",
    "创新点",
    "research gap",
    "研究空白",
    "baseline",
    "ablation",
    "实验设计",
    "投稿",
    "审稿",
    "reference",
    "参考文献",
    "科研写作",
    "后续论文怎么写",
)


@dataclass(frozen=True)
class DeliverableProfile:
    """最终产物模板。

    用来约束 planner 必须包含哪些模块，以及 synthesizer 最终要长成什么结构。
    """

    name: str
    label: str
    description: str
    required_modules: tuple[str, ...]
    final_sections: tuple[str, ...]
    evaluator_focus: tuple[str, ...]


@dataclass
class ResearchConfig:
    """科研工作流的运行时参数。"""

    max_worker_rounds: int = 4
    max_parallel_workers: int = 3
    max_revision_cycles: int = 2
    clarification_attempts: int = 1
    worker_search_budget_by_role: dict[str, int] = field(
        default_factory=lambda: {
            "citation_worker": 3,
            "argument_worker": 2,
            "method_worker": 2,
        }
    )
    worker_search_budget_by_module: dict[str, int] = field(
        default_factory=lambda: {
            "problem_definition": 2,
            "related_work": 6,
            "argument_map": 2,
            "contributions": 1,
            "limitations": 2,
            "method_candidates": 3,
            "experiment_design": 2,
        }
    )

    @classmethod
    def from_dict(cls, data: dict | None) -> "ResearchConfig":
        payload = data or {}
        return cls(**{k: payload[k] for k in payload if k in cls.__dataclass_fields__})

    def search_budget_for(self, module_id: str, owner_role: str) -> int:
        module_key = str(module_id or "").strip()
        role_key = str(owner_role or "").strip()
        if module_key in self.worker_search_budget_by_module:
            return max(int(self.worker_search_budget_by_module[module_key]), 0)
        return max(int(self.worker_search_budget_by_role.get(role_key, self.max_worker_rounds)), 0)


DELIVERABLE_PROFILES: dict[str, DeliverableProfile] = {
    DEFAULT_DELIVERABLE_TYPE: DeliverableProfile(
        name=DEFAULT_DELIVERABLE_TYPE,
        label="文献综述",
        description="输出一份面向研究决策的文献综述与研究建议。",
        required_modules=(
            "problem_definition",
            "related_work",
            "argument_map",
            "contributions",
            "limitations",
        ),
        final_sections=(
            "## 研究问题定义",
            "## 相关工作与证据综述",
            "## 研究空白与论证链",
            "## 可主张的贡献",
            "## 局限性与风险",
            "## 参考文献",
        ),
        evaluator_focus=(
            "citation_authenticity_traceability",
            "citation_relevance_coverage",
            "citation_recency",
            "argument_chain_completeness",
            "intent_alignment",
        ),
    ),
    ACADEMIC_DELIVERABLE_TYPE: DeliverableProfile(
        name=ACADEMIC_DELIVERABLE_TYPE,
        label="论文写作指导",
        description="输出可直接服务论文、科研方案、实验规划的结构化草案。",
        required_modules=(
            "problem_definition",
            "related_work",
            "method_candidates",
            "experiment_design",
            "argument_map",
            "contributions",
            "limitations",
        ),
        final_sections=(
            "## 文献综述正文",
            "## 研究空白与可切入点",
            "## 方法与实验路径建议",
            "## 对后续论文写作的明确建议",
            "## 当前可以主张的点",
            "## 当前不能写过头的点",
            "## 建议的论文结构",
            "## 参考文献",
        ),
        evaluator_focus=(
            "citation_authenticity_traceability",
            "citation_relevance_coverage",
            "citation_recency",
            "methodological_rigor",
            "experimental_feasibility",
            "argument_chain_completeness",
            "intent_alignment",
        ),
    ),
    "paper_outline": DeliverableProfile(
        name="paper_outline",
        label="论文提纲",
        description="输出论文结构、段落目标与证据缺口清单。",
        required_modules=(
            "problem_definition",
            "related_work",
            "argument_map",
            "contributions",
        ),
        final_sections=(
            "## 论文主线",
            "## Introduction 分段建议",
            "## Related Work 组织建议",
            "## Method 与 Experiment 结构建议",
            "## 证据缺口与补充建议",
        ),
        evaluator_focus=(
            "citation_relevance_coverage",
            "argument_chain_completeness",
            "intent_alignment",
        ),
    ),
    "research_proposal": DeliverableProfile(
        name="research_proposal",
        label="研究方案",
        description="输出研究目标、方法、实验设计与风险评估。",
        required_modules=(
            "problem_definition",
            "method_candidates",
            "experiment_design",
            "contributions",
            "limitations",
        ),
        final_sections=(
            "## 研究目标与问题定义",
            "## 方法候选与选择理由",
            "## 实验设计与评估指标",
            "## 预期贡献",
            "## 风险与局限性",
        ),
        evaluator_focus=(
            "methodological_rigor",
            "experimental_feasibility",
            "intent_alignment",
        ),
    ),
}


def normalize_report_profile(report_profile: str | None) -> str:
    """把外部传入的 profile 名称归一化。"""
    normalized = str(report_profile or "").strip().lower()
    if not normalized:
        return DEFAULT_REPORT_PROFILE
    return REPORT_PROFILE_ALIASES.get(normalized, normalized)


def looks_like_academic_paper_task(query: str) -> bool:
    """根据关键词判断当前是否偏论文/科研写作任务。"""
    lowered = str(query or "").lower()
    return any(keyword in lowered for keyword in ACADEMIC_PROFILE_KEYWORDS)


def resolve_report_profile(query: str, requested_profile: str | None = None) -> str:
    """根据显式指定值和 query 内容决定 report_profile。"""
    raw_requested = str(requested_profile or "").strip()
    normalized = normalize_report_profile(requested_profile) if raw_requested else ""
    if normalized in REPORT_PROFILE_TO_DELIVERABLE:
        return normalized
    if looks_like_academic_paper_task(query):
        return ACADEMIC_PAPER_GUIDANCE_PROFILE
    return DEFAULT_REPORT_PROFILE


def resolve_deliverable_type(query: str, requested_profile: str | None = None) -> str:
    """把旧的 report_profile 输入映射到新的产物类型。"""
    profile = resolve_report_profile(query, requested_profile)
    return REPORT_PROFILE_TO_DELIVERABLE.get(profile, DEFAULT_DELIVERABLE_TYPE)


def get_deliverable_profile(deliverable_type: str | None) -> DeliverableProfile:
    """获取某种产物类型对应的模板。未知值回落到默认综述模板。"""
    key = str(deliverable_type or "").strip().lower() or DEFAULT_DELIVERABLE_TYPE
    return DELIVERABLE_PROFILES.get(key, DELIVERABLE_PROFILES[DEFAULT_DELIVERABLE_TYPE])
