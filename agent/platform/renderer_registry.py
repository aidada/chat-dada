from __future__ import annotations

from typing import Any


class RendererRegistry:
    def __init__(self) -> None:
        self._renderers: dict[str, Any] = {}

    def register(self, artifact_type: str, renderer: Any) -> None:
        self._renderers[artifact_type] = renderer

    def get(self, artifact_type: str) -> Any:
        return self._renderers.get(artifact_type)

    def summary(self) -> str:
        return "\n".join(f"- {name}" for name in sorted(self._renderers))


registry = RendererRegistry()

from agent.domains.patent.renderers import render_patent_markdown, render_patent_pptx  # noqa: E402
from agent.domains.research.renderers import render_markdown, render_pptx  # noqa: E402
from agent.domains.zero_report.renderers import render_zero_report_markdown, render_zero_report_pptx  # noqa: E402

registry.register("research_markdown", render_markdown)
registry.register("research_pptx", render_pptx)
registry.register("patent_markdown", render_patent_markdown)
registry.register("patent_pptx", render_patent_pptx)
registry.register("zero_report_markdown", render_zero_report_markdown)
registry.register("zero_report_pptx", render_zero_report_pptx)
