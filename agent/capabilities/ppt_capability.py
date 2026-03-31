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
        slides.append(SlideContent(
            heading=section.get("heading", ""),
            body=section.get("body", ""),
            notes=section.get("notes", ""),
        ))
    return PPTDeck(title=plan.title, author=plan.author, slides=slides)


def markdown_to_deck(title: str, markdown_report: str, *, author: str = "") -> PPTDeck:
    """Convert a markdown report into a PPTDeck by splitting on ## headings."""
    slides: list[SlideContent] = []
    current_heading = title
    current_body_lines: list[str] = []

    for line in markdown_report.splitlines():
        if line.startswith("## "):
            if current_body_lines:
                slides.append(SlideContent(
                    heading=current_heading,
                    body="\n".join(current_body_lines).strip(),
                ))
            current_heading = line.lstrip("# ").strip()
            current_body_lines = []
        elif line.startswith("# ") and not slides:
            current_heading = line.lstrip("# ").strip()
        else:
            current_body_lines.append(line)

    if current_body_lines:
        slides.append(SlideContent(
            heading=current_heading,
            body="\n".join(current_body_lines).strip(),
        ))

    return PPTDeck(title=title, author=author, slides=slides)


def render_deck_to_pptx(deck: PPTDeck, output_path: str) -> str:
    """Render a PPTDeck to .pptx file — fallback writes placeholder text."""
    try:
        from agent.ppt_engine.renderer import render_pptx
        from agent.ppt_engine.dsl_schema import SlideDeck as PptSlideDeck, DeckMeta, Slide

        ppt_slides = [
            Slide(layout="content_only", title=sc.heading, body=sc.body, speaker_notes=sc.notes or None)
            for sc in deck.slides
        ]
        ppt_deck = PptSlideDeck(
            meta=DeckMeta(title=deck.title, author=deck.author),
            slides=ppt_slides,
        )
        render_pptx(ppt_deck, output_path)
    except ImportError:
        _log.warning("ppt_engine not available, writing placeholder pptx")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(
            f"[PPT Placeholder] {deck.title}\n"
            + "\n".join(f"--- {s.heading} ---\n{s.body}" for s in deck.slides),
            encoding="utf-8",
        )
    except Exception as exc:
        _log.error("PPT rendering failed: %s", exc)
        raise
    return output_path
