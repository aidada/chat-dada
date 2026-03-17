from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docx import Document

from renderers.word_renderer import run


class WordRendererTests(unittest.TestCase):
    def test_run_rejects_empty_content(self) -> None:
        result = run({"title": "Empty"})
        self.assertEqual(result["status"], "error")
        self.assertIn("No content provided", result["result"])

    def test_run_writes_title_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.docx"
            result = run(
                {
                    "title": "测试报告",
                    "content": "# 摘要\n- 第一条\n- 第二条\n结论段落",
                    "output_path": str(output_path),
                }
            )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(output_path.exists())

            doc = Document(str(output_path))
            texts = [paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip()]
            self.assertIn("测试报告", texts)
            self.assertIn("摘要", texts)
            self.assertIn("第一条", texts)
            self.assertIn("结论段落", texts)
