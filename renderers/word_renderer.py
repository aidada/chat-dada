"""
Word Renderer — converts markdown text to a .docx file via python-docx.
"""
import os
import re

from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH


def run(input_data) -> dict:
    """
    Render markdown-like text to a .docx file.

    Args:
        input_data: dict with "content" (markdown text), "title", "output_path"
    """
    if isinstance(input_data, str):
        content = input_data
        title = "Document"
        output_path = "outputs/document.docx"
    elif isinstance(input_data, dict):
        content = input_data.get("content", input_data.get("text", ""))
        title = input_data.get("title", "Document")
        output_path = input_data.get("output_path", "outputs/document.docx")
    else:
        return {"status": "error", "result": "Invalid input"}

    doc = Document()

    # Title
    doc.add_heading(title, level=0)

    # Parse markdown-like content
    for line in content.split("\n"):
        line = line.strip()
        if not line:
            continue
        elif line.startswith("### "):
            doc.add_heading(line[4:], level=3)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=2)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=1)
        elif line.startswith("- ") or line.startswith("* "):
            doc.add_paragraph(line[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s", line):
            text = re.sub(r"^\d+\.\s", "", line)
            doc.add_paragraph(text, style="List Number")
        else:
            doc.add_paragraph(line)

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    doc.save(output_path)

    return {
        "status": "ok",
        "result": f"Word document saved: {output_path}",
        "files": [output_path],
    }
