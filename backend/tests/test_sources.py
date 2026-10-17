"""Tests for the source layer, including the tier boundary.

The tier tests are the ones that matter most in a public repository: they are
what stops a friend's ingest run from executing a personal-tier scraper on the
owner's server.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.sources.base import (
    ContractType,
    HealthStatus,
    RawJob,
    SearchQuery,
    SourceTier,
    assess_health,
    enabled_sources,
)
from app.sources.public.arbeitnow import ArbeitnowSource, classify_contract, matches, to_raw_job


class StubSource:
    def __init__(self, source_id: str, tier: SourceTier) -> None:
        self.id = source_id
        self.tier = tier

    async def search(self, query):  # pragma: no cover
        return []

    async def health(self):  # pragma: no cover
        return assess_health(self.id, [])


PUBLIC = StubSource("arbeitnow", SourceTier.PUBLIC)
PERSONAL = StubSource("linkedin", SourceTier.PERSONAL)


# --- tier boundary ---------------------------------------------------------


def test_non_owner_never_receives_a_personal_source():
    out = enabled_sources([PUBLIC, PERSONAL], is_owner=False)
    assert [source.id for source in out] == ["arbeitnow"]


def test_owner_receives_both_tiers():
    out = enabled_sources([PUBLIC, PERSONAL], is_owner=True)
    assert {source.id for source in out} == {"arbeitnow", "linkedin"}


def test_disabled_source_is_skipped_for_the_owner_too():
    out = enabled_sources([PUBLIC, PERSONAL], is_owner=True, disabled_ids=frozenset({"linkedin"}))
    assert [source.id for source in out] == ["arbeitnow"]


def test_personal_only_deployment_yields_nothing_for_a_guest():
    assert enabled_sources([PERSONAL], is_owner=False) == []


# --- health ----------------------------------------------------------------


def _job(**kw) -> RawJob:
    base = dict(source_id="s", title="Praktikum Softwareentwicklung", company="Acme", url="u")
    return RawJob(**{**base, **kw})


def test_no_results_is_unknown_not_broken():
    # A quiet run is not a broken parser; only a probe can tell them apart.
    assert assess_health("s", []).status is HealthStatus.UNKNOWN


def test_all_companies_blank_means_degraded():
    jobs = [_job(company=""), _job(company="  ")]
    health = assess_health("s", jobs)
    assert health.status is HealthStatus.DEGRADED
    assert "company" in health.detail


def test_undecoded_entities_in_title_means_degraded():
    health = assess_health("s", [_job(title="Praktikum Data &amp; AI")])
    assert health.status is HealthStatus.DEGRADED


def test_clean_results_are_healthy():
    health = assess_health("s", [_job(), _job()])
    assert health.status is HealthStatus.HEALTHY
    assert health.checked_results == 2


# --- contract classification ----------------------------------------------


def test_classify_internship():
    assert classify_contract("Praktikum Softwareentwicklung (m/w/d)") is ContractType.INTERNSHIP
    assert classify_contract("Pflichtpraktikant Informatik") is ContractType.INTERNSHIP


def test_classify_working_student():
    assert classify_contract("Werkstudent Data Analyst") is ContractType.WORKING_STUDENT
    assert classify_contract("Data Engineer Working Student") is ContractType.WORKING_STUDENT


def test_classify_both_when_posting_offers_either():
    # Real example: Deloitte advertises one role as "Praktikant / Werkstudent".
    assert classify_contract("Praktikant / Werkstudent Data Analytics") is ContractType.BOTH


def test_classify_returns_none_rather_than_guessing():
    assert classify_contract("Senior Backend Engineer") is None


# --- normalization and filtering ------------------------------------------


def test_to_raw_job_maps_fields_and_strips_whitespace():
    job = to_raw_job(
        {
            "title": "  Praktikum Frontend  ",
            "company_name": " PwC ",
            "url": "https://example.com/j/1",
            "location": "Frankfurt",
            "created_at": 1760000000,
            "slug": "praktikum-frontend",
        }
    )
    assert job.title == "Praktikum Frontend"
    assert job.company == "PwC"
    assert job.posted_date is not None
    assert job.contract_type is ContractType.INTERNSHIP


def test_missing_fields_become_none_not_empty_string():
    job = to_raw_job({"title": "X", "company_name": "Y", "url": "u"})
    assert job.posted_date is None
    assert job.deadline is None
    assert job.hours_per_week is None


def test_unparseable_date_is_none_not_today():
    job = to_raw_job({"title": "X", "company_name": "Y", "url": "u", "created_at": "garbage"})
    assert job.posted_date is None


def test_keyword_filter_matches_title_or_company():
    job = _job(title="Praktikum Data Engineering")
    assert matches(job, SearchQuery(keywords=["data"]))
    assert not matches(job, SearchQuery(keywords=["embedded"]))


def test_location_filter_is_case_insensitive():
    job = _job(location="Frankfurt am Main, Hesse")
    assert matches(job, SearchQuery(location="frankfurt"))
    assert not matches(job, SearchQuery(location="Berlin"))


def test_age_filter_drops_stale_postings():
    fresh = _job(posted_date=date.today() - timedelta(days=3))
    stale = _job(posted_date=date.today() - timedelta(days=90))
    query = SearchQuery(max_age_days=14)
    assert matches(fresh, query)
    assert not matches(stale, query)


def test_posting_without_a_date_is_kept():
    # Dropping undated postings would silently lose whole sources; the
    # architecture says keep and flag, never infer the date.
    assert matches(_job(posted_date=None), SearchQuery(max_age_days=14))


def test_arbeitnow_declares_public_tier():
    assert ArbeitnowSource().tier is SourceTier.PUBLIC
