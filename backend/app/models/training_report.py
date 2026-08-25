from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class TrainingReportRecord(Base):
    __tablename__ = "training_reports"
    __table_args__ = (
        UniqueConstraint("session_id", "source", name="uq_report_session_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True
    )
    call_id: Mapped[int | None] = mapped_column(
        ForeignKey("calls.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))
    score: Mapped[int] = mapped_column(Integer, default=60, server_default="60")
    suspected: Mapped[bool] = mapped_column(Boolean)
    gave_name: Mapped[bool] = mapped_column(Boolean)
    tried_hangup: Mapped[bool] = mapped_column(Boolean)
    summary: Mapped[str] = mapped_column(Text)
    coaching: Mapped[str] = mapped_column(Text)
    risk_behaviors: Mapped[list] = mapped_column(JSON, default=list)
    defense_behaviors: Mapped[list] = mapped_column(JSON, default=list)
    clawops_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="reports")
    call: Mapped["Call | None"] = relationship(back_populates="reports")
