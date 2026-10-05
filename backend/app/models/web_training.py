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


# 웹 훈련 페이지(가온 포털 모사)에서 훈련자가 할 수 있는 행동.
# 전화 훈련과 같은 "행동 라벨"로 환산해 report_service가 채점한다.
# 실제 입력값(이름·사건번호·계좌 등)은 저장하지 않는다 — 행동 종류만 남긴다.
WEB_EVENT_TYPES = (
    "link_opened",            # 문자 링크로 페이지에 진입
    "identity_submitted",     # 본인인증(성명/주민번호 등) 제출
    "case_lookup_submitted",  # 사건번호+성명으로 조회 시도
    "financial_info_submitted",  # 계좌/카드 등 금융정보 입력 제출
    "app_install_clicked",    # 안내 앱/프로그램 설치 버튼 클릭
    "report_clicked",         # "의심/신고" 버튼 클릭 (방어 행동)
    "left_without_input",     # 아무 정보도 넣지 않고 빠르게 이탈 (방어 행동)
)


class WebTrainingLink(Base):
    """세션마다 발급되는 1회성 훈련 링크.

    문자로 전달되는 URL(``/t/{token}``)의 토큰이 곧 인증 수단이다. 훈련자는
    로그인 상태가 아니므로, 추측 불가능한 토큰과 만료 시각만으로 접근을 통제한다.
    """

    __tablename__ = "web_training_links"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("training_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    session: Mapped["TrainingSession"] = relationship(back_populates="web_links")
    events: Mapped[list["WebTrainingEvent"]] = relationship(
        back_populates="link",
        cascade="all, delete-orphan",
    )


class WebTrainingEvent(Base):
    """훈련자가 웹 페이지에서 한 행동 1건.

    같은 행동을 여러 번 눌러도 점수가 중복으로 깎이지 않도록 (link, event_type)
    조합은 한 번만 집계한다.
    """

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
