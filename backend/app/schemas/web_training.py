from pydantic import BaseModel

# 리포트에 실리는 웹 훈련 결과 스키마는 report.py에 둔다(전화 점수와 같은 모듈).
# 여기서 재노출만 해 import 경로를 하나로 유지한다.
from app.schemas.report import WebTrainingReport

__all__ = [
    "RecordWebEventRequest",
    "WebTrainingPageResponse",
    "WebTrainingReport",
]


class RecordWebEventRequest(BaseModel):
    eventType: str


class WebTrainingPageResponse(BaseModel):
    """``/t/{token}`` 진입 시 프론트가 페이지를 그릴 때 쓰는 최소 정보.

    세션 식별자 등 민감 정보는 내려주지 않는다 — 링크 유효 여부만 확인한다.
    """

    valid: bool = True
