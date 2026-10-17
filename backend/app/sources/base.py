"""Job source contract and the tier boundary.

The tier split is a legal boundary, not a feature flag.

  PUBLIC    the provider's terms permit programmatic access. Every user of a
            deployment may run these.
  PERSONAL  no such permission. Only the deployment owner may run them, for
            their own job search. Shipped as local plugins, never committed -
            see app/sources/personal/README.md.

``enabled_sources`` below is the single enforcement point. Putting the check in
the API layer or the UI would mean one slip runs a personal scraper on behalf
of a friend, using the owner's server. The check belongs where the list is
built, so there is no other path to a source object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

# Vocabularies live in app.domain: they appear in database CHECK constraints
# too, and two definitions of the same string set is the drift that
# tests/test_schema_vocabularies.py exists to prevent. Re-exported here so
# callers of this module keep their imports.
from app.domain import ContractType, HealthStatus, SourceTier

__all__ = [
    "ContractType",
    "HealthStatus",
    "JobSource",
    "RawJob",
    "SearchQuery",
    "SourceHealth",
    "SourceTier",
    "assess_health",
    "enabled_sources",
]

# A note the enums no longer carry, worth keeping where the health logic is:
# DEGRADED means the parser half-works - exit 0, fields null or garbled - which
# is the failure mode that silently poisons the job table. RATE_LIMITED is
# never evidence of breakage; back off rather than re-probing.


@dataclass(slots=True)
class SearchQuery:
    keywords: list[str] = field(default_factory=list)
    location: str | None = None
    country: str | None = None
    max_age_days: int = 14
    limit: int = 20


@dataclass(slots=True)
class RawJob:
    """One search hit, before normalization.

    ``None`` means the source did not state the field. It never means zero and
    never means "unknown, go guess" - nothing downstream may infer a deadline
    or an hour count that the posting did not contain.
    """

    source_id: str
    title: str
    company: str
    url: str
    location: str | None = None
    posted_date: date | None = None
    deadline: date | None = None
    description: str | None = None
    hours_per_week: int | None = None
    contract_type: ContractType | None = None
    external_ref: str | None = None


@dataclass(slots=True)
class SourceHealth:
    source_id: str
    status: HealthStatus
    detail: str = ""
    checked_results: int = 0


@runtime_checkable
class JobSource(Protocol):
    id: str
    tier: SourceTier

    async def search(self, query: SearchQuery) -> list[RawJob]: ...

    async def health(self) -> SourceHealth: ...


def assess_health(source_id: str, results: list[RawJob]) -> SourceHealth:
    """Judge a source from the results it just returned - no extra requests.

    A scraper whose target changed its markup usually exits 0 and returns rows
    with empty companies or HTML left in the titles. Exit codes do not catch
    that; the data does.
    """
    if not results:
        return SourceHealth(source_id, HealthStatus.UNKNOWN, "no results this run")

    blank_company = sum(1 for job in results if not (job.company or "").strip())
    blank_title = sum(1 for job in results if not (job.title or "").strip())
    undecoded = sum(1 for job in results if "&amp;" in job.title or "<" in job.title)

    if blank_company == len(results):
        return SourceHealth(
            source_id, HealthStatus.DEGRADED, "company empty on every result", len(results)
        )
    if blank_title:
        return SourceHealth(
            source_id, HealthStatus.DEGRADED, f"{blank_title} empty titles", len(results)
        )
    if undecoded:
        return SourceHealth(
            source_id,
            HealthStatus.DEGRADED,
            f"{undecoded} titles carry undecoded HTML",
            len(results),
        )
    return SourceHealth(source_id, HealthStatus.HEALTHY, checked_results=len(results))


def enabled_sources(
    all_sources: list[JobSource], *, is_owner: bool, disabled_ids: frozenset[str] = frozenset()
) -> list[JobSource]:
    """The only way to get a runnable source list.

    A non-owner never receives a PERSONAL source, whatever the request asked
    for and whatever the UI offered.
    """
    out: list[JobSource] = []
    for source in all_sources:
        if source.id in disabled_ids:
            continue
        if source.tier is SourceTier.PERSONAL and not is_owner:
            continue
        out.append(source)
    return out
