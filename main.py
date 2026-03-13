"""
Local Agent - FastAPI + Multi-Agent + WebSocket 实时推送
运行: uvicorn main:app --reload --port 8000
"""
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from agents.orchestrator import run_agent

app = FastAPI(title="Local Agent")


@app.get("/")
async def index():
    """返回前端页面"""
    html = Path("static/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/download/{filename}")
async def download_file(filename: str):
    """下载生成的文件（PPT 等）"""
    # Sanitize filename to prevent path traversal
    safe_name = Path(filename).name
    path = Path("outputs") / safe_name
    if not path.exists():
        return {"error": "文件不存在"}
    return FileResponse(path, filename=safe_name)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    WebSocket 端点：接收任务 → 执行 Agent → 实时推送步骤
    消息格式:
      Client → Server: {"task": "帮我搜索..."}
      Server → Client: {"type": "step"|"result"|"error"|"file", "content": "..."}
    """
    await websocket.accept()
    print("[WS] 客户端连接")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            task = data.get("task", "").strip()

            if not task:
                await websocket.send_json({"type": "error", "content": "任务不能为空"})
                continue

            print(f"[Task] {task}")
            await websocket.send_json({"type": "start", "content": f"开始执行: {task}"})

            async def on_step(step_info: str):
                # Check if step_info is a JSON file message
                try:
                    parsed = json.loads(step_info)
                    if isinstance(parsed, dict) and parsed.get("type") == "file":
                        await websocket.send_json(parsed)
                        return
                except (json.JSONDecodeError, TypeError):
                    pass
                await websocket.send_json({"type": "step", "content": step_info})

            try:
                result = await run_agent(task, on_step)
                await websocket.send_json({"type": "result", "content": result})
            except Exception as e:
                await websocket.send_json({"type": "error", "content": str(e)})

    except WebSocketDisconnect:
        print("[WS] 客户端断开")


# Mount static files LAST (after API routes)
app.mount("/static", StaticFiles(directory="static"), name="static")
