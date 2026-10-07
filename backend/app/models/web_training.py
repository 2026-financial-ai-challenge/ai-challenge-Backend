from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# 웹 훈련 페이지에서 한 행동의 종류만 저장한다. 입력한 이름·계좌 등 실제 값은 저장하지 않는다.
WEB_EVENT_TYPES = (
    "link_opened",
    "identity_submitted",
    "case_lookup_submitted",
    "financial_info_submitted",
    "app_install_clicked",
    "report_clicked",
    "left_without_input",
)

# 이 행동이 기록되면 링크를 닫아, 다른 기기에서 다시 열어 반복하지 못하게 한다.
WEB_RISK_EVENT_TYPES = (
    "identity_submitted",
    "case_lookup_submitted",
    "financial_info_submitted",
    "app_install_clicked",
)


class WebTrainingLink(Base):
    """세션마다 발급되는 1회성 훈련 링크. 훈련자는 로그인하지 않으므로 추측 불가능한 토큰과 만료 시각으로 접근을 통제한다."""

    __tablename__ = "web_training_links"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="web_links")
    events: Mapped[list["WebTrainingEvent"]] = relationship(
        back_populates="link",
        cascade="all, delete-orphan",
    )


class WebTrainingEvent(Base):
    """웹 페이지에서 한 행동 1건. 같은 행동은 (token, event_type) 유니크 제약으로 한 번만 남는다."""

    __tablename__ = "web_training_events"
    __table_args__ = (
        UniqueConstraint("token", "event_type", name="uq_web_event_once"),
        CheckConstraint(
            "event_type IN ("
            + ", ".join(f"'{event}'" for event in WEB_EVENT_TYPES)
            + ")",
            name="ck_web_event_type",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("web_training_links.token", ondelete="CASCADE"),
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    link: Mapped["WebTrainingLink"] = relationship(back_populates="events")
