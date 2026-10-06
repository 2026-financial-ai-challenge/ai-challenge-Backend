import base64
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone
from secrets import randbelow, token_urlsafe

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ApiError
from app.models.participant import Participant
from app.phone_verification_store import (
    claim_send_slot,
    consume_token,
    count_failure,
    discard_challenge,
    issue_token,
    mark_phone_verified,
    read_challenge,
    release_send_slot,
    store_challenge,
    token_was_spent,
)
from app.schemas.auth import (
    AuthParticipant,
    AuthResponse,
    RequestSignupOtpResponse,
    VerifySignupOtpResponse,
)
from app.services.session_service import mask_phone_number
from app.services.sms_service import expose_dev_code, send_verification_code

PHONE_PATTERN = re.compile(r"010\d{8}")
OTP_TTL_SEC = 300
TOKEN_TTL_SEC = 600
ACCESS_TOKEN_TTL_SEC = 3600
OTP_RESEND_SEC = 60
MAX_FAILS = 5


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if not PHONE_PATTERN.fullmatch(digits):
        raise ApiError(400, "INVALID_PHONE", "010으로 시작하는 휴대전화번호 11자리를 입력해 주세요.")
    return digits


def request_signup_otp(db: Session, phone: str) -> RequestSignupOtpResponse:
    phone = normalize_phone(phone)
    existing = db.scalar(select(Participant).where(Participant.phone_number == phone))
    if existing is not None and existing.password_hash is not None:
        raise ApiError(409, "PHONE_ALREADY_REGISTERED", "이미 가입된 전화번호입니다.")
    if not claim_send_slot(phone, OTP_RESEND_SEC):
        raise ApiError(429, "OTP_COOLDOWN", f"인증번호는 {OTP_RESEND_SEC}초 후 다시 요청할 수 있습니다.")
    code = f"{randbelow(1_000_000):06d}"
    store_challenge(phone, _digest(f"{phone}:{code}"), OTP_TTL_SEC)
    try:
        send_verification_code(phone, code)
    except Exception:
        # 발송에 실패한 코드는 남기지 않고, 재요청 대기 시간도 되돌린다.
        discard_challenge(phone)
        release_send_slot(phone)
        raise ApiError(502, "SMS_SEND_FAILED", "인증번호 발송에 실패했습니다.") from None
    return RequestSignupOtpResponse(
        phoneNumberMasked=mask_phone_number(phone), expiresInSec=OTP_TTL_SEC,
        resendAvailableInSec=OTP_RESEND_SEC, devCode=code if expose_dev_code() else None,
    )


def verify_signup_otp(phone: str, code: str) -> VerifySignupOtpResponse:
    phone = normalize_phone(phone)
    challenge = read_challenge(phone)
    if challenge is None:
        raise ApiError(400, "OTP_NOT_REQUESTED", "인증번호를 먼저 요청해 주세요.")
    if int(challenge.get("fail_count", 0)) >= MAX_FAILS:
        raise ApiError(429, "OTP_LOCKED", "인증 시도 횟수를 초과했습니다.")
    expected = _digest(f"{phone}:{code.strip()}")
    if not hmac.compare_digest(challenge["code_hash"], expected):
        failures = count_failure(phone)
        if failures >= MAX_FAILS:
            raise ApiError(429, "OTP_LOCKED", "인증 시도 횟수를 초과했습니다.")
        raise ApiError(400, "OTP_INVALID", f"인증번호가 올바르지 않습니다. ({MAX_FAILS - failures}회 남음)")
    token = token_urlsafe(32)
    issue_token(phone, _digest(token), TOKEN_TTL_SEC)
    return VerifySignupOtpResponse(verificationToken=token, expiresInSec=TOKEN_TTL_SEC)


def signup(
    db: Session,
    verification_token: str,
    password: str,
    *,
    privacy: bool,
    unannounced_training: bool,
) -> AuthResponse:
    _validate_password(password)
    if not privacy or not unannounced_training:
        raise ApiError(400, "CONSENT_REQUIRED", "필수 동의 항목에 모두 동의해야 합니다.")
    token_hash = _digest(verification_token)
    phone = consume_token(token_hash, spent_ttl_sec=TOKEN_TTL_SEC)
    if phone is None:
        if token_was_spent(token_hash):
            raise ApiError(400, "VERIFICATION_TOKEN_USED", "이미 사용된 인증 토큰입니다.")
        raise ApiError(400, "INVALID_VERIFICATION_TOKEN", "유효하지 않은 인증 토큰입니다.")
    now = _now()
    participant = db.scalar(
        select(Participant).where(Participant.phone_number == phone).with_for_update()
    )
    if participant is not None and participant.password_hash is not None:
        raise ApiError(409, "PHONE_ALREADY_REGISTERED", "이미 가입된 전화번호입니다.")
    if participant is None:
        participant = Participant(phone_number=phone)
        db.add(participant)
    participant.password_hash = hash_password(password)
    record_training_consent(participant, now=now)
    db.commit()
    db.refresh(participant)
    mark_phone_verified(participant.id, now)
    return _auth_response(participant)


def login(db: Session, phone: str, password: str) -> AuthResponse:
    phone = normalize_phone(phone)
    participant = db.scalar(select(Participant).where(Participant.phone_number == phone))
    if (
        participant is None
        or participant.password_hash is None
        or not verify_password(password, participant.password_hash)
    ):
        raise ApiError(401, "INVALID_CREDENTIALS", "전화번호 또는 비밀번호가 올바르지 않습니다.")
    return _auth_response(participant)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    rounds = 600_000
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${_b64(salt)}${_b64(hashed)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(rounds))
        return hmac.compare_digest(actual, _unb64(expected))
    except (ValueError, TypeError):
        return False


def create_access_token(participant_id: int) -> str:
    now = int(_now().timestamp())
    header = _b64_json({"alg": "HS256", "typ": "JWT"})
    payload = _b64_json({"sub": str(participant_id), "iat": now, "exp": now + ACCESS_TOKEN_TTL_SEC})
    return f"{header}.{payload}.{_sign(header, payload)}"


def decode_access_token(token: str) -> int:
    try:
        header, payload, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(header, payload)):
            raise ValueError
        data = json.loads(_unb64(payload))
        if int(data["exp"]) <= int(_now().timestamp()):
            raise ValueError
        return int(data["sub"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise ApiError(401, "INVALID_ACCESS_TOKEN", "로그인이 필요합니다.") from None


def record_training_consent(participant: Participant, *, now: datetime | None = None) -> None:
    now = now or _now()
    participant.privacy_agreed = True
    participant.surprise_call_agreed = True
    if participant.consented_at is None:
        participant.consented_at = now


def _auth_response(participant: Participant) -> AuthResponse:
    return AuthResponse(
        accessToken=create_access_token(participant.id),
        expiresInSec=ACCESS_TOKEN_TTL_SEC,
        participant=AuthParticipant(
            id=participant.id,
            phoneNumberMasked=mask_phone_number(participant.phone_number),
            hasConsented=participant.has_training_consent(),
        ),
    )


def _validate_password(password: str) -> None:
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ApiError(400, "WEAK_PASSWORD", "비밀번호는 영문과 숫자를 포함해 8자 이상이어야 합니다.")


def _sign(header: str, payload: str) -> str:
    return _b64(hmac.new(_jwt_secret(), f"{header}.{payload}".encode(), hashlib.sha256).digest())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64_json(value: dict) -> str:
    return _b64(json.dumps(value, separators=(",", ":")).encode())


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _jwt_secret() -> bytes:
    return os.getenv("JWT_SECRET", "local-development-secret-change-me").encode()


def _now() -> datetime:
    return datetime.now(timezone.utc)
