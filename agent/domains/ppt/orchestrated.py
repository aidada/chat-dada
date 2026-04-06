"""
PPT domain agent — OfficeCLI version.

The agent receives the officecli SKILL.md as system context and uses
officecli_run / officecli_batch tools to create .pptx files directly.
Research tools (web_search, etc.) are also available for content gathering.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

from pydantic import BaseModel

from agent.capabilities.review_gates import ReviewGate
from agent.domains.ppt.tools import get_ppt_tools
from agent.platform.streaming import stream_nested_graph
from agent.workflows.orchestrator import build_orchestrated_graph, DomainSpec

_log = logging.getLogger("chatdada.ppt.orchestrated")


# ── SubagentConfig (defined locally, PRD §8.3 C3) ───────────────────────────────


@dataclass
class SubagentConfig:
    """Configuration for a deepagents subagent."""

    name: str
    description: str
    system_prompt: str
    tools: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "system_prompt": self.system_prompt,
            "tools": self.tools,
        }

# ── Load OfficeCLI SKILL.md (once at import time) ───────────────────────────

_SKILL_PATH = Path(__file__).resolve().parents[3] / "skills" / "officecli" / "SKILL.md"

_OFFICECLI_SKILL: str = ""
if _SKILL_PATH.exists():
    _OFFICECLI_SKILL = _SKILL_PATH.read_text(encoding="utf-8")
    _log.info("Loaded OfficeCLI SKILL.md (%d chars)", len(_OFFICECLI_SKILL))
else:
    _log.warning("OfficeCLI SKILL.md not found at %s", _SKILL_PATH)


# ── Shared result model ─────────────────────────────────────────────────────

class PptDomainResult(BaseModel):
    status: str
    result: str
    artifact_refs: list[dict[str, Any]]
    review: dict[str, Any]
    budget: dict[str, Any]


# ── System prompt ────────────────────────────────────────────────────────────

_PPT_SYSTEM = """\
你是 PPT 生成专家。你可以用 officecli 工具直接创建和编辑 PowerPoint 文件。

## 工作流程

1. **搜索素材**：先用 web_search / academic_search 等工具搜索相关素材和数据
2. **规划大纲**：确定 PPT 结构（封面、各章节、总结）
3. **创建 PPT**：用 officecli_run("create <filename>.pptx") 创建空文件
4. **逐步构建**：用 officecli_run 或 officecli_batch 添加幻灯片和内容
5. **验证检查**：用 officecli_run("validate <filename>.pptx") 检查文件质量
6. **修复问题**：如果 validate 或 view issues 发现问题，用 set/remove 修复

## 关键规则

- 文件名只用英文字母/数字/下划线，例如 "report_q4.pptx"
- 所有文件操作自动在 outputs/ 目录下进行，只传文件名
- 不确定属性名时，先运行 officecli_run("pptx set shape") 查询帮助
- 每页正文控制在 50-80 字，要点化表达
- 使用中文内容
- 完成后必须运行 validate 确认文件有效

## 输出要求

完成 PPT 创建后，你的最终回复必须包含以下 JSON（用 ```json 包裹）：
```json
{{"filename": "<文件名>.pptx", "title": "<PPT标题>", "slide_count": <页数>}}
```

## OfficeCLI 参考手册

{skill_content}
"""


# ── DomainSpec ───────────────────────────────────────────────────────────────

PPT_SPEC = DomainSpec(
    name="ppt",
    model_role="orchestrator",
    system_prompt=_PPT_SYSTEM.format(skill_content=_OFFICECLI_SKILL),
    tools=get_ppt_tools(),
    subagents=[
        SubagentConfig(
            name="content_researcher",
            description="Search for relevant data, statistics, and materials for PPT slides.",
            system_prompt="搜索与 PPT 主题相关的数据、案例和素材。输出结构化的要点和来源。",
            tools=[t for t in get_ppt_tools() if t.name not in ("officecli_run", "officecli_batch")],
        ),
    ],
    evaluator=ReviewGate(),
    strategy_hints=["sequential"],
    max_steps=15,  # More steps needed: agent iterates create→add→validate→fix
    max_cost=3.0,
)

_graph = build_orchestrated_graph(PPT_SPEC)


# ── Entry point ──────────────────────────────────────────────────────────────

def _safe_emit(event_type: str, content: str | dict[str, Any]) -> None:
    try:
        from langgraph.config import get_stream_writer
        payload = dict(content) if isinstance(content, dict) else {"content": content}
        payload.setdefault("event_type", event_type)
        get_stream_writer()(payload)
    except Exception:
        pass


def _extract_result_json(text: str) -> dict | None:
    """Extract the {filename, title, slide_count} JSON from agent's final response."""
    if "```json" in text:
        try:
            json_str = text.split("```json")[1].split("```")[0].strip()
            return json.loads(json_str)
        except (json.JSONDecodeError, IndexError):
            pass
    # Fallback: search for JSON object with "filename" key
    import re
    m = re.search(r'\{[^}]*"filename"[^}]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    return None


async def run_ppt_domain_orchestrated(
    input_data: dict[str, Any],
) -> PptDomainResult:
    """Run PPT domain: agent uses officecli tools to create .pptx directly."""
    query = input_data.get("query") or input_data.get("task", "")
    task_id = input_data.get("task_id", "ppt_unknown")

    _log.info("Starting OfficeCLI PPT: query=%s task_id=%s", str(query)[:60], task_id)
    _safe_emit("step", "🎨 PPT 生成中（OfficeCLI）...")

    result = await stream_nested_graph(
        _graph,
        {
            "goal": str(query),
            "task_id": str(task_id),
            "report_profile": "",
            "cost": 0.0,
            "progress": 0.0,
            "confidence": 0.0,
            "max_cost": PPT_SPEC.max_cost,
            "max_steps": PPT_SPEC.max_steps,
            "intermediate_results": [],
            "evaluations": [],
            "step_history": [],
            "coverage": {},
        },
        config={"configurable": {"thread_id": str(task_id)}},
        extra_payload={
            "nested_graph": "ppt_orchestrated_graph",
            "domain_name": "ppt",
            "source": "domain_orchestrated_wrapper",
        },
    )

    content_text = result.get("final_result", "")
    strategy_trace = result.get("step_history", [])
    strategies_used = [s.get("strategy", "") for s in strategy_trace]

    if not content_text:
        return PptDomainResult(
            status="error",
            result="PPT 生成失败：agent 未返回结果。",
            artifact_refs=[],
            review={"passed": False, "reason": "No content generated"},
            budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
        )

    # Extract filename from agent's structured JSON output
    result_meta = _extract_result_json(content_text)
    outputs_dir = Path(os.getenv("OFFICECLI_ALLOWED_DIR", "outputs")).resolve()

    if result_meta and result_meta.get("filename"):
        filename = Path(result_meta["filename"]).name  # prevent traversal
        title = result_meta.get("title", str(query)[:30])
        slide_count = result_meta.get("slide_count", "?")
        output_path = outputs_dir / filename

        if output_path.exists():
            _safe_emit("step", f"✅ PPT 已生成: {filename}")
            _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))

            result_text = f"PPT 已生成：《{title}》，共 {slide_count} 页。\n下载: /download/{filename}"
            return PptDomainResult(
                status="ok",
                result=result_text,
                artifact_refs=[{"name": filename, "type": "pptx", "url": f"/download/{filename}"}],
                review={"passed": True, "reason": "PPT created via OfficeCLI"},
                budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
            )

    # Fallback: look for any .pptx file created during this run
    pptx_files = sorted(outputs_dir.glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if pptx_files:
        latest = pptx_files[0]
        filename = latest.name
        _safe_emit("step", f"✅ PPT 已生成: {filename}")
        _safe_emit("file", json.dumps({"type": "file", "url": f"/download/{filename}", "name": filename}))

        return PptDomainResult(
            status="ok",
            result=f"PPT 已生成：{filename}\n下载: /download/{filename}\n\n{content_text}",
            artifact_refs=[{"name": filename, "type": "pptx", "url": f"/download/{filename}"}],
            review={"passed": True, "reason": "PPT file found in outputs"},
            budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
        )

    # No file produced — return raw content
    _log.warning("Agent produced text but no .pptx file was found")
    return PptDomainResult(
        status="ok",
        result=content_text,
        artifact_refs=[],
        review={"passed": False, "reason": "No .pptx file produced"},
        budget={"action": "allow", "reason": f"orchestrated({' → '.join(strategies_used)})"},
    )
