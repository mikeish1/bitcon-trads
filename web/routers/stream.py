"""GET /api/stream - Server-Sent Events for near-real-time updates (see web/stream.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from web.deps import AppState, auth_read, get_ctx
from web.stream import event_generator

router = APIRouter(prefix="/api", tags=["realtime"])


@router.get("/stream", dependencies=[Depends(auth_read)])
async def stream(request: Request, ctx: AppState = Depends(get_ctx)) -> StreamingResponse:
    return StreamingResponse(
        event_generator(request, ctx),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable proxy buffering (nginx/Railway edge)
        },
    )
