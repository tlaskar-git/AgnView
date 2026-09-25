"""Routes for phone uploads: create, send chunks, resume, finish, cancel.

The pairing-key check in app.py covers every /api route, these included. A
request from iroh is the hub's own in-process call, so it passes the same
checks as one from the LAN. All state and every limit live in UploadManager.
"""

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt

from ..core import dispatch_guard, uploads
from ..core.uploads import UploadError, UploadManager

router = APIRouter(prefix="/api")

# How long a client has to send one chunk over the LAN.
CHUNK_BODY_TIMEOUT_SECONDS = 60.0


class CreateUploadRequest(BaseModel):
    name: str
    size: StrictInt
    mime: Optional[str] = None


class FinishUploadRequest(BaseModel):
    sha256: Optional[str] = None


def _manager(request: Request) -> UploadManager:
    manager = getattr(request.app.state, "uploads", None)
    if manager is None:
        raise UploadError(uploads.ERROR_DISABLED)
    if dispatch_guard.is_iroh_request(request) and not getattr(request.app.state, "iroh_uploads_enabled", False):
        raise UploadError(uploads.ERROR_DISABLED)
    return manager


def _owner(request: Request) -> str:
    """Who to count an upload against: the iroh peer, or the LAN address."""
    if dispatch_guard.is_iroh_request(request):
        return str(request.scope.get("agnview_peer") or "iroh:unknown")
    client = request.client
    return "lan:" + (client.host if client else "unknown")


def _refusal(error: UploadError) -> JSONResponse:
    return JSONResponse(status_code=error.status, content=error.body())


async def _read_chunk(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > uploads.CHUNK_SIZE:
                raise UploadError(uploads.ERROR_TOO_LARGE, max_chunk=uploads.CHUNK_SIZE)
        except ValueError:
            raise UploadError(uploads.ERROR_BAD_REQUEST)
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > uploads.CHUNK_SIZE:
            raise UploadError(uploads.ERROR_TOO_LARGE, max_chunk=uploads.CHUNK_SIZE)
    return bytes(data)


@router.post("/uploads", status_code=201)
def create_upload(req: CreateUploadRequest, request: Request) -> Any:
    try:
        manager = _manager(request)
        return manager.create(req.name, req.size, req.mime, _owner(request))
    except UploadError as error:
        return _refusal(error)


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: str, request: Request) -> Any:
    try:
        return _manager(request).status(upload_id)
    except UploadError as error:
        return _refusal(error)


@router.put("/uploads/{upload_id}")
async def put_upload_chunk(upload_id: str, request: Request, offset: int = Query(..., ge=0)) -> Any:
    try:
        manager = _manager(request)
        try:
            data = await asyncio.wait_for(_read_chunk(request), timeout=CHUNK_BODY_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            return JSONResponse(status_code=408, content={"detail": "timeout"})
        # The whole chunk is in memory before anything is written, so a client
        # that drops mid-chunk changes nothing.
        return await run_in_threadpool(manager.write_chunk, upload_id, offset, data)
    except UploadError as error:
        return _refusal(error)


@router.post("/uploads/{upload_id}/finish")
def finish_upload(upload_id: str, request: Request, req: Optional[FinishUploadRequest] = None) -> Any:
    try:
        return _manager(request).finish(upload_id, req.sha256 if req else None)
    except UploadError as error:
        return _refusal(error)


@router.delete("/uploads/{upload_id}")
def cancel_upload(upload_id: str, request: Request) -> Any:
    try:
        return _manager(request).cancel(upload_id)
    except UploadError as error:
        return _refusal(error)
