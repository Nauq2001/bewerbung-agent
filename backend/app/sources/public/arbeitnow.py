"""Arbeitnow - the first public source.

Keyless public job-board feed with German coverage, which makes it the only
source a fresh clone can run with no setup at all. Coverage is narrower than
the personal-tier scrapers: measure it before promising anyone this is enough
(docs/ARCHITECTURE.md section 10 tracks that as the project's top risk).

API: https://www.arbeitnow.com/api/job-board-api
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.sources.base import (
    ContractType,
    HealthStatus,
    JobSource,
    RawJob,
    SearchQuery,
    SourceHealth,
    SourceTier,
    assess_health,
)

_ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"


def classify_contract(title: str) -> ContractType | None:
    """Infer the contract track from a job title - no LLM.

    German postings are consistent enough that regex-free substring checks
    beat a model call here, and this runs on every row of every ingest.
    """
    low = title.lower()
    internship = "praktik" in low  # Praktikum, Praktikant, Pflichtpraktikum
    working_student = "werkstud" in low or "working student" in low
    if internship and working_student:
        return ContractType.BOTH
    if internship:
        return ContractType.INTERNSHIP
    if working_student:
        return ContractType.WORKING_STUDENT
    return None


def _parse_created(value: Any) -> date | None:
    """Arbeitnow sends a unix timestamp; tolerate ISO strings and junk."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def to_raw_job(entry: dict[str, Any]) -> RawJob:
    title = (entry.get("title") or "").strip()
    return RawJob(
        source_id="arbeitnow",
        title=title,
        company=(entry.get("company_name") or "").strip(),
        url=(entry.get("url") or "").strip(),
        location=(entry.get("location") or None),
        posted_date=_parse_created(entry.get("created_at")),
        description=entry.get("description") or None,
        contract_type=classify_contract(title),
        external_ref=entry.get("slug") or None,
    )


def matches(job: RawJob, query: SearchQuery) -> bool:
    """Client-side filter.

    The feed has no query parameters worth the name, so filtering happens
    here. Age is filtered client-side too: a source with no recency filter
    still gets scoped, it just costs us the bandwidth.
    """
    if query.keywords:
        haystack = f"{job.title} {job.company}".lower()
        if not any(word.lower() in haystack for word in query.keywords):
            return False
    if query.location and job.location:
        if query.location.lower() not in job.location.lower():
            return False
    if query.max_age_days and job.posted_date:
        age = (date.today() - job.posted_date).days
        if age > query.max_age_days:
            return False
    return True


class ArbeitnowSource(JobSource):
    id = "arbeitnow"
    tier = SourceTier.PUBLIC

    def __init__(self, http: Any | None = None) -> None:
        self._http = http

    async def _fetch(self, page: int) -> list[dict[str, Any]]:
        if self._http is None:
            import httpx

            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(_ENDPOINT, params={"page": page})
                response.raise_for_status()
                return response.json().get("data", [])
        payload = await self._http.get(_ENDPOINT, params={"page": page})
        return payload.get("data", [])

    async def search(self, query: SearchQuery) -> list[RawJob]:
        out: list[RawJob] = []
        # Paginated feeds repeat rows when new postings shift the page
        # boundaries between requests, so the same posting can arrive twice in
        # one run. The database would collapse them on dedup_key, but letting
        # them through here inflates the health check's result count and wastes
        # a detail fetch per copy.
        seen_urls: set[str] = set()
        page = 1
        while len(out) < query.limit and page <= 3:
            entries = await self._fetch(page)
            if not entries:
                break
            for entry in entries:
                job = to_raw_job(entry)
                if not job.url or job.url in seen_urls:
                    continue
                if matches(job, query):
                    seen_urls.add(job.url)
                    out.append(job)
                    if len(out) >= query.limit:
                        break
            page += 1
        return out

    async def health(self) -> SourceHealth:
        try:
            entries = await self._fetch(1)
        except Exception as exc:
            return SourceHealth(self.id, HealthStatus.BROKEN, str(exc)[:200])
        return assess_health(self.id, [to_raw_job(entry) for entry in entries])
