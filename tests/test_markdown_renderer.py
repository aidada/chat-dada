from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from renderers.markdown_renderer import run


class MarkdownRendererTests(unittest.TestCase):
    def test_run_rejects_empty_content(self) -> None:
        result = run({"title": "Empty"})
        self.assertEqual(result["status"], "error")
        self.assertIn("No content provided", result["result"])

    def test_run_writes_markdown_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.md"
            result = run(
                {
                    "title": "测试报告",
                    "content": "## 摘要\n- 第一条\n- 第二条\n结论段落",
                    "output_path": str(output_path),
                }
            )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(output_path.exists())
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "# 测试报告\n\n## 摘要\n- 第一条\n- 第二条\n结论段落\n",
            )

    def test_run_preserves_existing_h1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.md"
            run(
                {
                    "title": "测试报告",
                    "content": "# 已有标题\n正文",
                    "output_path": str(output_path),
                }
            )

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "# 已有标题\n正文\n",
            )
