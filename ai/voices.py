"""Korean voices available on this ElevenLabs account.

Theo is the default library voice. For a cloned caller, set
ELEVENLABS_VOICE_ID to the Instant Voice Clone id from the ElevenLabs dashboard.
Lower stability (~0.35–0.45) sounds more human; 0.7+ sounds like a robot.
"""

from __future__ import annotations
from secrets import choice

THEO = "CxErO97xpQgQXYmapDKX"
YOHAN_KOO = "4JJwo477JUAx3HV0T7n7"
Kelee_K = "5DWGv3VDkihNUcbvaonB"
Hanna = "zgDzx5jLLCqEp6Fl7Kl7"
Onyu = "NaQdbkW5gNZD8wfwXeTV"
Zara = "jqcCZkN6Knx8BJ5TBdYR"

AVAILABLE_VOICE_IDS = (
    THEO,
    YOHAN_KOO,
    Kelee_K,
)


def random_voice_id() -> str:
    return choice(AVAILABLE_VOICE_IDS)
