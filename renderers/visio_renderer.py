"""
Visio Renderer — converts diagram JSON (nodes/edges/type) to Visio format.

Placeholder implementation: saves diagram JSON as a .json file.
TODO: implement actual .vsdx generation via python-vsdx when ready.
"""
import json
import os


def run(input_data) -> dict:
    """
    Render diagram JSON to Visio format (placeholder, outputs JSON).

    Args:
        input_data: dict with "diagram" (nodes/edges/type JSON),
                    optional "title" and "output_path"
    """
    if isinstance(input_data, str):
        try:
            diagram = json.loads(input_data)
        except json.JSONDecodeError:
            diagram = {"raw": input_data}
        title = "diagram"
        output_path = "outputs/diagram.json"
    elif isinstance(input_data, dict):
        diagram = input_data.get("diagram", input_data)
        title = input_data.get("title", "diagram")
        output_path = input_data.get("output_path", "outputs/diagram.json")
    else:
        return {"status": "error", "result": "Invalid input"}

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(diagram, f, ensure_ascii=False, indent=2)

    return {
        "status": "ok",
        "result": f"Diagram JSON saved: {output_path} (Visio rendering not yet implemented)",
        "files": [output_path],
    }
