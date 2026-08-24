import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from hmac import compare_digest
from math import ceil
from secrets import randbelow
from threading import Lock

from app.errors import ApiError
from app.schemas.consent import RequestOtpResponse, SessionResponse
from app.services.call_service import start_training_calls
from app.services.octomo_service import message_exists, send_to_number
from app.services.rate_limit import SlidingWindowLimiter
from app.services.session_service import (
    confirm_verified_phone,
    mask_phone_number,
    session_exists,
)


logger = logging.getLogger(__name__)

OTP_TTL_SEC = 300
OTP_RESEND_COOLDOWN_SEC = 60
OTP_MAX_SENDS_PER_SESSION_PHONE = 5
OTP_MAX_FAILS = 5
OTP_SEND_LIMIT_PER_PHONE_HOUR = 8
OTP_SEND_LIMIT_PER_IP_HOUR = 20
OTP_SEND_LIMIT_PER_SESSION_HOUR = 10
OTP_VERIFY_LIMIT_PER_IP_HOUR = 60
HOUR_SEC = 3600
PHONE_PATTERN = re.compile(r"010\d{8}")
CODE_PATTERN = re.compile(r"\d{6}")


@dataclass
class OtpChallenge:
    session_id: str
    phone_number: str
    code: str
    code_hash: str
    expires_at: datetime
    last_sent_at: datetime
    send_count: int
    fail_count: int
    locked: bool
    used: bool


_challenges: dict[str, OtpChallenge] = {}
_send_counts: dict[tuple[str, str], int] = {}
_phone_last_sent: dict[str, datetime] = {}
_lock = Lock()
_send_limiter = SlidingWindowLimiter()
_verify_limiter = SlidingWindowLimiter()


def request_otp(session_id: str, phone_number: str, client_ip: str) -> RequestOtpResponse:
    digits = _normalize_phone(phone_number)
    now = _now()

    if not session_exists(session_id):
        raise ApiError(404, "SESSION_NOT_FOUND", "세션을 찾을 수 없습니다.")

    with _lock:
        send_key = (session_id, digits)
        send_count = _send_counts.get(send_key, 0)
        if send_count >= OTP_MAX_SENDS_PER_SESSION_PHONE:
            raise ApiError(429, "OTP_RATE_LIMITED", "인증번호 요청 횟수를 초과했습니다.")

        remaining_cooldown = _cooldown_remaining(session_id, digits, now)
        if remaining_cooldown > 0:
            raise ApiError(
                429,
                "OTP_COOLDOWN",
                f"인증번호는 {remaining_cooldown}초 후 다시 요청할 수 있습니다.",
            )

        _assert_send_abuse_limits(session_id, digits, client_ip, now)

        code = f"{randbelow(1_000_000):06d}"
        challenge = OtpChallenge(
            session_id=session_id,
            phone_number=digits,
            code=code,
            code_hash=_hash_code(session_id, digits, code),
            expires_at=now + timedelta(seconds=OTP_TTL_SEC),
            last_sent_at=now,
            send_count=send_count + 1,
            fail_count=0,
            locked=False,
            used=False,
        )
        _send_counts[send_key] = challenge.send_count
        _phone_last_sent[digits] = now
        _challenges[session_id] = challenge

    return RequestOtpResponse(
        phoneNumberMasked=mask_phone_number(digits),
        code=code,
        sendToNumber=send_to_number(),
        expiresInSec=OTP_TTL_SEC,
        resendAvailableInSec=OTP_RESEND_COOLDOWN_SEC,
    )


def verify_otp(session_id: str, phone_number: str, code: str, client_ip: str) -> SessionResponse:
    digits = _normalize_phone(phone_number)
    now = _now()

    if not session_exists(session_id):
        raise ApiError(404, "SESSION_NOT_FOUND", "세션을 찾을 수 없습니다.")

    if not _verify_limiter.allow(f"ip:{client_ip}", OTP_VERIFY_LIMIT_PER_IP_HOUR, HOUR_SEC, now):
        raise ApiError(429, "OTP_RATE_LIMITED", "인증번호 요청 횟수를 초과했습니다.")

    with _lock:
        challenge = _challenges.get(session_id)
        if challenge is None or challenge.used:
            raise ApiError(400, "OTP_NOT_REQUESTED", "인증번호를 먼저 요청해 주세요.")

        if challenge.phone_number != digits:
            raise ApiError(
                400,
                "OTP_PHONE_MISMATCH",
                "인증번호를 받은 번호와 일치하지 않습니다.",
            )

        if challenge.locked:
            raise ApiError(
                429,
                "OTP_LOCKED",
                "인증 시도 횟수를 초과했습니다. 인증번호를 다시 받아 주세요.",
            )

        if now >= challenge.expires_at:
            raise ApiError(
                400,
                "OTP_EXPIRED",
                "인증번호가 만료되었습니다. 다시 받아 주세요.",
            )

        if not _code_matches(challenge, code):
            challenge.fail_count += 1
            remaining = OTP_MAX_FAILS - challenge.fail_count
            if remaining <= 0:
                challenge.locked = True
                raise ApiError(
                    429,
                    "OTP_LOCKED",
                    "인증 시도 횟수를 초과했습니다. 인증코드를 다시 받아 주세요.",
                )
            raise ApiError(
                400,
                "OTP_INVALID",
                f"인증번호가 올바르지 않습니다. ({remaining}회 남음)",
            )

        stored_code = challenge.code

    if not message_exists(digits, stored_code, within_minutes=max(1, OTP_TTL_SEC // 60)):
        raise ApiError(
            400,
            "OTP_NOT_RECEIVED",
            "문자가 아직 확인되지 않았습니다. 보낸 뒤 전송 완료를 다시 눌러 주세요.",
        )

    with _lock:
        challenge = _challenges.get(session_id)
        if challenge is None or challenge.used or challenge.phone_number != digits:
            raise ApiError(400, "OTP_NOT_REQUESTED", "인증번호를 먼저 요청해 주세요.")
        challenge.used = True

    session = confirm_verified_phone(session_id, digits)
    if session is None:
        raise ApiError(404, "SESSION_NOT_FOUND", "세션을 찾을 수 없습니다.")

    try:
        start_training_calls(session_id, digits)
    except Exception:
        logger.exception("Failed to start training calls after OTP verify")
    return session


def reset_otp_state() -> None:
    with _lock:
        _challenges.clear()
        _send_counts.clear()
        _phone_last_sent.clear()
    _send_limiter.clear()
    _verify_limiter.clear()


def _assert_send_abuse_limits(session_id: str, phone_number: str, client_ip: str, now: datetime) -> None:
    checks = (
        (f"ip:{client_ip}", OTP_SEND_LIMIT_PER_IP_HOUR),
        (f"phone:{phone_number}", OTP_SEND_LIMIT_PER_PHONE_HOUR),
        (f"session:{session_id}", OTP_SEND_LIMIT_PER_SESSION_HOUR),
    )
    for key, limit in checks:
        if not _send_limiter.is_allowed(key, limit, HOUR_SEC, now):
            raise ApiError(429, "OTP_RATE_LIMITED", "인증번호 요청 횟수를 초과했습니다.")
    for key, _limit in checks:
        _send_limiter.record(key, now)


def _cooldown_remaining(session_id: str, phone_number: str, now: datetime) -> int:
    last_sent_times: list[datetime] = []
    current = _challenges.get(session_id)
    if current is not None and current.phone_number == phone_number:
        last_sent_times.append(current.last_sent_at)
    phone_sent = _phone_last_sent.get(phone_number)
    if phone_sent is not None:
        last_sent_times.append(phone_sent)

    if not last_sent_times:
        return 0

    last_sent = max(last_sent_times)
    ready_at = last_sent + timedelta(seconds=OTP_RESEND_COOLDOWN_SEC)
    remaining = (ready_at - now).total_seconds()
    if remaining <= 0:
        return 0
    return max(1, ceil(remaining))


def _normalize_phone(phone_number: str) -> str:
    digits = re.sub(r"\D", "", phone_number)
    if not PHONE_PATTERN.fullmatch(digits):
        raise ApiError(400, "INVALID_PHONE", "010으로 시작하는 휴대전화번호 11자리를 입력해 주세요.")
    return digits


def _hash_code(session_id: str, phone_number: str, code: str) -> str:
    return sha256(f"{session_id}:{phone_number}:{code}".encode()).hexdigest()


def _code_matches(challenge: OtpChallenge, code: str) -> bool:
    normalized = code.strip()
    if not CODE_PATTERN.fullmatch(normalized):
        return False
    expected = _hash_code(challenge.session_id, challenge.phone_number, normalized)
    return compare_digest(challenge.code_hash, expected)


def _now() -> datetime:
    return datetime.now(timezone.utc)
