"""drop web training links and events

통화 중 문자 링크 훈련을 빼면서 링크·행동 기록 테이블을 지운다.

Revision ID: e9b4c2d71f08
Revises: d8e3f1a20b57
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e9b4c2d71f08"
down_revision: Union[str, Sequence[str], None] = "d8e3f1a20b57"
branch_labels = None
depends_on = None


_EVENT_TYPES = (
    "link_opened",
    "identity_submitted",
    "case_lookup_submitted",
    "financial_info_submitted",
    "app_install_clicked",
    "report_clicked",
    "left_without_input",
)


def upgrade() -> None:
    op.drop_index("ix_web_training_events_session_id", "web_training_events")
    op.drop_index("ix_web_training_events_token", "web_training_events")
    op.drop_table("web_training_events")
    op.drop_index("ix_web_training_links_session_id", "web_training_links")
    op.drop_table("web_training_links")


def downgrade() -> None:
    op.create_table(
        "web_training_links",
        sa.Column("token", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=40), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["training_sessions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_web_training_links_session_id",
        "web_training_links",
        ["session_id"],
    )

    event_values = ", ".join(f"'{value}'" for value in _EVENT_TYPES)
    op.create_table(
        "web_training_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=40), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["token"], ["web_training_links.token"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["training_sessions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("token", "event_type", name="uq_web_event_once"),
        sa.CheckConstraint(
            f"event_type IN ({event_values})", name="ck_web_event_type"
        ),
    )
    op.create_index(
        "ix_web_training_events_token",
        "web_training_events",
        ["token"],
    )
    op.create_index(
        "ix_web_training_events_session_id",
        "web_training_events",
        ["session_id"],
    )
