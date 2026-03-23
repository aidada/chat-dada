"""
Deep Research Agent — configuration, data types, and constants.
"""
from dataclasses import dataclass
from typing import Annotated
from typing_extensions import TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    report_profile: str
    research_context: dict      # serialized ResearchContext — 唯一数据源
    task_id: str                # research memory task ID
    progress: dict              # serialized ProgressTracker
    research_plan: dict         # serialized ResearchPlan (P1-1)
    current_subtask: dict       # current subtask or {} (P1-1)


CHECKPOINT_INTERVAL = 5
SUMMARY_INTERVAL = 6


@dataclass
class ResearchConfig:
    max_steps: int = 15
    checkpoint_interval: int = 5
    summary_interval: int = 6
    max_parallel_workers: int = 3
    raw_content_threshold: int = 8000
    compact_snippet_length: int = 200

    @classmethod
    def from_dict(cls, data: dict) -> "ResearchConfig":
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})


DEFAULT_REPORT_PROFILE = "default"
ACADEMIC_PAPER_GUIDANCE_PROFILE = "academic_paper_guidance"


@dataclass(frozen=True)
class ReportProfile:
    name: str
    research_addendum: str
    final_addendum: str
    final_sections: tuple[str, ...]


REPORT_PROFILES: dict[str, ReportProfile] = {
    DEFAULT_REPORT_PROFILE: ReportProfile(
        name=DEFAULT_REPORT_PROFILE,
        research_addendum="",
        final_addendum=(
            "默认输出是一份问题导向的研究报告。\n"
            "你必须使用 Markdown 二级标题，且至少包含：\n"
            "`## 直接结论`\n"
            "`## 证据链`\n"
            "`## 机理与成立条件`\n"
            "`## 工程判断`\n"
            "`## 限制与证据缺口`\n"
            "`## 参考来源`\n"
            "开头必须是 `## 直接结论`，用 3-5 句直接回答“是否成立、为什么、依赖什么条件、工程上如何理解”。"
        ),
        final_sections=(
            "## 直接结论",
            "## 证据链",
            "## 机理与成立条件",
            "## 工程判断",
            "## 限制与证据缺口",
            "## 参考来源",
        ),
    ),
    ACADEMIC_PAPER_GUIDANCE_PROFILE: ReportProfile(
        name=ACADEMIC_PAPER_GUIDANCE_PROFILE,
        research_addendum=(
            "当前任务是“科研论文写作导向”的深度研究，不是普通报告。\n"
            "检索时除结论本身外，还要额外提取：\n"
            "1. 问题背景与研究动机如何铺垫；\n"
            "2. 主流方法脉络、代表性论文及其局限；\n"
            "3. 现有工作尚未解决的研究空白；\n"
            "4. 能支撑后续论文写作的 claim、证据强度与风险边界；\n"
            "5. 后续论文必须补的实验、baseline、ablation、误差分析与图表。\n"
            "如果证据不足以支撑论文写作建议，必须明确标记“需要补证据后再写”。"
        ),
        final_addendum=(
            "你现在输出的是“为后续论文写作服务的科研综述型报告”，不是普通研究报告。\n"
            "硬性要求：\n"
            "1. 先写 `## 文献综述正文`，使用连续段落，尽量模拟论文 introduction / 绪论 / related work 的写法，而不是项目符号堆砌。\n"
            "2. 每个事实判断、方法归纳、趋势结论后都要尽量附引用编号，如 [1]、[2]；如果研究笔记里的出处信息不足以稳定编号，则在句末保留来源 URL 或明确说明出处信息不完整。\n"
            "3. 综述之后必须单独给出“对后续论文写作的明确建议”，且必须回答：核心问题怎么定义、创新点怎么表述、Introduction 如何分段、Experiment 还需要补哪些关键实验。\n"
            "4. 必须把“当前可以主张的点”和“当前不能写过头的点”分开写，防止把摘要级证据写成强结论。\n"
            "5. 所有写作建议都必须基于前文文献证据，不得脱离证据自由发挥。\n"
            "6. 如果某项建议依赖尚未补齐的实验、公式或正文细节，必须明确标注“需要补证据后再写”。\n"
            "7. 末尾必须有 `## 参考文献`，按编号或可识别顺序列出题目、作者、年份、来源、链接；若信息缺失，必须标注缺失项。"
        ),
        final_sections=(
            "## 文献综述正文",
            "## 研究空白与可切入点",
            "## 对后续论文写作的明确建议",
            "## 当前可以主张的点",
            "## 当前不能写过头的点",
            "## 建议的论文结构",
            "## 建议补充的实验与材料",
            "## 参考文献",
        ),
    ),
}

REPORT_PROFILE_ALIASES = {
    "general": DEFAULT_REPORT_PROFILE,
    "default": DEFAULT_REPORT_PROFILE,
    "academic": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_intro": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "academic_paper_guidance": ACADEMIC_PAPER_GUIDANCE_PROFILE,
    "paper": ACADEMIC_PAPER_GUIDANCE_PROFILE,
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
