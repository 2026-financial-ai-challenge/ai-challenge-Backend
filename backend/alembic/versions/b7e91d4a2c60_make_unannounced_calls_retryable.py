"""make unannounced calls retryable

Revision ID: b7e91d4a2c60
Revises: f2a6c901d3e4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b7e91d4a2c60"
down_revision: Union[str, Sequence[str], None] = "f2a6c901d3e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("scheduled_trainings")}
    checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("scheduled_trainings")
    }
    added_retry_tracking = "attempt_count" not in columns

    if "ck_scheduled_training_status" in checks:
        op.drop_constraint(
            "ck_scheduled_training_status",
            "scheduled_trainings",
            type_="check",
        )
    if "completed_at" not in columns:
        op.add_column(
            "scheduled_trainings",
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "attempt_count" not in columns:
        op.add_column(
            "scheduled_trainings",
            sa.Column(
                "attempt_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )
    if "last_error" not in columns:
        op.add_column(
            "scheduled_trainings",
            sa.Column("last_error", sa.String(500), nullable=True),
        )
    op.create_check_constraint(
        "ck_scheduled_training_status",
        "scheduled_trainings",
        "status IN ('pending', 'started', 'completed', 'failed', 'cancelled')",
    )
    # Jobs created before retry tracking cannot be dispatched safely again:
    # the original call may already have reached the recipient.
    if added_retry_tracking:
        op.execute(
            "UPDATE scheduled_trainings "
            "SET status = 'failed', attempt_count = 2, "
            "last_error = 'legacy_started_job' "
            "WHERE status = 'started'"
        )


def downgrade() -> None:
    op.execute(
        "UPDATE scheduled_trainings SET status = 'started' "
        "WHERE status IN ('completed', 'failed')"
    )
    op.drop_constraint(
        "ck_scheduled_training_status",
        "scheduled_trainings",
        type_="check",
    )
    op.create_check_constraint(
        "ck_scheduled_training_status",
        "scheduled_trainings",
        "status IN ('pending', 'started', 'cancelled')",
    )
    op.drop_column("scheduled_trainings", "last_error")
    op.drop_column("scheduled_trainings", "attempt_count")
    op.drop_column("scheduled_trainings", "completed_at")
