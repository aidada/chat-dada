"""PPT Capability — shared storyline planning + DSL generation + rendering.

This is the cross-domain PPT output pipeline. Research findings, patent
disclosures, and zero-report summaries can all be rendered as .pptx via
this capability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger("chatdada.ppt_capability")


@dataclass(frozen=True)
class PPTPlan:
    title: str
    storyline: str
    author: str = ""


@dataclass
class SlideContent:
    heading: str
    body: str
    notes: str = ""


@dataclass
class PPTDeck:
    title: str
    author: str = ""
    slides: list[SlideContent] = field(default_factory=list)


def build_ppt_plan(title: str, storyline: str, *, author: str = "") -> PPTPlan:
    return PPTPlan(title=title, storyline=storyline, author=author)


def plan_to_deck(plan: PPTPlan, sections: list[dict[str, str]]) -> PPTDeck:
    """Convert a PPT plan and section content into a renderable deck."""
    slides = []
    for section in sections:
        slides.append(
            SlideContent(
                heading=section.get("heading", ""),
                body=section.get("body", ""),
                notes=section.get("notes", ""),
            )
        )
    return PPTDeck(title=plan.title, author=plan.author, slides=slides)


def markdown_to_deck(title: str, markdown_report: str, *, author: str = "") -> PPTDeck:
    """Convert a markdown report into a PPTDeck by splitting on ## headings."""
    slides: list[SlideContent] = []
    current_heading = title
    current_body_lines: list[str] = []

    for line in markdown_report.splitlines():
        if line.startswith("## "):
            if current_body_lines:
                slides.append(
                    SlideContent(
                        heading=current_heading,
                        body="\n".join(current_body_lines).strip(),
                    )
                )
            current_heading = line.lstrip("# ").strip()
            current_body_lines = []
        elif line.startswith("# ") and not slides:
            current_heading = line.lstrip("# ").strip()
        else:
            current_body_lines.append(line)

    if current_body_lines:
        slides.append(
            SlideContent(
                heading=current_heading,
                body="\n".join(current_body_lines).strip(),
            )
        )

    return PPTDeck(title=title, author=author, slides=slides)


def render_deck_to_pptx(deck: PPTDeck, output_path: str) -> str:
    """Render a PPTDeck to .pptx file via OfficeCLI batch commands."""
    import asyncio
    import json as _json

    from agent.tools.officecli import run as officecli_run, run_batch, ALLOWED_DIR

    filename = Path(output_path).name

    async def _render() -> None:
        # Create the file
        result = await officecli_run(f"create {filename}")
        if result["status"] == "error":
            raise RuntimeError(f"officecli create failed: {result['result']}")

        # Build batch commands
        commands: list[dict[str, Any]] = []
        for i, sc in enumerate(deck.slides, 1):
            slide_props: dict[str, str] = {}
            if sc.heading:
                slide_props["title"] = sc.heading
            commands.append({"command": "add", "parent": "/", "type": "slide", "props": slide_props})

            if sc.body:
                commands.append(
                    {
                        "command": "add",
                        "parent": f"/slide[{i}]",
                        "type": "shape",
                        "props": {"text": sc.body, "x": "2cm", "y": "4cm", "w": "30cm", "h": "12cm", "size": "18"},
                    }
                )

        if commands:
            result = await run_batch({"file": filename, "commands": commands})
            if result["status"] == "error":
                raise RuntimeError(f"officecli batch failed: {result['result']}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already in async context — create task
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            pool.submit(lambda: asyncio.run(_render())).result()
    else:
        asyncio.run(_render())

    # Ensure file is at expected output_path
    generated = ALLOWED_DIR / filename
    target = Path(output_path)
    if generated.resolve() != target.resolve() and generated.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copy2(str(generated), str(target))

    return output_path
