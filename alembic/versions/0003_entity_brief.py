"""agent-written entity brief

Revision ID: 0003_entity_brief
Revises: 0002_pg_trgm
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_entity_brief"
down_revision = "0002_pg_trgm"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("entities", sa.Column("brief", postgresql.JSONB(), nullable=True))
    op.add_column("entities", sa.Column("brief_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("entities", sa.Column("brief_model", sa.Text(), nullable=True))
    op.add_column("entities", sa.Column("brief_source_count", sa.Integer(), nullable=True))


def downgrade():
    for c in ("brief_source_count", "brief_model", "brief_at", "brief"):
        op.drop_column("entities", c)
