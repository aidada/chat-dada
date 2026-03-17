"""
Markdown Renderer — saves markdown text to a .md file.
"""
import os


def run(input_data) -> dict:
    """
    Save markdown-like text to a .md file.

    Args:
        input_data: dict with "content" (markdown text), "title", "output_path"
    """
    if isinstance(input_data, str):
        content = input_data
        title = "Document"
        output_path = "outputs/document.md"
    elif isinstance(input_data, dict):
        content = input_data.get("content", input_data.get("text", ""))
        title = str(input_data.get("title", "Document")).strip()
        output_path = input_data.get("output_path", "outputs/document.md")
    else:
        return {"status": "error", "result": "Invalid input"}

    content = str(content or "").strip()
    if not content:
        return {
            "status": "error",
            "result": "No content provided for Markdown rendering",
            "files": [],
        }

    if title and not content.startswith("# "):
        content = f"# {title}\n\n{content}"

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(content + "\n")

    return {
        "status": "ok",
        "result": f"Markdown file saved: {output_path}",
        "files": [output_path],
    }
