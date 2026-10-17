"""Add the `account` source tier and the contacts table.

Revision ID: 0005_account_tier
Revises: 0004_retrieval
Create Date: 2026-10-17

Two changes, both following from the local-first deployment decision.

**`account` tier.** With one user per deployment, tier stopped being a
permission question and became a risk question: `public` touches no account,
`personal` reads public pages without logging in, and `account` drives the
user's logged-in session. Only the third can get a real LinkedIn account
restricted, so it needs to be nameable, off by default, and refused in a
hosted deployment.

This is why 0001 is not edited: it has already been applied somewhere, and a
migration is a record of what ran. Widening a CHECK means dropping and
re-adding it here. ``tests/test_schema_vocabularies.py`` is what forced this
file to exist - adding the enum value made it fail until the constraint
followed.

**`contacts`.** Referral lookup reads from the archive LinkedIn lets each
person download of their own data, not from a scraped connection list. That
makes it a plain table import rather than a source connector: complete,
permitted, and incapable of putting an account at risk.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_account_tier"
down_revision = "0004_retrieval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("sources_tier_check", "sources", type_="check")
    op.create_check_constraint(
        "sources_tier_check", "sources", "tier IN ('account', 'personal', 'public')"
    )

    # A source that drives a logged-in session must be switched on
    # deliberately, per source, with the consequence spelled out in the UI.
    op.add_column(
        "sources",
        sa.Column(
            "account_tier_acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Enforced in the database rather than the service layer: an enabled
    # account-tier source with no acknowledgement should be impossible to
    # write, whichever code path writes it.
    op.create_check_constraint(
        "sources_account_ack_check",
        "sources",
        "tier <> 'account' OR NOT enabled OR account_tier_acknowledged",
    )

    op.create_table(
        "contacts",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("profile_url", sa.Text(), nullable=True),
        sa.Column("connected_on", sa.Date(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "source IN ('linkedin_export', 'manual')", name="contacts_source_check"
        ),
    )
    # Referral lookup joins jobs.company against this, case-insensitively.
    op.execute("CREATE INDEX contacts_company ON contacts (user_id, lower(company))")
    # An export re-imported later must update rather than duplicate. profile_url
    # is the stable identity; rows without one fall back to name + company.
    op.execute(
        "CREATE UNIQUE INDEX contacts_profile_uniq ON contacts (user_id, profile_url) "
        "WHERE profile_url IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX contacts_name_company_uniq ON contacts "
        "(user_id, lower(full_name), lower(coalesce(company, ''))) "
        "WHERE profile_url IS NULL"
    )

    op.create_table(
        "imports",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        # What the importer recognised and what it skipped. The archive layout
        # is not something we control, so a run records its own findings rather
        # than assuming a fixed file list.
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint("kind IN ('linkedin_export')", name="imports_kind_check"),
    )
    op.create_index("imports_user", "imports", ["user_id", "imported_at"])


def downgrade() -> None:
    op.drop_table("imports")
    op.drop_table("contacts")
    op.drop_constraint("sources_account_ack_check", "sources", type_="check")
    op.drop_column("sources", "account_tier_acknowledged")
    op.drop_constraint("sources_tier_check", "sources", type_="check")
    op.create_check_constraint(
        "sources_tier_check", "sources", "tier IN ('personal', 'public')"
    )
