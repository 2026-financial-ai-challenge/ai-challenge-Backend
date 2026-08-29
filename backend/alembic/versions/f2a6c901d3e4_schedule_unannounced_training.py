"""schedule unannounced training calls

Revision ID: f2a6c901d3e4
Revises: f8a2c4e6b013
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2a6c901d3e4"
down_revision: Union[str, Sequence[str], None] = "f8a2c4e6b013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_trainings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_session_id", sa.String(40), nullable=False),
        sa.Column("result_session_id", sa.String(40), nullable=True),
        sa.Column(
            "training_type",
            sa.String(20),
            server_default="unannounced",
            nullable=False,
        ),
        sa.Column(
            "status", sa.String(20), server_default="pending", nullable=False
        ),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "training_type IN ('unannounced')",
            name="ck_scheduled_training_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'started', 'cancelled')",
            name="ck_scheduled_training_status",
        ),
        sa.ForeignKeyConstraint(
            ["source_session_id"],
            ["training_sessions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["result_session_id"],
            ["training_sessions.id"],
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("source_session_id"),
        sa.UniqueConstraint("result_session_id"),
    )
    op.create_index(
        "ix_scheduled_trainings_source_session_id",
        "scheduled_trainings",
        ["source_session_id"],
    )
    op.create_index(
        "ix_scheduled_trainings_status", "scheduled_trainings", ["status"]
    )
    op.create_index(
        "ix_scheduled_trainings_scheduled_at",
        "scheduled_trainings",
        ["scheduled_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduled_trainings_scheduled_at", table_name="scheduled_trainings"
    )
    op.drop_index(
        "ix_scheduled_trainings_status", table_name="scheduled_trainings"
    )
    op.drop_index(
        "ix_scheduled_trainings_source_session_id",
        table_name="scheduled_trainings",
    )
    op.drop_table("scheduled_trainings")
