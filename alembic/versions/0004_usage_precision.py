"""widen cost columns: 4 decimals silently rounded away sub-cent LLM calls

Revision ID: 0004_usage_precision
Revises: 0003_entity_brief
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_usage_precision"
down_revision = "0003_entity_brief"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("llm_usage", "cost_usd", type_=sa.Numeric(12, 6), existing_type=sa.Numeric(10, 4))
    op.alter_column("agent_tasks", "cost_usd", type_=sa.Numeric(12, 6), existing_type=sa.Numeric(10, 5))


def downgrade():
    op.alter_column("llm_usage", "cost_usd", type_=sa.Numeric(10, 4), existing_type=sa.Numeric(12, 6))
    op.alter_column("agent_tasks", "cost_usd", type_=sa.Numeric(10, 5), existing_type=sa.Numeric(12, 6))
