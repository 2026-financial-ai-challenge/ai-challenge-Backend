"""Redis-backed record of when each participant's phone was verified."""

import logging
import os
from datetime import datetime
from functools import lru_cache

import redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "phone_verified_at:"


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
