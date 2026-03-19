"""
Visio Renderer — converts diagram JSON (nodes/edges/type) to .drawio and .vsdx files.

Input format:
    {"diagram": {"type": "flowchart", "nodes": [...], "edges": [...]},
     "title": "...", "output_path": "outputs/my_diagram"}

Output: dual format — always produces .drawio, attempts .vsdx (graceful degradation).
"""
import json
import logging
import math
import os
import xml.etree.ElementTree as ET

log = logging.getLogger("chatdada.renderers.visio")

# Layout constants
NODE_W = 120
NODE_H = 60
H_GAP = 40
V_GAP = 60
MARGIN_X = 40
MARGIN_Y = 40

# draw.io style mapping
_SHAPE_STYLES = {
    "rect": "rounded=0;whiteSpace=wrap;html=1;",
    "rectangle": "rounded=0;whiteSpace=wrap;html=1;",
    "rounded": "rounded=1;whiteSpace=wrap;html=1;",
    "diamond": "rhombus;whiteSpace=wrap;html=1;",
    "circle": "ellipse;whiteSpace=wrap;html=1;",
    "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    "start": "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
    "end": "ellipse;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;",
    "process": "rounded=0;whiteSpace=wrap;html=1;",
    "decision": "rhombus;whiteSpace=wrap;html=1;",
    "parallelogram": "shape=parallelogram;whiteSpace=wrap;html=1;",
}

_DEFAULT_STYLE = "rounded=1;whiteSpace=wrap;html=1;"
_EDGE_STYLE = "edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;"


def run(input_data) -> dict:
    """
    Render diagram JSON to .drawio and .vsdx files.

    Args:
        input_data: dict with "diagram" (nodes/edges/type JSON),
                    optional "title" and "output_path"
    """
    if isinstance(input_data, str):
        try:
            input_data = json.loads(input_data)
        except json.JSONDecodeError:
            return {"status": "error", "result": "Cannot parse input as JSON"}

    if not isinstance(input_data, dict):
        return {"status": "error", "result": "Invalid input: expected dict"}

    diagram = input_data.get("diagram", input_data)
    title = input_data.get("title", "diagram")
    output_base = input_data.get("output_path", "outputs/diagram")

    # Strip any extension from output_base
    if output_base.endswith((".vsdx", ".drawio", ".json")):
        output_base = os.path.splitext(output_base)[0]

    os.makedirs(os.path.dirname(output_base) or ".", exist_ok=True)

    nodes = diagram.get("nodes", [])
    edges = diagram.get("edges", [])

    if not nodes:
        return {"status": "error", "result": "Diagram has no nodes"}

    files = []

    # Always generate .drawio
    drawio_path = f"{output_base}.drawio"
    try:
        _generate_drawio(nodes, edges, title, drawio_path)
        files.append(drawio_path)
    except Exception as e:
        log.error(f"Failed to generate .drawio: {e}")
        return {"status": "error", "result": f"Failed to generate .drawio: {e}"}

    # Attempt .vsdx generation (graceful degradation)
    vsdx_path = f"{output_base}.vsdx"
    try:
        _generate_vsdx(nodes, edges, title, vsdx_path)
        files.append(vsdx_path)
    except Exception as e:
        log.warning(f"Failed to generate .vsdx (continuing with .drawio only): {e}")

    fmt_list = " / ".join(os.path.splitext(f)[1] for f in files)
    return {
        "status": "ok",
        "result": f"图表已生成 ({fmt_list}): {', '.join(files)}",
        "files": files,
    }


# ---------------------------------------------------------------------------
# .drawio generation
# ---------------------------------------------------------------------------

def _grid_layout(nodes: list[dict]) -> dict[str, tuple[float, float]]:
    """Assign (x, y) positions to nodes in a grid layout."""
    n = len(nodes)
    cols = max(1, math.ceil(math.sqrt(n)))
    positions = {}
    for i, node in enumerate(nodes):
        nid = str(node.get("id", i))
        col = i % cols
        row = i // cols
        x = MARGIN_X + col * (NODE_W + H_GAP)
        y = MARGIN_Y + row * (NODE_H + V_GAP)
        positions[nid] = (x, y)
    return positions


def _generate_drawio(
    nodes: list[dict], edges: list[dict], title: str, path: str
) -> None:
    mxfile = ET.Element("mxfile")
    diagram = ET.SubElement(mxfile, "diagram", name=title)
    model = ET.SubElement(diagram, "mxGraphModel")
    root = ET.SubElement(model, "root")

    # Required root cells
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")

    positions = _grid_layout(nodes)

    # Nodes
    for node in nodes:
        nid = str(node.get("id", ""))
        label = node.get("label", node.get("text", nid))
        shape = node.get("shape", node.get("type", "rect"))
        style = _SHAPE_STYLES.get(shape, _DEFAULT_STYLE)

        x, y = positions.get(nid, (MARGIN_X, MARGIN_Y))
        cell = ET.SubElement(
            root, "mxCell",
            id=f"n{nid}", value=label, style=style,
            vertex="1", parent="1",
        )
        ET.SubElement(
            cell, "mxGeometry",
            x=str(x), y=str(y),
            width=str(NODE_W), height=str(NODE_H),
            **{"as": "geometry"},
        )

    # Edges
    for i, edge in enumerate(edges):
        src = str(edge.get("source", edge.get("from", "")))
        tgt = str(edge.get("target", edge.get("to", "")))
        label = edge.get("label", edge.get("text", ""))
        cell = ET.SubElement(
            root, "mxCell",
            id=f"e{i}", value=label, style=_EDGE_STYLE,
            edge="1", source=f"n{src}", target=f"n{tgt}", parent="1",
        )
        ET.SubElement(cell, "mxGeometry", relative="1", **{"as": "geometry"})

    tree = ET.ElementTree(mxfile)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------------------------
# .vsdx generation
# ---------------------------------------------------------------------------

def _generate_vsdx(
    nodes: list[dict], edges: list[dict], title: str, path: str
) -> None:
    from vsdx import VisioFile

    pkg_dir = os.path.dirname(__import__("vsdx").__file__)
    template_path = os.path.join(pkg_dir, "media", "media.vsdx")

    with VisioFile(template_path) as vis:
        # Add a fresh page for our diagram
        page = vis.add_page()
        page.name = title

        # Get template shapes from template page for copying
        template_page = vis.pages[0]
        rect_template = template_page.find_shape_by_text("RECTANGLE")
        circle_template = template_page.find_shape_by_text("CIRCLE")
        connector_template = template_page.find_shape_by_text("STRAIGHT_CONNECTOR")

        positions = _grid_layout(nodes)
        shape_map: dict[str, object] = {}

        def to_inches(px: float) -> float:
            return px / 96.0

        shape_type_map = {
            "circle": circle_template,
            "ellipse": circle_template,
            "start": circle_template,
            "end": circle_template,
        }

        for node in nodes:
            nid = str(node.get("id", ""))
            label = node.get("label", node.get("text", nid))
            node_shape = node.get("shape", node.get("type", "rect"))
            px, py = positions.get(nid, (MARGIN_X, MARGIN_Y))

            # Pick template shape
            src_shape = shape_type_map.get(node_shape, rect_template)
            if not src_shape:
                continue

            new_shape = src_shape.copy(page=page)
            new_shape.text = label

            # Position: Visio y-axis is bottom-up
            x_in = to_inches(px + NODE_W / 2)
            y_in = 11.0 - to_inches(py + NODE_H / 2)
            new_shape.x = x_in
            new_shape.y = y_in
            new_shape.width = to_inches(NODE_W)
            new_shape.height = to_inches(NODE_H)

            shape_map[nid] = new_shape

        for edge in edges:
            src = str(edge.get("source", edge.get("from", "")))
            tgt = str(edge.get("target", edge.get("to", "")))
            src_s = shape_map.get(src)
            tgt_s = shape_map.get(tgt)
            if src_s and tgt_s and connector_template:
                try:
                    conn = connector_template.copy(page=page)
                    conn.text = edge.get("label", edge.get("text", ""))
                    page.add_connect(src_s, conn, "BeginX")
                    page.add_connect(tgt_s, conn, "EndX")
                except Exception as e:
                    log.debug(f"Connector {src}->{tgt} failed: {e}")

        # Remove template page
        vis.remove_page_by_index(0)

        vis.save_vsdx(path)
