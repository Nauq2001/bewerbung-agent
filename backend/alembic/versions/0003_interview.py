"""Phase 4: interview packs, practice answers, and the trigger that queues them.

Revision ID: 0003_interview
Revises: 0002_applications
Create Date: 2026-10-17

The trigger is the point of this revision. Agent 5 is not something the user
remembers to press - the moment an employer invites them to interview, the
prep pack should already be building. Putting that rule in the database rather
than in a service means it fires whichever code path recorded the event:
the API, the email sync, or a manual fix in SQL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_interview"
down_revision = "0002_applications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generic work queue. arq owns the Redis-side scheduling; this table is
    # the durable record, so a worker restart cannot lose a queued job.
    op.create_table(
        "task_queue",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('done', 'failed', 'queued', 'running')", name="task_queue_state_check"
        ),
    )
    op.create_index("task_queue_pending", "task_queue", ["state", "created_at"],
                    postgresql_where=sa.text("state IN ('queued', 'running')"))

    op.create_table(
        "interview_packs",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("application_id", sa.UUID(),
                  sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        # company_brief / likely_questions / star_mapping / tough_questions /
        # questions_to_ask / red_flags_to_probe. Every company claim inside
        # company_brief carries its own source_url.
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("model_used", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("interview_packs_app", "interview_packs",
                    ["application_id", "created_at"])

    op.create_table(
        "practice_answers",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("pack_id", sa.UUID(),
                  sa.ForeignKey("interview_packs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_idx", sa.Integer(), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        # The user's own read on how it went - the signal for what to drill.
        sa.Column("confidence", sa.SmallInteger(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 1 AND 5",
            name="practice_answers_confidence_range",
        ),
    )
    op.create_index("practice_answers_pack", "practice_answers", ["pack_id", "question_idx"])

    op.execute(
        """
        CREATE OR REPLACE FUNCTION queue_interview_prep() RETURNS trigger AS $$
        BEGIN
            IF NEW.to_status = 'interview'
               AND (OLD IS NULL OR OLD.to_status IS DISTINCT FROM 'interview') THEN
                INSERT INTO task_queue (kind, payload)
                VALUES ('interview_prep',
                        json_build_object('application_id', NEW.application_id)::json);
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER application_events_interview_prep
        AFTER INSERT ON application_events
        FOR EACH ROW EXECUTE FUNCTION queue_interview_prep()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS application_events_interview_prep ON application_events")
    op.execute("DROP FUNCTION IF EXISTS queue_interview_prep()")
    op.drop_table("practice_answers")
    op.drop_table("interview_packs")
    op.drop_table("task_queue")
