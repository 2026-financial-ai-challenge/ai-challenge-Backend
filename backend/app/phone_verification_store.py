"""Redis-backed signup verification state.

A challenge and the token it earns only matter for minutes, so Redis holds
them under a TTL instead of Postgres holding them forever. Two things follow
from that and the callers below depend on both:

- There is no cleanup job to forget. The old phone_verifications table kept
  every phone number that ever requested a code, including people who never
  finished signing up, because nothing ever deleted the rows.
- A lapsed challenge is simply absent. Expiry is not a timestamp anyone
  compares against; the key is gone, so "expired" and "never requested" are
  the same answer here.
"""

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
    # Signup already succeeded and is committed by the time we get here, so a
    # Redis outage must not turn a finished signup into a 500.
    try:
        _client().set(f"{KEY_PREFIX}{participant_id}", when.isoformat())
    except redis.RedisError:
        logger.exception("Failed to record phone verification for %s", participant_id)


def claim_send_slot(phone: str, cooldown_sec: int) -> bool:
    """Reserve the right to send this phone a code, or return False.

    The key's own TTL is the resend window, so there is no "when was the last
    one" timestamp to read back -- and nothing to keep once it lapses.
    """
    return bool(
        _client().set(f"{_COOLDOWN_PREFIX}{phone}", "1", nx=True, ex=cooldown_sec)
    )


def release_send_slot(phone: str) -> None:
    """Hand the slot back, so a send that failed does not burn the cooldown."""
    _client().delete(f"{_COOLDOWN_PREFIX}{phone}")


def store_challenge(phone: str, code_hash: str, ttl_sec: int) -> None:
    """Replace any challenge for this phone with a fresh one.

    Replace rather than update: a newly sent code deserves the full attempt
    budget, and the previous challenge may already have failures against it.
    """
    key = f"{_CODE_PREFIX}{phone}"
    pipe = _client().pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping={"code_hash": code_hash, "fail_count": 0})
    pipe.expire(key, ttl_sec)
    pipe.execute()


def read_challenge(phone: str) -> dict[str, str] | None:
    return _client().hgetall(f"{_CODE_PREFIX}{phone}") or None


def count_failure(phone: str) -> int:
    """Record a wrong code and return the new failure count.

    HINCRBY is atomic, which is what the row lock used to buy: two requests
    racing on the same challenge cannot both read the same count and overwrite
    each other's increment.
    """
    return _client().hincrby(f"{_CODE_PREFIX}{phone}", "fail_count", 1)


def discard_challenge(phone: str) -> None:
    _client().delete(f"{_CODE_PREFIX}{phone}")


def issue_token(phone: str, token_hash: str, ttl_sec: int) -> None:
    """Trade a solved challenge for a token, and retire the challenge."""
    pipe = _client().pipeline()
    pipe.set(f"{_TOKEN_PREFIX}{token_hash}", phone, ex=ttl_sec)
    pipe.delete(f"{_CODE_PREFIX}{phone}")
    pipe.execute()


def consume_token(token_hash: str, *, spent_ttl_sec: int) -> str | None:
    """The phone this token verified, or None. Spends the token.

    Read and delete are one command, which is what makes the token single use:
    two signups racing on the same token cannot both be handed a phone number.
    The cost is that a signup which fails after this point cannot retry on the
    same token -- acceptable, because the realistic failure is "already
    registered", where another attempt would fail the same way.

    A spent token leaves a marker behind so a replay can be reported as such
    rather than as a token that never existed.
    """
    phone = _client().getdel(f"{_TOKEN_PREFIX}{token_hash}")
    if phone is not None:
        _client().set(f"{_SPENT_PREFIX}{token_hash}", "1", ex=spent_ttl_sec)
    return phone


def token_was_spent(token_hash: str) -> bool:
    return bool(_client().exists(f"{_SPENT_PREFIX}{token_hash}"))


def reset_signup_state() -> None:
    """Drop every challenge, cooldown and token. For tests."""
    client = _client()
    for prefix in _SIGNUP_PREFIXES:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
