"""회원가입 휴대폰 인증 상태를 Redis에 TTL로 저장한다. 만료된 값은 키가 사라져 별도 정리 작업이 필요 없다."""

import logging
import os
from datetime import datetime
from functools import lru_cache

import redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "phone_verified_at:"
_CODE_PREFIX = "otp:code:"
_COOLDOWN_PREFIX = "otp:cooldown:"
_TOKEN_PREFIX = "signup:token:"
_SPENT_PREFIX = "signup:spent:"
_SIGNUP_PREFIXES = (_CODE_PREFIX, _COOLDOWN_PREFIX, _TOKEN_PREFIX, _SPENT_PREFIX)


@lru_cache
def _client() -> redis.Redis:
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    return redis.Redis.from_url(url, decode_responses=True)


def mark_phone_verified(participant_id: int, when: datetime) -> None:
    # 가입은 이미 커밋된 뒤라, Redis 장애로 가입 응답을 실패시키지 않는다.
    try:
        _client().set(f"{KEY_PREFIX}{participant_id}", when.isoformat())
    except redis.RedisError:
        logger.exception("Failed to record phone verification for %s", participant_id)


def claim_send_slot(phone: str, cooldown_sec: int) -> bool:
    """재발송 대기 시간 동안 한 번만 발송을 허용한다. 이미 대기 중이면 False."""
    return bool(
        _client().set(f"{_COOLDOWN_PREFIX}{phone}", "1", nx=True, ex=cooldown_sec)
    )


def release_send_slot(phone: str) -> None:
    _client().delete(f"{_COOLDOWN_PREFIX}{phone}")


def store_challenge(phone: str, code_hash: str, ttl_sec: int) -> None:
    """새 인증번호로 교체하고 실패 횟수를 0부터 다시 센다."""
    key = f"{_CODE_PREFIX}{phone}"
    pipe = _client().pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping={"code_hash": code_hash, "fail_count": 0})
    pipe.expire(key, ttl_sec)
    pipe.execute()


def read_challenge(phone: str) -> dict[str, str] | None:
    return _client().hgetall(f"{_CODE_PREFIX}{phone}") or None


def count_failure(phone: str) -> int:
    return _client().hincrby(f"{_CODE_PREFIX}{phone}", "fail_count", 1)


def discard_challenge(phone: str) -> None:
    _client().delete(f"{_CODE_PREFIX}{phone}")


def issue_token(phone: str, token_hash: str, ttl_sec: int) -> None:
    pipe = _client().pipeline()
    pipe.set(f"{_TOKEN_PREFIX}{token_hash}", phone, ex=ttl_sec)
    pipe.delete(f"{_CODE_PREFIX}{phone}")
    pipe.execute()


def consume_token(token_hash: str, *, spent_ttl_sec: int) -> str | None:
    """토큰이 인증한 전화번호를 돌려주고 토큰을 소모한다.

    읽기와 삭제를 한 명령(GETDEL)으로 해 동시 요청에도 한 번만 쓰이게 한다.
    재사용 시도를 구분하도록 소모 표시를 남긴다.
    """
    phone = _client().getdel(f"{_TOKEN_PREFIX}{token_hash}")
    if phone is not None:
        _client().set(f"{_SPENT_PREFIX}{token_hash}", "1", ex=spent_ttl_sec)
    return phone


def token_was_spent(token_hash: str) -> bool:
    return bool(_client().exists(f"{_SPENT_PREFIX}{token_hash}"))


def reset_signup_state() -> None:
    """테스트용: 모든 인증 상태를 지운다."""
    client = _client()
    for prefix in _SIGNUP_PREFIXES:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
