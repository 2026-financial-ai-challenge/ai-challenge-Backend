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

# Verified by synthesizing a Korean line against this account: THEO,
# YOHAN_KOO, Kelee_K and Onyu return audio; Hanna and Zara return nothing at
# all (Voice Library voices need a paid plan -- the API answers 402 and the
# stream closes with zero bytes, which on a call is total silence, not a
# degraded voice). Only assign a scenario a voice from this tuple.
WORKING_VOICE_IDS = (
    THEO,
    YOHAN_KOO,
    Kelee_K,
    Onyu,
)

AVAILABLE_VOICE_IDS = (
    THEO,
    YOHAN_KOO,
    Kelee_K,
)


def random_voice_id() -> str:
    return choice(AVAILABLE_VOICE_IDS)
