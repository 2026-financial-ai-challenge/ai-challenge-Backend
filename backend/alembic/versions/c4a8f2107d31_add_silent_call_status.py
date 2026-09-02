"""add silent call status

Revision ID: c4a8f2107d31
Revises: b7e91d4a2c60
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c4a8f2107d31"
down_revision: Union[str, Sequence[str], None] = "b7e91d4a2c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    checks = {
        constraint["name"]: constraint.get("sqltext", "")
        for constraint in inspector.get_check_constraints("training_sessions")
    }
    current = checks.get("ck_session_call_status", "")
    if "silent" in current:
        return
    if "ck_session_call_status" in checks:
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
