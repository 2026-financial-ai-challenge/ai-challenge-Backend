from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TranscriptEvent(Base):
    __tablename__ = "transcript_events"
    __table_args__ = (
        UniqueConstraint(
            "clawops_call_id", "event_type", name="uq_transcript_call_event"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clawops_call_id: Mapped[str] = mapped_column(String(80), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
