"""add silent call status

Revision ID: c4a8f2107d31
Revises: b7e91d4a2c60
"""
from typing import Sequence, Union

from alembic import op


revision: str = "c4a8f2107d31"
down_revision: Union[str, Sequence[str], None] = "b7e91d4a2c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_session_call_status",
        "training_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_session_call_status",
        "training_sessions",
        "call_status IS NULL OR call_status IN "
        "('waiting', 'calling', 'completed', 'missed', 'silent', 'failed')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE training_sessions SET call_status = 'missed' "
        "WHERE call_status = 'silent'"
    )
    op.drop_constraint(
        "ck_session_call_status",
        "training_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_session_call_status",
        "training_sessions",
        "call_status IS NULL OR call_status IN "
        "('waiting', 'calling', 'completed', 'missed', 'failed')",
    )
