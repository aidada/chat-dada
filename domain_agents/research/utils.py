from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capabilities.citation_manager import CitationMap
from capabilities.evidence_store import EvidenceCollection, EvidenceItem
from core.content_utils import extract_result_text, normalize_markdown_report

from domain_agents.research.config import (
    DEFAULT_DELIVERABLE_TYPE,
    DEFAULT_RESEARCH_MODE,
    get_deliverable_profile,
    resolve_deliverable_type,
    resolve_report_profile,
)
from domain_agents.research.schemas import (
    ResearchBrief,
    ResearchEvidence,
    ResearchModuleDraft,
    ResearchModulePlan,
)

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


def extract_json_payload(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start == -1 or end == -1 or end <= start:
            continue
        chunk = raw[start : end + 1]
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def collect_urls(text: str) -> list[str]:
    return list(dict.fromkeys(_URL_RE.findall(str(text or ""))))


def fallback_brief(query: str, requested_profile: str | None, input_data: dict[str, Any]) -> dict[str, Any]:
    deliverable_type = str(
        input_data.get("deliverable_type") or resolve_deliverable_type(query, requested_profile)
    ).strip() or DEFAULT_DELIVERABLE_TYPE

    research_mode = str(input_data.get("research_mode") or "").strip().lower()
    lowered = query.lower()
    if not research_mode:
        if any(token in lowered for token in ("实验", "empirical", "benchmark", "ablation", "定量")):
            research_mode = "empirical"
        elif deliverable_type == "paper_guidance":
            research_mode = "mixed"
        else:
            research_mode = DEFAULT_RESEARCH_MODE

    languages = input_data.get("literature_languages") or []
    if not languages:
        if "中文" in query and "英文" not in query:
            languages = ["zh"]
        elif "英文" in query:
            languages = ["en"]
        else:
            languages = ["en", "zh"]

    emphasis: list[str] = []
    if "近" in query or "recent" in lowered:
        emphasis.append("recent literature")
    if "工程" in query or "engineering" in lowered:
        emphasis.append("engineering feasibility")
    if "综述" in query or "related work" in lowered:
        emphasis.append("related work quality")

    constraints = input_data.get("constraints")
    if isinstance(constraints, str) and constraints.strip():
        user_constraints = [constraints.strip()]
    elif isinstance(constraints, list):
        user_constraints = [str(item).strip() for item in constraints if str(item).strip()]
    else:
        user_constraints = []

    profile = get_deliverable_profile(deliverable_type)
    brief = ResearchBrief(
        raw_query=query,
        clarified_goal=str(input_data.get("clarified_goal") or query).strip(),
        discipline=str(input_data.get("discipline") or "").strip(),
        deliverable_type=deliverable_type,
        research_mode=research_mode,
        time_scope=str(input_data.get("time_scope") or "recent + seminal"),
        literature_languages=[str(item) for item in languages],
        citation_style=str(input_data.get("citation_style") or "APA"),
        output_language=str(input_data.get("output_language") or "zh-CN"),
        user_constraints=user_constraints,
        success_criteria=[
            "模块化中间稿可评估",
            "引用可追溯",
            "低分模块可单独修订",
            f"最终产物符合 {profile.label} 目标",
        ],
        unresolved_questions=[],
        preferred_emphasis=emphasis,
    )
    return brief.model_dump()


def merge_brief(base: dict[str, Any], override: dict[str, Any], input_data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value in (None, "", [], {}):
            continue
        merged[key] = value

    for key in (
        "discipline",
        "deliverable_type",
        "research_mode",
        "time_scope",
        "literature_languages",
        "citation_style",
        "output_language",
    ):
        if input_data.get(key):
            merged[key] = input_data[key]

    merged["deliverable_type"] = str(merged.get("deliverable_type") or DEFAULT_DELIVERABLE_TYPE)
    merged["clarified_goal"] = str(merged.get("clarified_goal") or merged.get("raw_query") or "")
    merged["user_constraints"] = [str(item) for item in merged.get("user_constraints", []) if str(item).strip()]
    merged["preferred_emphasis"] = [str(item) for item in merged.get("preferred_emphasis", []) if str(item).strip()]
    merged["success_criteria"] = [str(item) for item in merged.get("success_criteria", []) if str(item).strip()]
    merged["unresolved_questions"] = [str(item) for item in merged.get("unresolved_questions", []) if str(item).strip()]
    return ResearchBrief(**merged).model_dump()


def fallback_plan(brief: dict[str, Any]) -> dict[str, Any]:
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    blueprint = {
        "problem_definition": {
            "title": "研究问题定义",
            "owner_role": "citation_worker",
            "objective": "界定研究问题、应用场景、关键术语和评价目标。",
            "depends_on": [],
            "required_evidence": ["problem framing", "scenario background"],
            "required_output_fields": ["problem statement", "scope", "assumptions"],
            "evaluation_dimensions": ["intent_alignment", "argument_chain_completeness"],
        },
        "related_work": {
            "title": "相关工作与文献脉络",
            "owner_role": "citation_worker",
            "objective": "梳理主流文献脉络、代表性工作、证据强度和研究空白。",
            "depends_on": ["problem_definition"],
            "required_evidence": ["papers", "surveys", "benchmarks"],
            "required_output_fields": ["key papers", "coverage", "gaps"],
            "evaluation_dimensions": [
                "citation_authenticity_traceability",
                "citation_relevance_coverage",
                "citation_recency",
            ],
        },
        "method_candidates": {
            "title": "方法候选",
            "owner_role": "method_worker",
            "objective": "提出与研究问题匹配的方法候选、变量、数据依赖和评价指标。",
            "depends_on": ["problem_definition", "related_work"],
            "required_evidence": ["method evidence", "comparative baselines"],
            "required_output_fields": ["candidate methods", "variables", "metrics"],
            "evaluation_dimensions": ["methodological_rigor", "intent_alignment"],
        },
        "experiment_design": {
            "title": "实验设计",
            "owner_role": "method_worker",
            "objective": "给出实验流程、数据、baseline、ablation 和误差分析建议。",
            "depends_on": ["method_candidates"],
            "required_evidence": ["datasets", "benchmarks", "evaluation metrics"],
            "required_output_fields": ["experiment plan", "metrics", "risks"],
            "evaluation_dimensions": ["experimental_feasibility", "methodological_rigor"],
        },
        "argument_map": {
            "title": "论证链",
            "owner_role": "argument_worker",
            "objective": "连接研究背景、空白、方法路径和结论边界，形成闭环论证。",
            "depends_on": ["problem_definition", "related_work"],
            "required_evidence": ["supported claims", "counterpoints"],
            "required_output_fields": ["logic chain", "claim boundaries", "support level"],
            "evaluation_dimensions": ["argument_chain_completeness", "intent_alignment"],
        },
        "contributions": {
            "title": "贡献与创新点",
            "owner_role": "argument_worker",
            "objective": "总结当前可主张的贡献，区分保守可写与需补证据后再写的主张。",
            "depends_on": ["argument_map", "method_candidates"],
            "required_evidence": ["claim support"],
            "required_output_fields": ["defensible claims", "innovation framing"],
            "evaluation_dimensions": ["argument_chain_completeness", "intent_alignment"],
        },
        "limitations": {
            "title": "局限性与风险",
            "owner_role": "argument_worker",
            "objective": "说明当前证据边界、实验缺口、潜在反例和写作风险。",
            "depends_on": ["argument_map", "experiment_design"],
            "required_evidence": ["evidence gaps", "method risks"],
            "required_output_fields": ["limitations", "risks", "next evidence needs"],
            "evaluation_dimensions": ["experimental_feasibility", "argument_chain_completeness"],
        },
    }

    modules: list[dict[str, Any]] = []
    included = set(profile.required_modules)
    if "method_candidates" in included and "related_work" not in included:
        included.add("related_work")
    if "experiment_design" in included:
        included.add("method_candidates")
    if "contributions" in included:
        included.add("argument_map")
    if "limitations" in included:
        included.add("argument_map")

    for module_id in (
        "problem_definition",
        "related_work",
        "method_candidates",
        "experiment_design",
        "argument_map",
        "contributions",
        "limitations",
    ):
        if module_id not in included:
            continue
        base = blueprint[module_id]
        depends_on = [dep for dep in base["depends_on"] if dep in included]
        modules.append(
            ResearchModulePlan(
                module_id=module_id,
                title=base["title"],
                module_type=module_id,
                owner_role=base["owner_role"],
                objective=base["objective"],
                depends_on=depends_on,
                required_evidence=base["required_evidence"],
                required_output_fields=base["required_output_fields"],
                evaluation_dimensions=base["evaluation_dimensions"],
            ).model_dump()
        )

    return {"modules": modules, "checkpoints": ["checkpoint_a", "checkpoint_b", "checkpoint_c"]}


def normalize_plan(plan_data: dict[str, Any] | None, brief: dict[str, Any]) -> dict[str, Any]:
    fallback = fallback_plan(brief)
    if not isinstance(plan_data, dict):
        return fallback
    modules = plan_data.get("modules")
    if not isinstance(modules, list):
        return fallback

    normalized: list[dict[str, Any]] = []
    for item in modules:
        if not isinstance(item, dict):
            continue
        try:
            normalized.append(ResearchModulePlan(**item).model_dump())
        except Exception:
            continue

    if not normalized:
        return fallback

    required_ids = {module["module_id"] for module in fallback["modules"]}
    normalized_ids = {module["module_id"] for module in normalized}
    if missing := [module for module in fallback["modules"] if module["module_id"] not in normalized_ids]:
        normalized.extend(missing)

    ordered: list[dict[str, Any]] = []
    for module_id in [module["module_id"] for module in fallback["modules"]]:
        match = next((module for module in normalized if module["module_id"] == module_id), None)
        if match:
            match["depends_on"] = [dep for dep in match.get("depends_on", []) if dep in required_ids | normalized_ids]
            ordered.append(match)

    return {
        "modules": ordered,
        "checkpoints": plan_data.get("checkpoints") or fallback["checkpoints"],
    }


def module_dependency_context(module_plan: dict[str, Any], module_outputs: dict[str, dict[str, Any]]) -> str:
    parts: list[str] = []
    for dep in module_plan.get("depends_on", []):
        draft = module_outputs.get(dep) or {}
        content = str(draft.get("content", "") or "").strip()
        if content:
            parts.append(f"## {dep}\n{content}")
    return "\n\n".join(parts)


def aggregate_module_outputs(brief: dict[str, Any], module_outputs: dict[str, dict[str, Any]]) -> str:
    profile = get_deliverable_profile(brief.get("deliverable_type"))
    sections: list[str] = []
    for module_id in [module["module_id"] for module in fallback_plan(brief)["modules"]]:
        draft = module_outputs.get(module_id) or {}
        content = str(draft.get("content", "") or "").strip()
        if not content:
            continue
        title = next(
            (module["title"] for module in fallback_plan(brief)["modules"] if module["module_id"] == module_id),
            module_id,
        )
        sections.append(f"## {title}\n\n{content}")

    if not sections:
        sections.append(
            "## 当前进展\n\n尚未形成可评估草案，需要继续完成模块化研究。"
        )

    header = (
        f"# {profile.label}\n\n"
        f"目标：{brief.get('clarified_goal', brief.get('raw_query', ''))}\n\n"
    )
    return normalize_markdown_report(header + "\n\n".join(sections))


def build_evidence_records(module_id: str, title: str, text: str) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for index, url in enumerate(collect_urls(text), start=1):
        evidence.append(
            ResearchEvidence(
                evidence_id=f"{module_id}_ev_{index}",
                title=title,
                url=url,
                source_type="paper" if any(token in url for token in ("arxiv", "semanticscholar", "doi")) else "web",
                snippet=str(text or "")[:400],
                claim_supported=title,
                relevance_score=0.8,
                recency_score=0.6,
                traceable=True,
            ).model_dump()
        )
    return evidence


def merge_evidence(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_url: dict[str, dict[str, Any]] = {}
    for item in [*existing, *additions]:
        url = str(item.get("url", "") or "").strip()
        key = url or str(item.get("evidence_id", ""))
        by_url[key] = item
    return list(by_url.values())


def build_citation_bank(evidence_bank: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citations = CitationMap()
    for item in evidence_bank:
        url = str(item.get("url", "") or "").strip()
        if not url:
            continue
        citations.add(url, title=str(item.get("title", "") or ""), snippet=str(item.get("snippet", "") or ""))
    return citations.to_dicts()


def lock_module_snapshot(module_outputs: dict[str, dict[str, Any]], module_ids: list[str]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for module_id in module_ids:
        draft = module_outputs.get(module_id) or {}
        content = str(draft.get("content", "") or "").strip()
        if content:
            snapshot[module_id] = content
    return snapshot


def feedback_action(feedback: str | None) -> str:
    text = str(feedback or "").strip().lower()
    if not text:
        return "accept"
    if any(token in text for token in ("重做", "重来", "改方向", "换成", "不是这个", "replan", "重新规划")):
        return "replan"
    if any(token in text for token in ("继续", "通过", "可以", "ok", "accept", "looks good")):
        return "accept"
    return "revise"


def feedback_to_revision_targets(feedback: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(feedback or "").strip()
    if not text:
        return []
    lowered = text.lower()
    module_ids: list[str] = []
    keyword_map = {
        "related_work": ("文献", "引用", "相关工作", "reference", "citation"),
        "method_candidates": ("方法", "模型", "算法", "method"),
        "experiment_design": ("实验", "benchmark", "ablation", "dataset", "baseline"),
        "argument_map": ("论证", "逻辑", "背景", "研究空白", "motivation"),
        "contributions": ("贡献", "创新点", "claim"),
        "limitations": ("局限", "风险", "不能写过头"),
        "problem_definition": ("问题定义", "范围", "场景"),
    }
    for module_id, tokens in keyword_map.items():
        if any(token in lowered for token in tokens):
            module_ids.append(module_id)

    if not module_ids:
        module_ids = [module["module_id"] for module in plan.get("modules", []) if module["module_id"] in {
            "argument_map",
            "contributions",
            "limitations",
        }] or [module["module_id"] for module in plan.get("modules", [])[:2]]

    return [
        {
            "module_id": module_id,
            "reason": text,
            "priority": "medium",
            "actions": ["根据最新用户反馈修订当前模块"],
            "preserve_constraints": ["保持其他锁定模块不变"],
            "requires_new_evidence": any(token in lowered for token in ("引用", "文献", "evidence", "证据")),
        }
        for module_id in module_ids
    ]


def build_evidence_and_citations(
    task_id: str,
    report: str,
    module_outputs: dict[str, dict[str, Any]] | list[dict[str, Any]] | None = None,
) -> tuple[EvidenceCollection, CitationMap]:
    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()

    def _add_url(url: str, title: str, summary: str) -> None:
        evidence.add(
            EvidenceItem(
                evidence_id=f"ev_{len(evidence.items) + 1}",
                evidence_type="url",
                source=url,
                summary=summary,
            )
        )
        citations.add(url, title=title)

    if isinstance(module_outputs, dict):
        for module_id, draft in module_outputs.items():
            content = str(draft.get("content", "") or "")
            for url in collect_urls(content):
                _add_url(url, module_id, f"Collected from module: {module_id}")
    elif isinstance(module_outputs, list):
        for item in module_outputs:
            module_id = str(item.get("subtask_id", item.get("module_id", "worker")) or "worker")
            title = str(item.get("topic", module_id) or module_id)
            content = str(item.get("findings", item.get("content", "")) or "")
            for url in collect_urls(content):
                _add_url(url, title, f"Collected from module: {module_id}")

    for url in collect_urls(report):
        _add_url(url, "final_report", "Extracted from final report")

    return evidence, citations


def persist_evidence_and_citations(
    task_dir: Path,
    evidence: EvidenceCollection,
    citations: CitationMap,
) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if evidence.items:
        path = task_dir / "evidence.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "evidence_id": item.evidence_id,
                        "type": item.evidence_type,
                        "source": item.source,
                        "summary": item.summary,
                        "confidence": item.confidence,
                        "metadata": item.metadata,
                    }
                    for item in evidence.items
                ],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        refs.append({"type": "file", "name": "evidence.json", "path": str(path)})

    if citations.all():
        path = task_dir / "citations.json"
        path.write_text(json.dumps(citations.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": "citations.json", "path": str(path)})

    return refs


def collect_artifact_refs(task_dir: Path) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for name in (
        "plan.json",
        "module_outputs.json",
        "evaluation.json",
        "aggregated_draft.md",
        "final_report.md",
    ):
        path = task_dir / name
        if path.exists():
            refs.append({"type": "file", "name": path.name, "path": str(path)})
    return refs


def strategy_summary(trace: list[str]) -> str:
    cleaned = [item for item in trace if item]
    if not cleaned:
        return "research_workflow"
    return f"research_workflow({' → '.join(cleaned)})"


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def best_text(response: Any) -> str:
    return extract_result_text(response)
