"""Phase 2: the tracker - marks, applications, event history, roadmap tasks.

Revision ID: 0002_applications
Revises: 0001_core
Create Date: 2026-10-17

Two separations carry the design here, and both are easy to collapse by
accident later:

* ``job_user_state`` is the user's own label on a posting; ``evaluations`` is
  the model's score. Re-scoring happens whenever the CV changes, and it must
  not overwrite a human decision to hide a job.
* ``applications.status`` is the current state for fast queries;
  ``application_events`` is the append-only history. ``occurred_at`` is when
  it happened, ``recorded_at`` is when we were told - response-time statistics
  are wrong if they use the latter.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_applications"
down_revision = "0001_core"
branch_labels = None
depends_on = None

_STATUSES = (
    "'accepted', 'draft', 'ghosted', 'interview', 'offer', "
    "'rejected', 'screening', 'submitted', 'withdrawn'"
)


def upgrade() -> None:
    op.create_table(
        "job_user_state",
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("mark", sa.Text(), nullable=False, server_default="new"),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "mark IN ('hidden', 'interested', 'maybe', 'new')",
            name="job_user_state_mark_check",
        ),
    )
    # Hidden jobs stay in the table and still count in statistics; they are
    # only filtered out of the browse list.
    op.create_index("job_user_state_mark", "job_user_state", ["user_id", "mark"])

    op.create_table(
        "pipeline_templates",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # NULL = a system template, visible to everyone and cloned on edit.
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        # [{key, label, maps_to_status, tasks: [{label, auto, agent, when}]}]
        sa.Column("stages", sa.JSON(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "applications",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("job_id", sa.UUID(), sa.ForeignKey("jobs.id"), nullable=False),
        # '<company>_<role>', collision-safe: every character outside
        # [a-z0-9_] is dropped, so a '/' in a company name can never split the
        # storage path across two directories.
        sa.Column("folder_slug", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("template_id", sa.UUID(),
                  sa.ForeignKey("pipeline_templates.id", ondelete="SET NULL"), nullable=True),
        # What the user is working on. May run ahead of status: you prepare
        # documents while the employer still knows nothing about you.
        sa.Column("current_stage", sa.Text(), nullable=False, server_default="evaluate"),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("next_action_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("user_id", "job_id", name="applications_uniq"),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="applications_status_check"),
        sa.CheckConstraint(
            "folder_slug ~ '^[a-z0-9_]+$'", name="applications_folder_slug_shape"
        ),
    )
    op.create_index("applications_board", "applications", ["user_id", "status"])
    op.create_index("applications_due", "applications", ["next_action_at"],
                    postgresql_where=sa.text("next_action_at IS NOT NULL"))

    op.create_table(
        "application_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("application_id", sa.UUID(),
                  sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.Text(), nullable=True),
        sa.Column("to_status", sa.Text(), nullable=False),
        # User-editable: people record something that happened days ago.
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("origin", sa.Text(), nullable=False, server_default="manual"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(f"to_status IN ({_STATUSES})", name="application_events_to_check"),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUSES})",
            name="application_events_from_check",
        ),
        sa.CheckConstraint(
            "origin IN ('email_sync', 'manual', 'system')",
            name="application_events_origin_check",
        ),
    )
    op.create_index("app_events_app", "application_events", ["application_id", "occurred_at"])

    op.create_table(
        "application_tasks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("application_id", sa.UUID(),
                  sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("stage_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        # An agent ticks this one when its work lands; a human ticks the rest.
        sa.Column("auto", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("agent", sa.Text(), nullable=True),
        sa.Column("due_at", sa.Date(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("NOT auto OR agent IS NOT NULL", name="application_tasks_auto_agent"),
    )
    op.create_index("app_tasks_app", "application_tasks",
                    ["application_id", "stage_key", "sort_order"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("application_id", sa.UUID(),
                  sa.ForeignKey("applications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "kind IN ('cv_pdf', 'cv_tex', 'evaluation_json', 'letter_pdf', "
            "'letter_tex', 'posting_md')",
            name="artifacts_kind_check",
        ),
    )
    op.create_index("artifacts_app", "artifacts", ["application_id", "kind"])

    # --- seed: the default roadmap ----------------------------------------
    # Tasks with a "when" clause are evaluated against the job row at
    # instantiation time - no model call. The working-student question only
    # appears for postings where it actually applies.
    op.execute(
        """
        INSERT INTO pipeline_templates (user_id, name, stages, is_default) VALUES (
          NULL, 'Default', '[
            {"key":"evaluate","label":"Evaluate","maps_to_status":"draft","tasks":[
              {"label":"Automatic scoring","auto":true,"agent":"evaluator"},
              {"label":"Read the posting, confirm any FLAG gates","auto":false},
              {"label":"Decide: apply or skip","auto":false}]},
            {"key":"prepare","label":"Prepare documents","maps_to_status":"draft","tasks":[
              {"label":"Generate CV + cover letter","auto":true,"agent":"writer"},
              {"label":"Reread and adjust the tone","auto":false},
              {"label":"Check the PDF: page count, ATS text layer","auto":false},
              {"label":"Collect attachments (diplomas, certificates)","auto":false},
              {"label":"Ask the employer: will they sign a start date and certify the hours?",
               "auto":false,"when":{"job.contract_type":"working_student"}}]},
            {"key":"submit","label":"Submit","maps_to_status":"submitted","tasks":[
              {"label":"Submit via portal or email","auto":false},
              {"label":"Record the real submission date","auto":false},
              {"label":"Set a follow-up reminder for 14 days","auto":false}]},
            {"key":"wait","label":"Waiting","maps_to_status":"submitted","tasks":[
              {"label":"Follow up if more than 14 days have passed","auto":false},
              {"label":"Optional: look for a referral contact","auto":false}]},
            {"key":"interview","label":"Interview","maps_to_status":"interview","tasks":[
              {"label":"Generate the interview pack","auto":true,"agent":"interview_coach"},
              {"label":"Practise at least 10 questions","auto":false},
              {"label":"Read the company brief","auto":false},
              {"label":"Prepare questions to ask them","auto":false},
              {"label":"Send a thank-you note within 24h","auto":false}]},
            {"key":"offer","label":"Offer","maps_to_status":"offer","tasks":[
              {"label":"Compare the offer against the original posting","auto":false},
              {"label":"Confirm pay and weekly hours","auto":false}]}
          ]'::json, true)
        """
    )


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("application_tasks")
    op.drop_table("application_events")
    op.drop_table("applications")
    op.drop_table("pipeline_templates")
    op.drop_table("job_user_state")
