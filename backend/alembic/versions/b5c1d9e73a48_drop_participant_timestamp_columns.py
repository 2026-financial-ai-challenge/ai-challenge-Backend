"""drop participant timestamp columns

phone_verified_at now lives in Redis; updated_at was never read.

Revision ID: b5c1d9e73a48
Revises: a1b4c7e02f36
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b5c1d9e73a48"
down_revision: Union[str, Sequence[str], None] = "a1b4c7e02f36"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("participants", "phone_verified_at")
    op.drop_column("participants", "updated_at")


def downgrade() -> None:
    op.add_column(
        "participants",
        sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # updated_at was NOT NULL without a default, so existing rows need a value
    # before the constraint can go back on.
    op.add_column(
        "participants",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("participants", "updated_at", server_default=None)
