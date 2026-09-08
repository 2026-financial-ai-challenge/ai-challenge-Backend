"""normalize training schema and add domain constraints

Revision ID: e4d7a1c8b902
Revises: 9f3b2d1a7c04
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e4d7a1c8b902"
down_revision: Union[str, Sequence[str], None] = "9f3b2d1a7c04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("consents_participant_id_fkey", "consents", type_="foreignkey")
    op.drop_column("consents", "participant_id")
    op.drop_column("participants", "phone_number_masked")

    op.create_check_constraint("ck_session_training_type", "training_sessions", "current_training_type IN ('announced', 'unannounced')")
    op.create_check_constraint("ck_session_call_status", "training_sessions", "call_status IS NULL OR call_status IN ('waiting', 'calling', 'completed')")
    op.create_check_constraint("ck_session_report_status", "training_sessions", "report_status IS NULL OR report_status IN ('none', 'pending', 'draft', 'final', 'failed')")
    op.create_check_constraint("ck_call_status", "calls", "status IN ('calling', 'completed', 'failed')")
    op.create_check_constraint("ck_turn_role", "transcript_turns", "role IN ('user', 'assistant')")
    op.create_check_constraint("ck_turn_source", "transcript_turns", "source IN ('live', 'clawops')")
    op.create_check_constraint("ck_report_status", "training_reports", "status IN ('pending', 'draft', 'final', 'failed')")
    op.create_check_constraint("ck_report_source", "training_reports", "source IN ('live', 'clawops')")
    op.create_check_constraint("ck_report_score", "training_reports", "score BETWEEN 0 AND 100")


def downgrade() -> None:
    for name, table in (
        ("ck_report_score", "training_reports"), ("ck_report_source", "training_reports"),
        ("ck_report_status", "training_reports"), ("ck_turn_source", "transcript_turns"),
        ("ck_turn_role", "transcript_turns"), ("ck_call_status", "calls"),
        ("ck_session_report_status", "training_sessions"),
        ("ck_session_call_status", "training_sessions"),
        ("ck_session_training_type", "training_sessions"),
    ):
        op.drop_constraint(name, table, type_="check")
    op.add_column("participants", sa.Column("phone_number_masked", sa.String(20), nullable=True))
    op.execute("UPDATE participants SET phone_number_masked = left(phone_number, 3) || '-****-' || right(phone_number, 4)")
    op.alter_column("participants", "phone_number_masked", nullable=False)
    op.add_column("consents", sa.Column("participant_id", sa.Integer(), nullable=True))
    op.create_foreign_key("consents_participant_id_fkey", "consents", "participants", ["participant_id"], ["id"], ondelete="SET NULL")
    op.execute("UPDATE consents c SET participant_id = s.participant_id FROM training_sessions s WHERE s.id = c.session_id")
