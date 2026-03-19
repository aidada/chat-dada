from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from capabilities.memory import ResearchMemory
from tools.research_notes import (
    recall_research_notes,
    save_research_note,
    set_research_context,
)


class ResearchNotesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.mem = ResearchMemory("test_notes_001", root=self.root)
        self.mem.init("test query", "default")

    def tearDown(self) -> None:
        set_research_context(None, 0)
        self._tmp.cleanup()

    async def test_save_note_creates_file(self) -> None:
        set_research_context(self.mem, 3)
        result = await save_research_note.ainvoke({"topic": "GNSS精度", "content": "3m accuracy in open sky"})
        self.assertIn("笔记已保存", result)
        findings = self.mem.list_findings()
        self.assertTrue(len(findings) > 0)

    async def test_save_note_no_memory(self) -> None:
        set_research_context(None, 0)
        result = await save_research_note.ainvoke({"topic": "test", "content": "data"})
        self.assertIn("未初始化", result)

    async def test_recall_notes_returns_recent(self) -> None:
        set_research_context(self.mem, 1)
        # Save a few notes manually
        self.mem.save_finding(1, "note", "topic_a", "Content about A", [])
        self.mem.save_finding(2, "note", "topic_b", "Content about B", [])
        self.mem.save_finding(3, "note", "topic_c", "Content about C", [])

        result = await recall_research_notes.ainvoke({"topic": ""})
        self.assertIn("topic_c", result)
        self.assertIn("Content", result)

    async def test_recall_notes_filter_by_topic(self) -> None:
        set_research_context(self.mem, 1)
        self.mem.save_finding(1, "note", "GNSS", "GNSS related content", [])
        self.mem.save_finding(2, "note", "LiDAR", "LiDAR related content", [])

        result = await recall_research_notes.ainvoke({"topic": "GNSS"})
        self.assertIn("GNSS", result)
        self.assertNotIn("LiDAR", result)

    async def test_recall_notes_empty(self) -> None:
        set_research_context(self.mem, 1)
        result = await recall_research_notes.ainvoke({"topic": ""})
        self.assertIn("暂无", result)

    async def test_save_note_includes_evidence_strength(self) -> None:
        set_research_context(self.mem, 3)
        await save_research_note.ainvoke({
            "topic": "GNSS",
            "content": "Important finding",
            "evidence_strength": "strong",
        })
        findings = self.mem.list_findings()
        self.assertTrue(len(findings) > 0)
        text = findings[0].read_text(encoding="utf-8")
        self.assertIn("[evidence: strong]", text)

    async def test_recall_notes_sorted_by_evidence(self) -> None:
        set_research_context(self.mem, 1)
        # Save notes with different evidence strengths
        self.mem.save_finding(1, "note", "weak_finding", "[evidence: weak]\nWeak content", [])
        self.mem.save_finding(2, "note", "strong_finding", "[evidence: strong]\nStrong content", [])
        self.mem.save_finding(3, "note", "moderate_finding", "[evidence: moderate]\nModerate content", [])

        result = await recall_research_notes.ainvoke({"topic": ""})
        # Strong should appear before weak
        strong_pos = result.find("Strong content")
        weak_pos = result.find("Weak content")
        self.assertGreater(weak_pos, strong_pos, "Strong evidence should appear before weak")


if __name__ == "__main__":
    unittest.main()
