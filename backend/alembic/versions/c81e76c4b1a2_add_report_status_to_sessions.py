"""add report status to training sessions

Revision ID: c81e76c4b1a2
Revises: 9523413ee830
Create Date: 2026-08-25

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c81e76c4b1a2"
down_revision: Union[str, Sequence[str], None] = "9523413ee830"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "training_sessions",
        sa.Column("report_status", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("training_sessions", "report_status")
