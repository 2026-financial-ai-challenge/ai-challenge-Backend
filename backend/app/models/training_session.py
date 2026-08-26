from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrainingSession(Base):
    __tablename__ = "training_sessions"
    __table_args__ = (
        CheckConstraint("current_training_type IN ('announced', 'unannounced')", name="ck_session_training_type"),
        CheckConstraint("call_status IS NULL OR call_status IN ('waiting', 'calling', 'completed')", name="ck_session_call_status"),
        CheckConstraint("report_status IS NULL OR report_status IN ('none', 'pending', 'draft', 'final', 'failed')", name="ck_session_report_status"),
    )

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("participants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    current_training_type: Mapped[str] = mapped_column(
        String(20), default="announced"
    )
    call_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    report_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    participant: Mapped["Participant | None"] = relationship(
        back_populates="sessions"
    )
    consent: Mapped["Consent"] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False
    )
    calls: Mapped[list["Call"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    transcript_turns: Mapped[list["TranscriptTurnRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    reports: Mapped[list["TrainingReportRecord"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
