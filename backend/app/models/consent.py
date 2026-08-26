from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    privacy_agreed: Mapped[bool] = mapped_column(Boolean)
    surprise_call_agreed: Mapped[bool] = mapped_column(Boolean)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    session: Mapped["TrainingSession"] = relationship(back_populates="consent")
