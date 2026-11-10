import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from videre.domain.events import Event, EventType, Snapshot
from videre.services.events import EventHub

router = APIRouter(tags=["runs"])

KEEPALIVE_SECONDS = 15.0


@router.get("/runs/{run_id}")
async def get_snapshot(run_id: str, request: Request) -> Snapshot:
    return await request.app.state.runs.snapshot(run_id)


def format_sse(ev: Event) -> str:
    return f"id: {ev.seq}\nevent: {ev.type}\ndata: {json.dumps(ev.model_dump())}\n\n"


async def event_stream(
    hub: EventHub, run_id: str, after: int, keepalive_s: float = KEEPALIVE_SECONDS
) -> AsyncIterator[str]:
    """Backlog after `after`, then live events; ends after `run.closed`.

    Subscribing before reading the backlog leaves no gap; `last` drops the overlap.
    """
    async with hub.subscribe(run_id) as queue:
        last = after
        for ev in await hub.read_after(run_id, after):
            last = ev.seq
            yield format_sse(ev)
            if ev.type == EventType.RUN_CLOSED:
                return
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if ev.seq <= last:
                continue
            last = ev.seq
            yield format_sse(ev)
            if ev.type == EventType.RUN_CLOSED:
                return


@router.get("/runs/{run_id}/stream")
async def stream(
    run_id: str,
    request: Request,
    after: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    await request.app.state.runs.snapshot(run_id)  # 404 for unknown runs
    start = after if after is not None else (last_event_id or 0)
    return StreamingResponse(
        event_stream(request.app.state.hub, run_id, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
