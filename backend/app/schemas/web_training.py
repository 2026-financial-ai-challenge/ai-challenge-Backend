from pydantic import BaseModel


class RecordWebEventRequest(BaseModel):
    eventType: str


class WebTrainingPageResponse(BaseModel):
    """링크 유효 여부만 알려 준다. 세션 식별자 같은 정보는 내려주지 않는다."""

    valid: bool = True


class IssueLinkResponse(BaseModel):
    token: str
