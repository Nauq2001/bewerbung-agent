"""Phase 7: the retrieval layer - chunks, hybrid search indexes, citations.

Revision ID: 0004_retrieval
Revises: 0003_interview
Create Date: 2026-10-17

Held back to its own revision deliberately. Until a user's document corpus
outgrows a prompt, retrieval adds infrastructure without adding answers, and
pgvector plus an embedding provider is a dependency worth postponing.

Run this one only when phase 7 starts. ``alembic upgrade 0003_interview``
stops short of it.

The dimension below must match the embedding model in use. Changing model
means re-embedding the whole corpus, which is why ``embed_model`` is NOT NULL:
without it there is no way to tell which rows are stale, and comparing vectors
from two different models returns confident nonsense with no error.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_retrieval"
down_revision = "0003_interview"
branch_labels = None
depends_on = None

# voyage-4 family: confirm the dimension against the provider's own docs
# before running this, and keep it in sync with settings.embedding_dim.
EMBED_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        # Intentionally not a foreign key: one column points at documents,
        # artifacts or jobs depending on source_kind. The alternative is three
        # nullable FKs and a CHECK that exactly one is set - more schema for
        # the same guarantee, and this table is written only by the indexer.
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("embed_model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.CheckConstraint(
            "source_kind IN ('application_letter', 'document', 'job')",
            name="chunks_source_kind_check",
        ),
    )
    op.execute(f"ALTER TABLE chunks ADD COLUMN embedding vector({EMBED_DIM})")
    # German stemming ships with Postgres, which is what makes the sparse half
    # of hybrid retrieval free. Exact technical tokens - VBA, Angular 18,
    # Pflichtpraktikum - are where embeddings do worst and this does best.
    op.execute(
        "ALTER TABLE chunks ADD COLUMN tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('german', content)) STORED"
    )
    op.execute("CREATE INDEX chunks_vec ON chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX chunks_fts ON chunks USING gin (tsv)")
    op.create_index("chunks_user", "chunks", ["user_id", "source_kind"])
    op.create_index("chunks_source", "chunks", ["source_id"])

    op.create_table(
        "artifact_citations",
        sa.Column("artifact_id", sa.UUID(), sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
                  primary_key=True),
        # The sentence or bullet as it appears in the generated document.
        sa.Column("claim", sa.Text(), primary_key=True),
        # NULL is the failure case the whole table exists to expose: a claim
        # with no source chunk is a claim with no grounding, and it either
        # gets removed or the user confirms the fact into their documents so
        # it has a source next time.
        sa.Column("chunk_id", sa.UUID(), sa.ForeignKey("chunks.id", ondelete="SET NULL"),
                  nullable=True),
    )
    op.create_index("artifact_citations_chunk", "artifact_citations", ["chunk_id"])


def downgrade() -> None:
    op.drop_table("artifact_citations")
    op.drop_table("chunks")
    # The vector extension stays: dropping a shared extension is not this
    # migration's call.
