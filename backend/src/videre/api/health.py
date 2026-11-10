from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from videre import __version__

router = APIRouter(tags=["health"])


class Health(BaseModel):
    status: Literal["ok"]
    version: str
    rail: Literal["paypal", "mock"]
    demo_replay: bool


@router.get("/health")
async def health(request: Request) -> Health:
    """Liveness plus a real database round trip. Reports no secrets."""
    await request.app.state.store.fetch_one("SELECT 1")
    settings = request.app.state.settings
    return Health(
        status="ok", version=__version__, rail=settings.rail, demo_replay=settings.demo_replay
    )
