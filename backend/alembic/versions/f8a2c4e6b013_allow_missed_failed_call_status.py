"""allow missed and failed call status on training sessions

Revision ID: f8a2c4e6b013
Revises: e4d7a1c8b902
"""
from typing import Sequence, Union

from alembic import op

revision: str = "f8a2c4e6b013"
down_revision: Union[str, Sequence[str], None] = "e4d7a1c8b902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_session_call_status", "training_sessions", type_="check")
    op.create_check_constraint(
        "ck_session_call_status",
        "training_sessions",
        "call_status IS NULL OR call_status IN ('waiting', 'calling', 'completed', 'missed', 'failed')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_call_status", "training_sessions", type_="check")
    op.create_check_constraint(
        "ck_session_call_status",
        "training_sessions",
        "call_status IS NULL OR call_status IN ('waiting', 'calling', 'completed')",
    )
