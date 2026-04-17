from __future__ import annotations

from pathlib import Path

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / "skills" / "officecli"
_BASE_SKILL = _SKILLS_ROOT / "SKILL.md"

_FORMAT_SKILLS = {
    "pptx": _SKILLS_ROOT / "officecli-pptx" / "SKILL.md",
    "docx": _SKILLS_ROOT / "officecli-docx" / "SKILL.md",
    "xlsx": _SKILLS_ROOT / "officecli-xlsx" / "SKILL.md",
}

_SCENARIO_SKILLS = {
    "officecli-presentation-quality": _SKILLS_ROOT / "officecli-presentation-quality" / "SKILL.md",
    "officecli-pitch-deck": _SKILLS_ROOT / "officecli-pitch-deck" / "SKILL.md",
    "officecli-academic-paper": _SKILLS_ROOT / "officecli-academic-paper" / "SKILL.md",
    "officecli-data-dashboard": _SKILLS_ROOT / "officecli-data-dashboard" / "SKILL.md",
    "officecli-financial-model": _SKILLS_ROOT / "officecli-financial-model" / "SKILL.md",
    "morph-ppt": _SKILLS_ROOT / "morph-ppt" / "SKILL.md",
    "morph-ppt-3d": _SKILLS_ROOT / "morph-ppt-3d" / "SKILL.md",
}

_FORMAT_KEYWORDS = {
    "pptx": ("ppt", "pptx", "slides", "slide deck", "deck", "presentation", "powerpoint", "演示文稿", "幻灯片"),
    "docx": ("docx", "word", "report", "letter", "memo", "manuscript", "报告", "信函"),
    "xlsx": ("xlsx", "excel", "spreadsheet", "workbook", "dashboard", "model", "csv", "表格", "电子表格", "工作簿", "仪表盘"),
}
_SCENARIO_KEYWORDS = {
    "officecli-pitch-deck": ("pitch deck", "investor", "fundraising", "融资", "路演", "sales deck", "startup pitch"),
    "officecli-academic-paper": ("academic paper", "research paper", "white paper", "policy brief", "论文", "白皮书", "技术报告", "脚注", "公式", "目录", "toc"),
    "officecli-data-dashboard": ("dashboard", "kpi", "metrics", "analytics", "仪表盘", "指标", "看板"),
    "officecli-financial-model": ("financial model", "dcf", "cap table", "3-statement", "projection", "财务模型", "三表", "估值"),
    "morph-ppt-3d": ("3d", "glb", "model animation", "3d model"),
    "morph-ppt": ("morph", "转场"),
}
_SCENARIO_PRIORITY = (
    "morph-ppt-3d",
    "morph-ppt",
    "officecli-academic-paper",
    "officecli-financial-model",
    "officecli-data-dashboard",
    "officecli-pitch-deck",
)
_SCENARIO_FORMAT_GATES = {
    "officecli-pitch-deck": {"pptx"},
    "morph-ppt": {"pptx"},
    "morph-ppt-3d": {"pptx"},
    "officecli-academic-paper": {"docx"},
    "officecli-data-dashboard": {"xlsx"},
    "officecli-financial-model": {"xlsx"},
}
_FALLBACK_SKILL_TEXT = (
    "OfficeCLI local skill guide is unavailable in this environment. "
    "Do not guess unofficial syntax. Prefer calling structured officecli verbs "
    "and use officecli help for command discovery before retrying."
)
_STRUCTURED_TOOL_REMINDER = (
    "Structured tool reminder:\n"
    "- `officecli(...)` and `officecli_batch(...)` always use the canonical `verb` field.\n"
    "- Some raw `officecli batch` CLI examples below may show JSON objects with `command`.\n"
    "- When converting those raw CLI examples into structured tool calls, rewrite `command` to `verb`."
)


def load_officecli_skill_text(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def select_officecli_skill_paths(
    goal: str,
    file_hint: str | None = None,
    format_hint: str | None = None,
    operation_hint: str | None = None,
) -> list[Path]:
    selected: list[Path] = [_BASE_SKILL]
    format_name = _infer_format_skill(goal, file_hint=file_hint, format_hint=format_hint)
    format_path = _FORMAT_SKILLS.get(format_name) if format_name else None
    if format_path is not None:
        selected.append(format_path)

    if _should_include_presentation_quality_skill(
        goal,
        file_hint=file_hint,
        format_name=format_name,
        operation_hint=operation_hint,
    ):
        quality_path = _SCENARIO_SKILLS["officecli-presentation-quality"]
        if quality_path not in selected:
            selected.append(quality_path)

    scenario_name = _infer_scenario_skill(goal, format_name)
    scenario_path = _SCENARIO_SKILLS.get(scenario_name) if scenario_name else None
    if scenario_path is not None and scenario_path not in selected:
        selected.append(scenario_path)
    return selected


def build_officecli_skill_bundle(
    goal: str,
    file_hint: str | None = None,
    format_hint: str | None = None,
    operation_hint: str | None = None,
) -> str:
    paths = select_officecli_skill_paths(
        goal,
        file_hint=file_hint,
        format_hint=format_hint,
        operation_hint=operation_hint,
    )
    base_text = load_officecli_skill_text(paths[0]) or _FALLBACK_SKILL_TEXT
    parts = [base_text]

    for path in paths[1:]:
        content = load_officecli_skill_text(path)
        if not content:
            continue
        parts.extend(
            [
                "\n\n---\n\n",
                f"# Extra skill: {path.parent.name}\n\n",
                _STRUCTURED_TOOL_REMINDER,
                "\n\n",
                content,
            ]
        )

    return "".join(parts)


def _infer_format_skill(goal: str, file_hint: str | None = None, format_hint: str | None = None) -> str | None:
    explicit = (format_hint or "").strip().lower()
    if explicit in _FORMAT_SKILLS:
        return explicit

    lowered_goal = str(goal or "").lower()
    lowered_file = str(file_hint or "").lower()
    for ext in _FORMAT_SKILLS:
        if lowered_file.endswith(f".{ext}"):
            return ext

    for format_name, keywords in _FORMAT_KEYWORDS.items():
        if any(keyword in lowered_goal for keyword in keywords):
            return format_name

    return None


def _infer_scenario_skill(goal: str, format_name: str | None) -> str | None:
    lowered_goal = str(goal or "").lower()
    for scenario_name in _SCENARIO_PRIORITY:
        gates = _SCENARIO_FORMAT_GATES.get(scenario_name, set())
        if format_name not in gates:
            continue
        if any(keyword in lowered_goal for keyword in _SCENARIO_KEYWORDS[scenario_name]):
            return scenario_name
    return None


def _should_include_presentation_quality_skill(
    goal: str,
    *,
    file_hint: str | None,
    format_name: str | None,
    operation_hint: str | None,
) -> bool:
    if format_name != "pptx":
        return False

    operation = str(operation_hint or "").strip().lower()
    if operation in {"edit", "inspect"}:
        return False
    if operation in {"create", "transform"}:
        return True

    lowered_goal = str(goal or "").lower()
    lowered_file = str(file_hint or "").lower()
    edit_keywords = (
        "修改",
        "编辑",
        "更新",
        "润色",
        "替换",
        "review",
        "inspect",
        "edit",
        "update",
        "fix",
    )
    create_keywords = (
        "创建",
        "生成",
        "制作",
        "做一个",
        "做一份",
        "写一个",
        "create",
        "generate",
        "make",
        "draft",
    )

    if lowered_file.endswith(".pptx") and any(keyword in lowered_goal for keyword in edit_keywords):
        return False
    if any(keyword in lowered_goal for keyword in create_keywords):
        return True
    return not lowered_file.endswith(".pptx")
