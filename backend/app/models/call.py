from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True
    )
    clawops_call_id: Mapped[str] = mapped_column(
        String(80), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="calling")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="calls")
    transcript_turns: Mapped[list["TranscriptTurnRecord"]] = relationship(
        back_populates="call", cascade="all, delete-orphan"
    )
    reports: Mapped[list["TrainingReportRecord"]] = relationship(back_populates="call")
