from __future__ import annotations

from domain_agents.patent.schemas import ClaimTree, PriorArtMatrix, SpecDraft, TechnicalDisclosure


def render_patent_markdown(
    disclosure: TechnicalDisclosure,
    claim_tree: ClaimTree,
    matrix: PriorArtMatrix,
    spec_draft: SpecDraft,
) -> str:
    claim_lines = [f"- {claim.claim_id}: {claim.text}" for claim in claim_tree.claims]
    matrix_lines = [
        f"- {row.claim_id} <- {row.prior_art_title}: {row.coverage_note}"
        for row in matrix.rows
    ]
    embodiment_lines = [f"- {item}" for item in spec_draft.embodiments]
    return "\n".join(
        [
            f"# {disclosure.title or 'Patent Draft'}",
            "",
            "## 技术交底摘要",
            disclosure.summary,
            "",
            "## 权利要求树",
            *claim_lines,
            "",
            "## Prior Art Matrix",
            *matrix_lines,
            "",
            "## 说明书草稿",
            spec_draft.background,
            "",
            "### 发明内容",
            spec_draft.summary,
            "",
            "### 实施例",
            *embodiment_lines,
        ]
    ).strip()


def render_patent_pptx(
    disclosure: TechnicalDisclosure,
    claim_tree: ClaimTree,
    matrix: PriorArtMatrix,
    spec_draft: SpecDraft,
    output_path: str,
) -> str:
    """Render a patent draft as .pptx via the shared PPT capability."""
    from capabilities.ppt_capability import markdown_to_deck, render_deck_to_pptx

    md = render_patent_markdown(disclosure, claim_tree, matrix, spec_draft)
    deck = markdown_to_deck(disclosure.title or "Patent Draft", md)
    return render_deck_to_pptx(deck, output_path)

