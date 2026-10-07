"""Playbook.tts_voice_id용 ElevenLabs 목소리 id.

예전 서버 음성 처리 경로에서 쓰던 값. 현재 통화 목소리는 managed_agent.CARTESIA_VOICES.
"""

THEO = "CxErO97xpQgQXYmapDKX"
YOHAN_KOO = "4JJwo477JUAx3HV0T7n7"
Kelee_K = "5DWGv3VDkihNUcbvaonB"
Onyu = "NaQdbkW5gNZD8wfwXeTV"

# 이 계정에서 합성되는 목소리. 유료 라이브러리 목소리는 402가 나서 무음이 된다.
WORKING_VOICE_IDS = (THEO, YOHAN_KOO, Kelee_K, Onyu)
