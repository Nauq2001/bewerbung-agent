"""Source endpoints.

Note what the handlers do NOT do: decide who may run a personal-tier source.
That decision lives in ``enabled_sources``, so there is no route that can
accidentally bypass it.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.sources.base import SearchQuery, enabled_sources
from app.sources.registry import all_sources

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("")
async def list_sources() -> list[dict[str, str]]:
    available = enabled_sources(all_sources(), is_owner=settings.is_owner_request)
    return [{"id": source.id, "tier": source.tier.value} for source in available]


@router.post("/{source_id}/search")
async def search_source(
    source_id: str,
    keywords: str = "",
    location: str = "",
    max_age_days: int = 14,
    limit: int = 20,
) -> dict[str, object]:
    available = {
        source.id: source
        for source in enabled_sources(all_sources(), is_owner=settings.is_owner_request)
    }
    source = available.get(source_id)
    if source is None:
        # Deliberately 404, not 403: a guest learns nothing about which
        # personal-tier sources the owner has installed.
        raise HTTPException(status_code=404, detail="unknown or unavailable source")

    query = SearchQuery(
        keywords=[word for word in keywords.split(",") if word.strip()],
        location=location or None,
        max_age_days=max_age_days,
        limit=limit,
    )
    jobs = await source.search(query)
    return {
        "source": source_id,
        "count": len(jobs),
        "results": [
            {
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "url": job.url,
                "posted_date": job.posted_date.isoformat() if job.posted_date else None,
                "contract_type": job.contract_type.value if job.contract_type else None,
            }
            for job in jobs
        ],
    }


@router.get("/{source_id}/health")
async def source_health(source_id: str) -> dict[str, object]:
    available = {
        source.id: source
        for source in enabled_sources(all_sources(), is_owner=settings.is_owner_request)
    }
    source = available.get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="unknown or unavailable source")
    health = await source.health()
    return {
        "source": health.source_id,
        "status": health.status.value,
        "detail": health.detail,
        "checked_results": health.checked_results,
    }
