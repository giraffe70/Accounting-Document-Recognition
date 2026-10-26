"""Authenticated, owner-scoped asynchronous import endpoints."""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from app.services import auth, import_jobs

router = APIRouter(prefix="/api/import-jobs", tags=["imports"])


def owner(request):
    user = auth.get_user_by_token(request.cookies.get("session"))
    if not user:
        raise HTTPException(401, "未登入")
    return user["id"]


@router.post("")
async def create(request: Request, files: list[UploadFile] = File(...), company: str = Form("")):
    from app.main import _save_upload
    paths = []
    try:
        user_id = owner(request)
        for file in files:
            path = await _save_upload(file)
            paths.append({"path": str(path), "name": file.filename})
        return JSONResponse(import_jobs.create_job(paths, user_id, company), status_code=202)
    except ValueError as exc:
        for item in paths:
            from pathlib import Path
            Path(item["path"]).unlink(missing_ok=True)
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)


@router.get("")
def listing(request: Request):
    return import_jobs.list_jobs(owner(request))


@router.get("/{job_id}")
def detail(job_id: str, request: Request):
    try:
        return import_jobs.get_job(job_id, owner(request))
    except import_jobs.JobNotFound as exc:
        raise HTTPException(404, str(exc))


@router.get("/{job_id}/pages/{page_id}/preview")
def preview(job_id: str, page_id: str, request: Request):
    try:
        return FileResponse(import_jobs.preview(job_id, page_id, owner(request)))
    except import_jobs.JobNotFound as exc:
        raise HTTPException(404, str(exc))


@router.post("/{job_id}/{operation}")
def action(job_id: str, operation: str, request: Request):
    try:
        return import_jobs.action(job_id, owner(request), operation)
    except import_jobs.JobNotFound as exc:
        raise HTTPException(404, str(exc))
    except import_jobs.JobConflict as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
