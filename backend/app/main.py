"""FastAPI entry point.

Phase 1 goal: every capability testable through /docs before any React exists.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api import sources as sources_api

app = FastAPI(
    title="bewerbung-agent",
    description="Multi-agent job application assistant. API-first: drive it from /docs.",
    version="0.1.0",
)

app.include_router(sources_api.router)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    from app.config import settings

    return {
        "status": "ok",
        "environment": settings.environment,
        "mode": "single_user" if settings.single_user_mode else "multi_user",
    }
