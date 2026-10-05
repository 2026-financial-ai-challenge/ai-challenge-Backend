"""add closed_at to web training links

Revision ID: d8e3f1a20b57
Revises: c7d2e9f04a15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d8e3f1a20b57"
down_revision: Union[str, Sequence[str], None] = "c7d2e9f04a15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "web_training_links",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("web_training_links", "closed_at")
