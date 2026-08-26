from pydantic import BaseModel, Field


class RequestSignupOtpRequest(BaseModel):
    phoneNumber: str


class RequestSignupOtpResponse(BaseModel):
    phoneNumberMasked: str
    expiresInSec: int
    resendAvailableInSec: int
    devCode: str | None = None


class VerifySignupOtpRequest(BaseModel):
    phoneNumber: str
    code: str


class VerifySignupOtpResponse(BaseModel):
    verificationToken: str
    expiresInSec: int


class SignupRequest(BaseModel):
    verificationToken: str
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    phoneNumber: str
    password: str


class AuthParticipant(BaseModel):
    id: int
    phoneNumberMasked: str


class AuthResponse(BaseModel):
    accessToken: str
    tokenType: str = "bearer"
    expiresInSec: int
    participant: AuthParticipant
