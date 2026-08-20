from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Consent(Base):
    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id"),
        nullable=False,
    )

    privacy_agreed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )

    privacy_agreed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    surprise_call_agreed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    surprise_call_agreed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    participant = relationship(
        "Participant",
        back_populates="consents",
    )