from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.auth import (
    AuthResponse,
    LoginRequest,
    RequestSignupOtpRequest,
    RequestSignupOtpResponse,
    SignupRequest,
    VerifySignupOtpRequest,
    VerifySignupOtpResponse,
)
from app.services.auth_service import login, request_signup_otp, signup, verify_signup_otp


router = APIRouter(prefix="/v1/auth", tags=["Auth"])


@router.post("/signup/otp", response_model=RequestSignupOtpResponse)
def request_otp(body: RequestSignupOtpRequest, db: Session = Depends(get_db)):
    return request_signup_otp(db, body.phoneNumber)


@router.post("/signup/verify", response_model=VerifySignupOtpResponse)
def verify_otp(body: VerifySignupOtpRequest, db: Session = Depends(get_db)):
    return verify_signup_otp(db, body.phoneNumber, body.code)


@router.post("/signup", response_model=AuthResponse, status_code=201)
def create_account(body: SignupRequest, db: Session = Depends(get_db)):
    return signup(db, body.verificationToken, body.password)


@router.post("/login", response_model=AuthResponse)
def create_login(body: LoginRequest, db: Session = Depends(get_db)):
    return login(db, body.phoneNumber, body.password)
