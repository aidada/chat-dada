"""PPT compatibility wrapper around the Office domain."""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from agent.domains.office.orchestrated import run_office_domain_orchestrated

_log = logging.getLogger("chatdada.ppt.orchestrated")


class PptDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


def _map_ppt_artifacts(artifact_refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for ref in artifact_refs:
        item = dict(ref)
        if str(item.get("format", "") or "").lower() == "pptx":
            item["type"] = "pptx"
        mapped.append(item)
    return mapped


async def run_ppt_domain_orchestrated(
    input_data: dict[str, Any],
) -> PptDomainResult:
    office_input = dict(input_data)
    office_input.setdefault("format_hint", "pptx")

    result = await run_office_domain_orchestrated(office_input)
    return PptDomainResult(
        status=result.status,
        result=result.result,
        artifact_refs=_map_ppt_artifacts(result.artifact_refs),
        review=result.review,
        budget=result.budget,
    )
