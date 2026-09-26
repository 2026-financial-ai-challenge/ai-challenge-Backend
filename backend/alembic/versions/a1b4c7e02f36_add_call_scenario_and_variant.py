"""record the scenario and agent variant on each call

Revision ID: a1b4c7e02f36
Revises: d3f9a1c8e204
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b4c7e02f36"
down_revision: Union[str, Sequence[str], None] = "d3f9a1c8e204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calls", sa.Column("scenario_id", sa.String(length=64), nullable=True))
    op.add_column("calls", sa.Column("agent_variant", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("calls", "agent_variant")
    op.drop_column("calls", "scenario_id")
