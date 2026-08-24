import base64
import hashlib
import hmac
import os
from threading import Lock


_transcript_events: dict[tuple[str, str], dict[str, str]] = {}
_transcript_events_lock = Lock()


def verify_clawops_signature(
    *,
    url: str,
    params: dict[str, str],
    signature: str | None,
) -> bool:
    signing_secret = os.getenv("CLAWOPS_WEBHOOK_SIGNING_SECRET", "").strip()
    if not signing_secret:
        return True
    if not signature:
        return False

    data = url + "".join(
        f"{key}{value}" for key, value in sorted(params.items())
    )
    expected = base64.b64encode(
        hmac.new(
            signing_secret.encode(),
            data.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    return hmac.compare_digest(signature, expected)


def save_transcript_event(params: dict[str, str]) -> None:
    key = (params["CallId"], params["Event"])
    with _transcript_events_lock:
        _transcript_events[key] = params.copy()
