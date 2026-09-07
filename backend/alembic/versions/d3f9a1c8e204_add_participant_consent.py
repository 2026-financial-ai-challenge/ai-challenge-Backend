"""store training consent on the participant

Revision ID: d3f9a1c8e204
Revises: c4a8f2107d31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d3f9a1c8e204"
down_revision: Union[str, Sequence[str], None] = "c4a8f2107d31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "participants",
        sa.Column(
            "privacy_agreed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "participants",
        sa.Column(
            "surprise_call_agreed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "participants",
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("participants", "consented_at")
    op.drop_column("participants", "surprise_call_agreed")
    op.drop_column("participants", "privacy_agreed")
