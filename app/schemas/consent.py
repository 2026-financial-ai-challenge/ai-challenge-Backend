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


class RegisterPhoneRequest(BaseModel):
    phoneNumber: str


class SessionResponse(BaseModel):
    id: str
    phoneNumberMasked: str | None
    callStatus: Literal["waiting", "calling", "completed"] | None
    currentTrainingType: Literal["announced", "unannounced"]
    consents: ConsentRecord
    createdAt: datetime
    updatedAt: datetime


class RegisterPhoneResponse(BaseModel):
    session: SessionResponse
