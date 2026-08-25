from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TranscriptTurnRecord(Base):
    __tablename__ = "transcript_turns"
    __table_args__ = (
        UniqueConstraint("session_id", "source", "sequence", name="uq_turn_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[int | None] = mapped_column(
        ForeignKey("calls.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="live")
    sequence: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="transcript_turns")
    call: Mapped["Call | None"] = relationship(back_populates="transcript_turns")
