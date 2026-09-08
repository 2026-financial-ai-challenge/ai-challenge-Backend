"""add member authentication

Revision ID: 9f3b2d1a7c04
Revises: 6aa62836a202
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9f3b2d1a7c04"
down_revision: Union[str, Sequence[str], None] = "6aa62836a202"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("participants", sa.Column("password_hash", sa.String(255), nullable=True))
    op.add_column("participants", sa.Column("phone_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "phone_verifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone_number", sa.String(20), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("verification_token_hash", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fail_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("verification_token_hash"),
    )
    op.create_index("ix_phone_verifications_phone_number", "phone_verifications", ["phone_number"])


def downgrade() -> None:
    op.drop_index("ix_phone_verifications_phone_number", table_name="phone_verifications")
    op.drop_table("phone_verifications")
    op.drop_column("participants", "phone_verified_at")
    op.drop_column("participants", "password_hash")
