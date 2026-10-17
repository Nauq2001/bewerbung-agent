"""Phase 1: users, documents, profiles, sources, jobs, evaluations, agent configs.

Revision ID: 0001_core
Revises:
Create Date: 2026-10-17

Design notes that apply to every revision in this directory:

* **TEXT + CHECK, not native enums.** A Postgres enum needs ALTER TYPE to gain
  a value, which does not compose with a transactional migration and cannot be
  rolled back cleanly. Application statuses will gain values; a CHECK list is
  one DROP/ADD CONSTRAINT away.
* **CHECK values are written literally**, never imported from ``app.domain``.
  A migration describes the database as it was applied; reading live code
  would make it rewrite itself whenever an enum changes.
  ``tests/test_schema_vocabularies.py`` catches the drift instead.
* **NULL means the source did not say so.** No column here carries a default
  that would turn a missing value into a fabricated one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # cube + earthdistance give us ll_to_earth() for the commute-radius filter.
    # Both ship with Supabase. If a deployment lacks them, the fallback is a
    # bounding-box prefilter plus haversine in Python - slower, same answers.
    op.execute("CREATE EXTENSION IF NOT EXISTS cube")
    op.execute("CREATE EXTENSION IF NOT EXISTS earthdistance")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")  # gen_random_uuid()

    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        # NULL once a deployment moves to Supabase Auth (phase 6), where
        # auth.users owns credentials.
        sa.Column("password_hash", sa.Text(), nullable=True),
        # The only flag that unlocks personal-tier sources.
        sa.Column("is_owner", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        # Lets Agent 1 skip a file it has already parsed.
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ('certificate', 'cv', 'diploma', 'other', 'reference')",
            name="documents_kind_check",
        ),
    )
    op.create_index("documents_user", "documents", ["user_id", "kind"])
    op.create_unique_constraint(
        "documents_user_hash_uniq", "documents", ["user_id", "content_hash"]
    )

    op.create_table(
        "profiles",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_doc_id", sa.UUID(), sa.ForeignKey("documents.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        # A profile the user has reviewed outranks a freshly extracted one:
        # everything downstream is only as good as this row.
        sa.Column("user_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("profiles_user", "profiles", ["user_id", "created_at"])

    op.create_table(
        "search_prefs",
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("titles", sa.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("home_lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("home_lon", sa.Numeric(9, 6), nullable=True),
        sa.Column("max_commute_km", sa.Integer(), nullable=True),
        sa.Column("contract_types", sa.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("min_hours_week", sa.Integer(), nullable=True),
        sa.Column("required_paid", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # [{"code": "de", "level": "C1"}, ...] - drives the Language Gate.
        sa.Column("languages", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        # Deployment-specific hard rules, e.g. a 360-hour certificate.
        sa.Column("extra_rules", sa.JSON(), nullable=True),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.Text(), nullable=True),
        sa.CheckConstraint("tier IN ('personal', 'public')", name="sources_tier_check"),
        sa.CheckConstraint(
            "last_status IS NULL OR last_status IN "
            "('broken', 'degraded', 'healthy', 'rate_limited', 'unknown')",
            name="sources_status_check",
        ),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Pure function of the posting - see tools/job_key.py in the upstream
        # workspace for the two failure modes this prevents.
        sa.Column("dedup_key", sa.Text(), nullable=False, unique=True),
        sa.Column("source_id", sa.Text(), sa.ForeignKey("sources.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("lat", sa.Numeric(9, 6), nullable=True),
        sa.Column("lon", sa.Numeric(9, 6), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        # Verbatim, never a summary: the posting is usually gone from the web
        # by the time an interview happens.
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("contract_type", sa.Text(), nullable=True),
        sa.Column("hours_per_week", sa.Integer(), nullable=True),
        sa.Column("posted_date", sa.Date(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_checked", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "contract_type IS NULL OR contract_type IN "
            "('both', 'internship', 'other', 'working_student')",
            name="jobs_contract_type_check",
        ),
        sa.CheckConstraint(
            "hours_per_week IS NULL OR hours_per_week BETWEEN 1 AND 60",
            name="jobs_hours_sane_check",
        ),
    )
    op.create_index("jobs_recent", "jobs", ["posted_date"],
                    postgresql_where=sa.text("is_active"))
    op.create_index("jobs_company", "jobs", ["company"])
    # GiST index over the earth-point expression, so the radius filter in
    # Agent 2's query is an index scan rather than a table scan.
    op.execute(
        "CREATE INDEX jobs_geo ON jobs USING gist (ll_to_earth(lat::float8, lon::float8)) "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL"
    )

    op.create_table(
        "evaluations",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("profile_id", sa.UUID(), sa.ForeignKey("profiles.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("overall", sa.SmallInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        # eligibility / language / location / contract, each pass|flag|fail.
        sa.Column("gates", sa.JSON(), nullable=False),
        sa.Column("strengths", sa.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("gaps", sa.ARRAY(sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::text[]")),
        sa.Column("model_used", sa.Text(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        # Keyed on the profile too: an updated CV invalidates old scores
        # instead of silently leaving them to look current.
        sa.UniqueConstraint("user_id", "job_id", "profile_id", name="evaluations_uniq"),
        sa.CheckConstraint("overall BETWEEN 0 AND 100", name="evaluations_overall_range"),
        sa.CheckConstraint(
            "verdict IN ('good', 'moderate', 'poor', 'strong', 'weak')",
            name="evaluations_verdict_check",
        ),
    )
    op.create_index("evaluations_rank", "evaluations", ["user_id", "overall"])

    op.create_table(
        "agent_configs",
        sa.Column("agent", sa.Text(), primary_key=True),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("effort", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("max_tokens", sa.Integer(), nullable=False),
        sa.Column("use_batch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "agent IN ('cv_reader', 'evaluator', 'interview_coach', 'job_finder', "
            "'reviewer', 'writer')",
            name="agent_configs_agent_check",
        ),
        sa.CheckConstraint("effort IN ('high', 'low', 'medium')",
                           name="agent_configs_effort_check"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("agent", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        # Separate from input_tokens on purpose: if this stays zero across
        # repeated calls, prompt caching is silently broken and the real bill
        # is roughly ten times what it should be.
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("batch", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("llm_calls_cost", "llm_calls", ["created_at", "agent"])

    # --- seeds -------------------------------------------------------------
    # Starting model assignment. Changing any of these later is an UPDATE,
    # not a deploy - that is the whole point of the table.
    op.execute(
        """
        INSERT INTO agent_configs (agent, provider, model, effort, max_tokens, use_batch) VALUES
          ('cv_reader',       'anthropic', 'claude-haiku-5-5',  'low',     8000, false),
          ('job_finder',      'anthropic', 'claude-haiku-5-5',  'low',     2000, false),
          ('evaluator',       'anthropic', 'claude-sonnet-5-5', 'medium',  4000, true),
          ('writer',          'anthropic', 'claude-opus-5-5',   'high',   16000, false),
          ('reviewer',        'anthropic', 'claude-sonnet-5-5', 'high',    8000, false),
          ('interview_coach', 'anthropic', 'claude-opus-5-5',   'high',   16000, false)
        """
    )
    # The one source that ships with the repo and needs no credentials.
    op.execute("INSERT INTO sources (id, tier, enabled) VALUES ('arbeitnow', 'public', true)")
    # Single-user mode needs exactly one owner. Placeholder address on purpose:
    # a migration must not carry anybody's real contact details.
    op.execute(
        "INSERT INTO users (email, is_owner) VALUES ('owner@localhost', true) "
        "ON CONFLICT (email) DO NOTHING"
    )


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("agent_configs")
    op.drop_table("evaluations")
    op.execute("DROP INDEX IF EXISTS jobs_geo")
    op.drop_table("jobs")
    op.drop_table("sources")
    op.drop_table("search_prefs")
    op.drop_table("profiles")
    op.drop_table("documents")
    op.drop_table("users")
    # Extensions are left in place: other schemas may rely on them, and
    # dropping a shared extension is not this migration's call.
