from __future__ import annotations

from pathlib import Path

from agent.tools.officecli import get_officecli_skill_bundle
from agent.tools.officecli_skill_loader import (
    build_officecli_skill_bundle,
    select_officecli_skill_paths,
)


def test_select_officecli_skill_paths_for_general_ppt() -> None:
    paths = select_officecli_skill_paths("做一个介绍 AI 的 PPT", format_hint="pptx")
    names = [path.parent.name if path.name == "SKILL.md" and path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-pptx", "officecli-presentation-quality"]


def test_select_officecli_skill_paths_for_pitch_deck() -> None:
    paths = select_officecli_skill_paths("做一个 investor pitch deck", format_hint="pptx")
    names = [path.parent.name if path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-pptx", "officecli-presentation-quality", "officecli-pitch-deck"]


def test_select_officecli_skill_paths_for_morph_3d() -> None:
    paths = select_officecli_skill_paths("制作一个 3D morph PPT，附带 glb 模型", format_hint="pptx")
    names = [path.parent.name if path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-pptx", "officecli-presentation-quality", "morph-ppt-3d"]


def test_select_officecli_skill_paths_for_existing_ppt_edit_skips_quality_skill() -> None:
    paths = select_officecli_skill_paths(
        "修改 existing deck.pptx 的标题样式",
        file_hint="existing deck.pptx",
        format_hint="pptx",
        operation_hint="edit",
    )
    names = [path.parent.name if path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-pptx"]


def test_select_officecli_skill_paths_for_docx_academic() -> None:
    paths = select_officecli_skill_paths("写 academic paper", format_hint="docx")
    names = [path.parent.name if path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-docx", "officecli-academic-paper"]


def test_select_officecli_skill_paths_for_xlsx_dashboard() -> None:
    paths = select_officecli_skill_paths("做 KPI dashboard", format_hint="xlsx")
    names = [path.parent.name if path.parent.name != "officecli" else "base" for path in paths]
    assert names == ["base", "officecli-xlsx", "officecli-data-dashboard"]


def test_select_officecli_skill_paths_without_format_uses_only_base() -> None:
    paths = select_officecli_skill_paths("普通 office 文档操作")
    assert paths == [Path(__file__).resolve().parents[1] / "skills" / "officecli" / "SKILL.md"]


def test_build_officecli_skill_bundle_skips_missing_child_skill() -> None:
    bundle = build_officecli_skill_bundle("做一个 investor pitch deck", format_hint="pptx")
    assert "officecli-pitch-deck" in bundle
    assert "officecli-presentation-quality" in bundle


def test_get_officecli_skill_bundle_docx() -> None:
    bundle = get_officecli_skill_bundle("写 academic paper", format_hint="docx")
    assert "officecli-docx" in bundle
    assert "officecli-academic-paper" in bundle
