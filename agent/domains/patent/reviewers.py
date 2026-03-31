from __future__ import annotations

from agent.capabilities.review_gates import ReviewGate, ReviewIssue
from agent.domains.patent.schemas import ClaimTree, SpecDraft


class PatentReviewGate(ReviewGate):
    async def structural_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        claim_tree = ClaimTree.model_validate(payload.get("claim_tree") or {})
        spec = SpecDraft.model_validate(payload.get("spec_draft") or {})

        if not claim_tree.claims:
            issues.append(ReviewIssue(severity="error", message="缺少 ClaimTree"))
        claim_ids = {claim.claim_id for claim in claim_tree.claims}
        for claim in claim_tree.claims:
            missing = [dep for dep in claim.depends_on if dep not in claim_ids]
            if missing:
                issues.append(
                    ReviewIssue(
                        severity="error",
                        message=f"权利要求 {claim.claim_id} 依赖不存在的父项",
                        metadata={"missing_dependencies": missing},
                    )
                )
        if not spec.background.strip():
            issues.append(ReviewIssue(severity="error", message="说明书草稿缺少背景技术"))
        if not spec.embodiments:
            issues.append(ReviewIssue(severity="error", message="说明书草稿缺少实施例"))
        return issues

    async def semantic_checks(self, payload: dict[str, object]) -> list[ReviewIssue]:
        issues: list[ReviewIssue] = []
        disclosure_terms = {term.lower() for term in payload.get("key_terms", []) or [] if str(term).strip()}
        claim_text = " ".join(claim.get("text", "") for claim in (payload.get("claim_tree", {}) or {}).get("claims", []))
        if disclosure_terms and not any(term in claim_text.lower() for term in disclosure_terms):
            issues.append(ReviewIssue(severity="warning", message="权利要求未覆盖技术交底中的核心术语"))
        if not (payload.get("prior_art_matrix", {}) or {}).get("rows"):
            issues.append(ReviewIssue(severity="warning", message="PriorArtMatrix 为空，现有技术映射覆盖度不足"))
        return issues

