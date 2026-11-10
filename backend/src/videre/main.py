"""FastAPI app factory and router mounting."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from videre import __version__
from videre.api import health
from videre.config import Settings, get_settings
from videre.storage.db import Store


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = Store(settings.db_path)
        await store.open()
        app.state.store = store
        try:
            yield
        finally:
            await store.close()

    app = FastAPI(title="Videre", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],  # only the local frontend
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )
    app.include_router(health.router, prefix="/api")
    return app


def app() -> FastAPI:
    """Factory entry point: `uvicorn videre.main:app --factory`."""
    return create_app()
