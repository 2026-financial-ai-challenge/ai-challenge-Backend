from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SubmitConsentRequest(BaseModel):
    privacy: bool
    unannouncedTraining: bool


class SubmitConsentResponse(BaseModel):
    sessionId: str


class ConsentRecord(BaseModel):
    privacy: bool
    unannouncedTraining: bool
    consentedAt: datetime


class SessionResponse(BaseModel):
    id: str
    phoneNumberMasked: str | None
    callStatus: Literal["waiting", "calling", "completed", "missed", "silent", "failed"] | None
    callId: str | None = None
    reportStatus: Literal["none", "pending", "draft", "final", "failed"] | None = None
    currentTrainingType: Literal["announced", "unannounced"]
    consents: ConsentRecord
    createdAt: datetime
    updatedAt: datetime


class GetSessionResponse(BaseModel):
    session: SessionResponse
