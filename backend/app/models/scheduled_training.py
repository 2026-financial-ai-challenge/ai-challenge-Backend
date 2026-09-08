from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ScheduledTraining(Base):
    __tablename__ = "scheduled_trainings"
    __table_args__ = (
        CheckConstraint(
            "training_type IN ('unannounced')",
            name="ck_scheduled_training_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'started', 'completed', 'failed', 'cancelled')",
            name="ck_scheduled_training_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    result_session_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    training_type: Mapped[str] = mapped_column(
        String(20), default="unannounced", server_default="unannounced"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    source_session: Mapped["TrainingSession"] = relationship(
        foreign_keys=[source_session_id]
    )
    result_session: Mapped["TrainingSession | None"] = relationship(
        foreign_keys=[result_session_id]
    )
