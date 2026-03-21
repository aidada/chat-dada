from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

from capabilities.budget_policy import BudgetPolicy
from capabilities.citation_manager import CitationMap
from capabilities.evidence_store import EvidenceCollection, EvidenceItem
from core.content_utils import extract_result_text
from domain_agents.patent.prompts import PATENT_DOMAIN_PROMPT
from domain_agents.patent.renderers import render_patent_markdown
from domain_agents.patent.reviewers import PatentReviewGate
from domain_agents.patent.schemas import (
    ClaimNode,
    ClaimTree,
    PatentRiskNote,
    PriorArtItem,
    PriorArtMatrix,
    PriorArtMatrixRow,
    SpecDraft,
    TechnicalDisclosure,
)
from domain_agents.patent.tools import browser_verify_patent_page, get_patent_tools

_log = logging.getLogger("chatdada.patent")


def _safe_emit(event_type: str, content: str) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        writer({"event_type": event_type, "content": content})
    except Exception:
        pass


PATENT_DATA_ROOT = Path("data/patent")


class PatentDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


# ---------------------------------------------------------------------------
# Deepagents-backed patent agent
# ---------------------------------------------------------------------------

async def build_deepagents_patent_agent() -> object:
    """Build a deepagents-backed patent agent with 5 specialised subagents."""
    from deepagents import create_deep_agent

    from domain_agents.patent.prompts import (
        CLAIM_DRAFTER_PROMPT,
        DISCLOSURE_ANALYST_PROMPT,
        PATENT_REVIEWER_PROMPT,
        PRIOR_ART_RESEARCHER_PROMPT,
        SPECIFICATION_DRAFTER_PROMPT,
    )

    tools = get_patent_tools()
    subagents = [
        {
            "name": "technical_disclosure_analyst",
            "description": "Extract structured technical disclosure from user input.",
            "system_prompt": DISCLOSURE_ANALYST_PROMPT,
            "tools": tools,
        },
        {
            "name": "prior_art_researcher",
            "description": "Search for prior art and map coverage against claims.",
            "system_prompt": PRIOR_ART_RESEARCHER_PROMPT,
            "tools": tools,
        },
        {
            "name": "claim_drafter",
            "description": "Draft a patent claim tree with independent and dependent claims.",
            "system_prompt": CLAIM_DRAFTER_PROMPT,
            "tools": tools,
        },
        {
            "name": "specification_drafter",
            "description": "Draft the patent specification document.",
            "system_prompt": SPECIFICATION_DRAFTER_PROMPT,
            "tools": tools,
        },
        {
            "name": "patent_reviewer",
            "description": "Review the full patent draft for structural and semantic issues.",
            "system_prompt": PATENT_REVIEWER_PROMPT,
            "tools": tools,
        },
    ]
    from core.models import build_chat_model

    return create_deep_agent(
        model=build_chat_model("patent_domain"),
        system_prompt=PATENT_DOMAIN_PROMPT,
        tools=tools,
        subagents=subagents,
        checkpointer=False,
        name="patent_domain_agent",
    )


# ---------------------------------------------------------------------------
# Heuristic (fallback) pipeline
# ---------------------------------------------------------------------------

def _extract_terms(query: str) -> list[str]:
    parts = [part.strip(" ,.;:") for part in query.replace("，", " ").replace("。", " ").split()]
    unique: list[str] = []
    for part in parts:
        if len(part) < 2 or part in unique:
            continue
        unique.append(part)
    return unique[:6]


def _build_disclosure(query: str) -> TechnicalDisclosure:
    terms = _extract_terms(query)
    title = terms[0] if terms else "Patent Disclosure"
    return TechnicalDisclosure(
        title=f"{title} 专利草案",
        summary=query,
        key_terms=terms,
        problem_statement=f"待解决问题：{query}",
        proposed_solution=f"通过结构化专利方案解决：{query}",
    )


def _build_claim_tree(disclosure: TechnicalDisclosure) -> ClaimTree:
    core_term = disclosure.key_terms[0] if disclosure.key_terms else "技术方案"
    return ClaimTree(
        claims=[
            ClaimNode(claim_id="C1", text=f"一种{core_term}的方法，包括：接收输入、执行处理、输出结果。"),
            ClaimNode(claim_id="C2", text=f"根据权利要求C1所述的方法，其中所述{core_term}包含配置校验步骤。", depends_on=["C1"]),
        ]
    )


def _build_prior_art(disclosure: TechnicalDisclosure, claim_tree: ClaimTree) -> tuple[list[PriorArtItem], PriorArtMatrix]:
    prior_art = PriorArtItem(
        title=f"{disclosure.title} 相关现有技术",
        source="heuristic",
        summary="基于当前输入生成的占位 prior-art 对照项，待后续接入检索结果强化。",
        relation_to_claims=[claim.claim_id for claim in claim_tree.claims],
    )
    matrix = PriorArtMatrix(
        rows=[
            PriorArtMatrixRow(
                claim_id=claim.claim_id,
                prior_art_title=prior_art.title,
                coverage_note="需人工核实现有技术覆盖度；当前为结构化占位。",
            )
            for claim in claim_tree.claims
        ]
    )
    return [prior_art], matrix


def _build_spec(disclosure: TechnicalDisclosure, claim_tree: ClaimTree) -> SpecDraft:
    return SpecDraft(
        title=disclosure.title,
        background=disclosure.problem_statement,
        summary=disclosure.proposed_solution,
        embodiments=[claim.text for claim in claim_tree.claims],
    )


async def _run_heuristic_patent(query: str, task_id: str, browser_enabled: bool, input_data: dict[str, Any]) -> str:
    """Heuristic pipeline — deterministic, no LLM calls."""
    disclosure = _build_disclosure(query)
    claim_tree = _build_claim_tree(disclosure)
    prior_art_items, matrix = _build_prior_art(disclosure, claim_tree)
    spec_draft = _build_spec(disclosure, claim_tree)

    if browser_enabled and input_data.get("browser_task"):
        verification = await browser_verify_patent_page(str(input_data["browser_task"]), enabled=True)
        prior_art_items.append(
            PriorArtItem(
                title="browser verification",
                source="browser",
                summary=verification,
                relation_to_claims=[claim.claim_id for claim in claim_tree.claims],
            )
        )

    return render_patent_markdown(disclosure, claim_tree, matrix, spec_draft)


# ---------------------------------------------------------------------------
# Artifact persistence (shared by both paths)
# ---------------------------------------------------------------------------

def _persist_artifacts(
    *,
    task_id: str,
    disclosure: TechnicalDisclosure,
    prior_art_items: list[PriorArtItem],
    claim_tree: ClaimTree,
    matrix: PriorArtMatrix,
    spec_draft: SpecDraft,
    report: str,
) -> list[dict[str, Any]]:
    task_dir = PATENT_DATA_ROOT / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "technical_disclosure.json": disclosure.model_dump(),
        "prior_art_items.json": [item.model_dump() for item in prior_art_items],
        "claim_tree.json": claim_tree.model_dump(),
        "prior_art_matrix.json": matrix.model_dump(),
        "spec_draft.json": spec_draft.model_dump(),
    }
    refs: list[dict[str, Any]] = []
    for name, payload in artifacts.items():
        path = task_dir / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        refs.append({"type": "file", "name": name, "path": str(path)})
    report_path = task_dir / "patent_draft.md"
    report_path.write_text(report, encoding="utf-8")
    refs.append({"type": "file", "name": report_path.name, "path": str(report_path)})
    return refs


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def run_patent_domain(input_data: dict[str, Any]) -> PatentDomainResult:
    query = str(input_data.get("query", input_data.get("task", "")) or "").strip()
    task_id = str(input_data.get("task_id", "") or "patent_preview")
    browser_enabled = bool(input_data.get("browser_enabled", False))
    use_deepagents = bool(input_data.get("use_deepagents", True))

    _safe_emit("step", f"📋 Patent domain started: '{query[:60]}'")

    strategy = "heuristic"
    final_text = ""

    if use_deepagents:
        strategy = "deepagents_harness"
        _safe_emit("step", "🤖 Building deepagents patent agent (5 subagents)...")
        try:
            agent = await build_deepagents_patent_agent()
            _safe_emit("step", "🚀 Executing deepagents patent pipeline...")
            response = await agent.ainvoke(
                {"messages": [HumanMessage(content=f"请根据以下技术信息生成完整的专利草案（技术交底、权利要求树、说明书、prior-art 矩阵）：\n\n{query}")]}
            )
            messages = response.get("messages", []) if isinstance(response, dict) else []
            for message in reversed(messages):
                if isinstance(message, AIMessage):
                    final_text = extract_result_text(getattr(message, "content", ""))
                    if final_text:
                        break
        except Exception as exc:
            _log.warning("Deepagents patent agent failed, falling back to heuristic: %s", exc)
            _safe_emit("step", "⚠️ Deepagents failed, using heuristic fallback")

    if not final_text:
        strategy = "heuristic_fallback" if use_deepagents else "heuristic"
        _safe_emit("step", "🔧 Running heuristic patent pipeline...")
        final_text = await _run_heuristic_patent(query, task_id, browser_enabled, input_data)

    # Build structured artifacts from heuristic builders (always, for review gate)
    disclosure = _build_disclosure(query)
    claim_tree = _build_claim_tree(disclosure)
    prior_art_items, matrix = _build_prior_art(disclosure, claim_tree)
    spec_draft = _build_spec(disclosure, claim_tree)

    # Build evidence collection and citation map from prior-art items
    evidence = EvidenceCollection(task_id=task_id)
    citations = CitationMap()
    for pa in prior_art_items:
        evidence.add(EvidenceItem(
            evidence_id=f"ev_{len(evidence.items) + 1}",
            evidence_type="url" if pa.source.startswith("http") else "quote",
            source=pa.source or pa.title,
            summary=pa.summary,
            metadata={"relation_to_claims": pa.relation_to_claims},
        ))
        if pa.source.startswith("http"):
            citations.add(pa.source, title=pa.title)
    if citations.all():
        final_text = final_text.rstrip() + "\n\n" + citations.render_markdown_references()

    _safe_emit("step", "📝 Persisting artifacts...")
    artifact_refs = _persist_artifacts(
        task_id=task_id,
        disclosure=disclosure,
        prior_art_items=prior_art_items,
        claim_tree=claim_tree,
        matrix=matrix,
        spec_draft=spec_draft,
        report=final_text,
    )

    # Persist evidence and citations
    task_dir = PATENT_DATA_ROOT / task_id
    if evidence.items:
        ev_path = task_dir / "evidence.json"
        ev_path.write_text(json.dumps(
            [{"evidence_id": e.evidence_id, "type": e.evidence_type, "source": e.source, "summary": e.summary}
             for e in evidence.items],
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
        artifact_refs.append({"type": "file", "name": "evidence.json", "path": str(ev_path)})
    if citations.all():
        cit_path = task_dir / "citations.json"
        cit_path.write_text(json.dumps(citations.to_dicts(), ensure_ascii=False, indent=2), encoding="utf-8")
        artifact_refs.append({"type": "file", "name": "citations.json", "path": str(cit_path)})

    _safe_emit("step", f"✅ Patent complete (strategy={strategy}), running review gate...")
    review = await PatentReviewGate().evaluate(
        {
            "key_terms": disclosure.key_terms,
            "claim_tree": claim_tree.model_dump(),
            "prior_art_matrix": matrix.model_dump(),
            "spec_draft": spec_draft.model_dump(),
        }
    )
    budget = BudgetPolicy().assess(estimated_cost=0.0, remaining_budget=input_data.get("remaining_budget"))

    risk_notes = []
    if not review.passed:
        risk_notes = [PatentRiskNote(severity=issue.severity, message=issue.message).model_dump() for issue in review.issues]

    return PatentDomainResult(
        status="ok",
        result=final_text,
        artifact_refs=artifact_refs,
        review={
            "passed": review.passed,
            "issues": [
                {"severity": issue.severity, "message": issue.message, "metadata": issue.metadata}
                for issue in review.issues
            ],
            "risk_notes": risk_notes,
        },
        budget={"action": budget.action, "reason": budget.reason},
    )
