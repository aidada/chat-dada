"""
Deep Research Agent — prompt construction and report profile helpers.
"""
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from domain_agents.research.config import (
    ACADEMIC_PAPER_GUIDANCE_PROFILE,
    ACADEMIC_PROFILE_KEYWORDS,
    DEFAULT_REPORT_PROFILE,
    REPORT_PROFILE_ALIASES,
    REPORT_PROFILES,
    ReportProfile,
)


BASE_RESEARCH_SYSTEM = """你是一个专业的深度研究员。你的任务是围绕用户提出的问题做多轮检索，并给出直接、严谨、可执行的研究结论。

策略：
1. 先识别用户真正想知道的判断题、机理问题、工程问题。
1.1 如果用户问题存在关键歧义，而且这种歧义会改变检索方向、评价指标或工程判断，先调用 `ask_user_clarification` 追问一次；问题必须短、具体、可操作，且最多追问一次。
2. 优先搜索能直接回答该问题的论文、综述、实验结果、方法细节，不要只堆背景材料。
3. 搜索工具选择策略：
   - web_search（Tavily）：通用网页搜索，返回较完整摘要，速度快成本低，适合初步信息收集
   - brave_search：快速发现候选网页和来源列表，适合广撒网摸底
   - academic_search：搜索 Semantic Scholar + arXiv 论文，免费，适合找特定论文、作者、引用
   - exa_deep_search：AI 深度语义搜索，返回全文和要点提取，延迟高(5-60s)成本高，仅在快速搜索无法满足时使用，适合填补关键证据缺口或寻找深度分析文章
   - browser_navigate：浏览器抓取具体页面，适合动态内容或需要多步交互
   选择原则：先用快速工具(web_search/brave_search)建立信息基础，再用 academic_search 补充学术文献，最后用 exa_deep_search 填补关键缺口。避免在信息已充足时调用高成本工具。
4. 多角度搜索：中文 + 英文关键词；支持词、反对词、边界条件都要覆盖。
5. 每一轮最多调用 1-2 个工具，优先补齐最关键的信息缺口。
6. 如果缺少实验数据、公式、可观测性条件、工程限制，默认信息仍不足，不要过早停止。

最终输出要求：
1. 必须先回答用户真正关心的问题，而不是先铺陈背景。
2. 关键结论后要给来源 URL，或明确说明“当前仅有摘要级证据”。
3. 如果没有检索到关键实验数字、公式或正文证据，不得模糊带过，必须明确写出缺口。"""


BASE_FINAL_REPORT_SYSTEM = """你是研究报告编辑。请把已有研究笔记改写成一份直接回答用户问题的最终报告。

硬性要求：
1. 只能基于提供的研究笔记重写，不要新增笔记里没有的事实。
2. 结论必须紧扣用户问题本身，避免泛泛背景综述。
3. 如果证据只有摘要级、缺少实验数字或缺少正文公式，必须明确标注证据强度和缺口。"""


def _normalize_report_profile(report_profile: str | None) -> str:
    normalized = str(report_profile or "").strip().lower()
    if not normalized:
        return ""
    return REPORT_PROFILE_ALIASES.get(normalized, normalized)


def _get_report_profile(report_profile: str | None) -> ReportProfile:
    normalized = _normalize_report_profile(report_profile)
    return REPORT_PROFILES.get(normalized, REPORT_PROFILES[DEFAULT_REPORT_PROFILE])


def _looks_like_academic_paper_task(query: str) -> bool:
    lowered = str(query or "").lower()
    return any(keyword in lowered for keyword in ACADEMIC_PROFILE_KEYWORDS)


def _resolve_report_profile(query: str, requested_profile: str | None = None) -> str:
    normalized = _normalize_report_profile(requested_profile)
    if normalized in REPORT_PROFILES:
        return normalized
    if _looks_like_academic_paper_task(query):
        return ACADEMIC_PAPER_GUIDANCE_PROFILE
    return DEFAULT_REPORT_PROFILE


def _build_research_system(report_profile: str) -> str:
    profile = _get_report_profile(report_profile)
    if not profile.research_addendum:
        return BASE_RESEARCH_SYSTEM
    return f"{BASE_RESEARCH_SYSTEM}\n\n附加模板要求（{profile.name}）：\n{profile.research_addendum}"


def _build_final_report_system(report_profile: str) -> str:
    profile = _get_report_profile(report_profile)
    return f"{BASE_FINAL_REPORT_SYSTEM}\n\n附加模板要求（{profile.name}）：\n{profile.final_addendum}"


def _build_research_messages(query: str, context: str, report_profile: str = DEFAULT_REPORT_PROFILE, attention_block: str = "") -> list[BaseMessage]:
    notes = context or "(暂无研究笔记)"
    profile = _get_report_profile(report_profile)
    section_requirements = "\n".join(f"   `{section}`" for section in profile.final_sections)
    prompt = (
        f"研究主题：{query}\n\n"
        f"当前输出模板：{profile.name}\n\n"
        f"当前研究笔记（已压缩）：\n{notes}\n\n"
        "请基于当前笔记决定下一步：\n"
        "0. 如果当前笔记里还没有用户澄清，而研究目标、评价维度或输出重点存在关键歧义，先调用 ask_user_clarification；最多三次。\n"
        "1. 如果还缺少以下任一项，就继续调用最必要的 1-2 个工具：直接结论、关键证据、成立条件、工程限制、关键数据或明确缺口。\n"
        "2. 如果信息已经足够，直接输出最终研究报告。\n"
        "3. 不要重复已经完成的搜索。\n"
        "4. 最终报告必须围绕用户问题本身回答：命题是否成立、为什么、依赖哪些条件、工程上如何落地、还有哪些证据缺口。\n"
        "5. 不要把答案写成宽泛综述；优先给出判断、再给证据和限制。\n"
        "6. 最终报告至少要包含以下二级标题：\n"
        f"{section_requirements}"
    )
    if attention_block:
        prompt += f"\n\n{attention_block}"
    return [
        SystemMessage(content=_build_research_system(report_profile)),
        HumanMessage(content=prompt),
    ]
