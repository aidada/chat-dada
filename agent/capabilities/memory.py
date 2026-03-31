"""
File-system external memory for deep_research agent.

Storage layout:
  data/research/{task_id}/meta.json
  data/research/{task_id}/findings/step_01_web_search.md
  data/research/{task_id}/summaries/step_05.md
  data/research/{task_id}/summaries/latest.md
  data/research/{task_id}/checkpoints/step_05.json
  data/research/{task_id}/final_report.md
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("chatdada.research_memory")

DEFAULT_RESEARCH_ROOT = Path(os.environ.get("RESEARCH_DATA_DIR", "data/research"))
CHECKPOINT_VERSION = 1


def _sanitize_tool_name(name: str) -> str:
    """Make tool name safe for file paths: replace unsafe chars, truncate."""
    safe = re.sub(r"[/\\:\s]+", "_", name.strip())
    safe = re.sub(r"[^\w.-]", "", safe)
    return safe[:40] or "unknown"


class ResearchMemory:
    """Persist research artefacts to the file system."""

    def __init__(self, task_id: str, root: Path = DEFAULT_RESEARCH_ROOT) -> None:
        self.task_id = task_id
        self.root = root
        self.task_dir = self.root / task_id

    # -- lifecycle ----------------------------------------------------------

    def init(self, query: str, report_profile: str) -> None:
        """Create directory structure and write meta.json."""
        for subdir in ("findings", "summaries", "checkpoints"):
            (self.task_dir / subdir).mkdir(parents=True, exist_ok=True)

        meta = {
            "task_id": self.task_id,
            "query": query,
            "report_profile": report_profile,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path = self.task_dir / "meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_meta(self) -> dict[str, Any] | None:
        """Load meta.json for this task, or None if missing."""
        meta_path = self.task_dir / "meta.json"
        if not meta_path.exists():
            return None
        return json.loads(meta_path.read_text(encoding="utf-8"))

    # -- findings -----------------------------------------------------------

    def save_finding(self, step: int, tool_name: str, query: str, content: str, urls: list[str]) -> Path:
        safe_name = _sanitize_tool_name(tool_name)
        filename = f"step_{step:02d}_{safe_name}.md"
        path = self.task_dir / "findings" / filename

        header = f"# Step {step} — {tool_name}\n\n"
        if query:
            header += f"Query: {query}\n\n"
        if urls:
            header += "Sources:\n" + "\n".join(f"- {u}" for u in urls) + "\n\n"
        header += "---\n\n"

        path.write_text(header + content, encoding="utf-8")
        return path

    def load_finding(self, step: int, tool_name: str) -> str | None:
        safe_name = _sanitize_tool_name(tool_name)
        filename = f"step_{step:02d}_{safe_name}.md"
        path = self.task_dir / "findings" / filename
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def list_findings(self) -> list[Path]:
        findings_dir = self.task_dir / "findings"
        if not findings_dir.exists():
            return []
        return sorted(findings_dir.glob("step_*.md"))

    # -- summaries ----------------------------------------------------------

    def save_summary(self, step: int, summary: str) -> None:
        summaries_dir = self.task_dir / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)

        step_file = summaries_dir / f"step_{step:02d}.md"
        step_file.write_text(summary, encoding="utf-8")

        latest_file = summaries_dir / "latest.md"
        latest_file.write_text(summary, encoding="utf-8")

    def load_latest_summary(self) -> str | None:
        path = self.task_dir / "summaries" / "latest.md"
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    # -- checkpoints --------------------------------------------------------

    def save_checkpoint(self, step: int, state_dict: dict[str, Any]) -> Path:
        checkpoints_dir = self.task_dir / "checkpoints"
        checkpoints_dir.mkdir(parents=True, exist_ok=True)
        state_dict.setdefault("_checkpoint_version", CHECKPOINT_VERSION)
        path = checkpoints_dir / f"step_{step:02d}.json"
        path.write_text(json.dumps(state_dict, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_checkpoint(self, step: int | None = None) -> dict[str, Any] | None:
        checkpoints_dir = self.task_dir / "checkpoints"
        if not checkpoints_dir.exists():
            return None

        if step is not None:
            path = checkpoints_dir / f"step_{step:02d}.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            # Load latest checkpoint
            files = sorted(checkpoints_dir.glob("step_*.json"))
            if not files:
                return None
            data = json.loads(files[-1].read_text(encoding="utf-8"))

        version = data.get("_checkpoint_version", 0)
        if version != CHECKPOINT_VERSION:
            log.warning("Checkpoint version mismatch: expected %d, got %d", CHECKPOINT_VERSION, version)
        return data

    # -- final report -------------------------------------------------------

    def save_final_report(self, report: str) -> Path:
        path = self.task_dir / "final_report.md"
        path.write_text(report, encoding="utf-8")
        return path

    # -- cleanup ------------------------------------------------------------

    @classmethod
    def list_tasks(cls, root: Path = DEFAULT_RESEARCH_ROOT) -> list[dict]:
        """列出所有研究任务及其元数据。"""
        tasks: list[dict] = []
        if not root.exists():
            return tasks
        for task_dir in sorted(root.iterdir()):
            if not task_dir.is_dir():
                continue
            meta_path = task_dir / "meta.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    tasks.append(meta)
                except Exception:
                    tasks.append({"task_id": task_dir.name})
            else:
                tasks.append({"task_id": task_dir.name})
        return tasks

    def cleanup(self, keep_final_report: bool = True) -> None:
        """删除 findings 和 checkpoints，可选保留 final_report。"""
        import shutil
        for subdir in ("findings", "summaries", "checkpoints"):
            d = self.task_dir / subdir
            if d.exists():
                shutil.rmtree(d)
        if not keep_final_report:
            report = self.task_dir / "final_report.md"
            if report.exists():
                report.unlink()

    @classmethod
    def cleanup_old_tasks(cls, max_age_days: int = 30, root: Path = DEFAULT_RESEARCH_ROOT) -> int:
        """清理超过 max_age_days 天的研究任务。返回清理数量。"""
        import shutil
        from datetime import timedelta
        if not root.exists():
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        count = 0
        for task_dir in list(root.iterdir()):
            if not task_dir.is_dir():
                continue
            meta_path = task_dir / "meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(meta.get("created_at", ""))
                if created < cutoff:
                    shutil.rmtree(task_dir)
                    count += 1
            except Exception:
                continue
        return count
