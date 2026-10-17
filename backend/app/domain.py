"""Shared vocabularies: the string sets that appear in both the code and the
database CHECK constraints.

Stdlib-only, so the drift test in tests/test_schema_vocabularies.py can import
it without SQLAlchemy installed.

One rule about these and the migrations: a migration **never imports from this
module**. A migration is a historical record of what was applied, so if it
read its CHECK values from live code, editing an enum here would silently
rewrite history and the file would stop describing the database anyone
actually has. The values are written out literally in the migration, and the
drift test is what keeps the two in step.
"""

from __future__ import annotations

from enum import StrEnum


class SourceTier(StrEnum):
    """Whether a job source may be run on behalf of anyone but the owner."""

    PUBLIC = "public"
    PERSONAL = "personal"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BROKEN = "broken"
    RATE_LIMITED = "rate_limited"
    UNKNOWN = "unknown"


class ContractType(StrEnum):
    INTERNSHIP = "internship"
    WORKING_STUDENT = "working_student"
    BOTH = "both"
    OTHER = "other"


class DocumentKind(StrEnum):
    CV = "cv"
    DIPLOMA = "diploma"
    CERTIFICATE = "certificate"
    REFERENCE = "reference"
    OTHER = "other"


class GateVerdict(StrEnum):
    PASS = "pass"
    FLAG = "flag"
    FAIL = "fail"


class Verdict(StrEnum):
    """Fit bands from 04-job-evaluation.md's thresholds."""

    STRONG = "strong"
    GOOD = "good"
    MODERATE = "moderate"
    WEAK = "weak"
    POOR = "poor"


class JobMark(StrEnum):
    """The user's own label on a posting - their opinion, not the model's."""

    NEW = "new"
    INTERESTED = "interested"
    MAYBE = "maybe"
    HIDDEN = "hidden"


class ApplicationStatus(StrEnum):
    """Where the relationship with the employer actually stands.

    ``GHOSTED`` is a status of its own because it is the most common outcome of
    an internship application. Folded into ``SUBMITTED`` it would make every
    funnel statistic wrong.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFER = "offer"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    GHOSTED = "ghosted"
    WITHDRAWN = "withdrawn"


class EventOrigin(StrEnum):
    MANUAL = "manual"
    EMAIL_SYNC = "email_sync"
    SYSTEM = "system"


class ArtifactKind(StrEnum):
    CV_TEX = "cv_tex"
    CV_PDF = "cv_pdf"
    LETTER_TEX = "letter_tex"
    LETTER_PDF = "letter_pdf"
    POSTING_MD = "posting_md"
    EVALUATION_JSON = "evaluation_json"


class AgentName(StrEnum):
    """Rows in ``agent_configs``. Each is independently pointable at a model."""

    CV_READER = "cv_reader"
    JOB_FINDER = "job_finder"
    EVALUATOR = "evaluator"
    WRITER = "writer"
    REVIEWER = "reviewer"
    INTERVIEW_COACH = "interview_coach"


class ChunkSourceKind(StrEnum):
    DOCUMENT = "document"
    APPLICATION_LETTER = "application_letter"
    JOB = "job"


def values(enum_cls: type[StrEnum]) -> list[str]:
    """Sorted member values - the shape a CHECK constraint lists them in."""
    return sorted(member.value for member in enum_cls)
