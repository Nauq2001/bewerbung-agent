# Personal-tier sources

This directory is **not committed**. It is where the deployment owner drops
adapters for job boards whose terms do not permit programmatic access
(LinkedIn, StepStone, Indeed and the like).

Why it is empty in the repository: publishing a scraper is a different act
from running one for yourself. The framework, the interface and the tier
enforcement are public; the adapters that have no permission to be public
are not.

## Writing one

Each module exports a module-level `SOURCE` instance whose `tier` is
`SourceTier.PERSONAL`. The registry refuses to load anything here that
declares a different tier.

```python
# app/sources/personal/linkedin.py
from app.sources.base import JobSource, RawJob, SearchQuery, SourceHealth, SourceTier

class LinkedInSource(JobSource):
    id = "linkedin"
    tier = SourceTier.PERSONAL

    async def search(self, query: SearchQuery) -> list[RawJob]: ...
    async def health(self) -> SourceHealth: ...

SOURCE = LinkedInSource()
```

## Before you fetch

Run the robots.txt gate first. The rule is fail-closed: if the policy cannot
be read, there is no permission.

## Who may run these

Only a user whose `is_owner` column is true. Enforcement lives in
`app.sources.base.enabled_sources` — never re-implement that check in a route
or a component.
