"""pg_trgm indexes for Chinese/substring search

Revision ID: 0002_pg_trgm
Revises: b9b0dab72c78
"""
from alembic import op

revision = "0002_pg_trgm"
down_revision = "b9b0dab72c78"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE INDEX IF NOT EXISTS ix_items_title_trgm ON items USING gin (title gin_trgm_ops)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_items_summary_trgm ON items USING gin (summary_one gin_trgm_ops)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_items_summary_trgm")
    op.execute("DROP INDEX IF EXISTS ix_items_title_trgm")
