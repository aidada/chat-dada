from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from apps.web.runtime import OUTPUTS_DIR, UPLOAD_DIR, index_response, save_upload

router = APIRouter(tags=["files"])


@router.get("/")
async def index():
    return await index_response()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    return save_upload(file)


@router.get("/download/{filename}")
async def download_file(filename: str):
    safe_name = Path(filename).name
    path = OUTPUTS_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"文件不存在: {safe_name}")
    return FileResponse(path, filename=safe_name)


@router.get("/uploads/{filename}")
async def serve_upload(filename: str):
    safe_name = Path(filename).name
    path = UPLOAD_DIR / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path)
