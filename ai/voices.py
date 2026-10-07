"""시나리오별 ElevenLabs 목소리 id(Playbook.tts_voice_id).

서버가 직접 음성을 처리하던 때의 값이다. 지금 통화 목소리는 managed_agent.CARTESIA_VOICES가 정한다.
"""

THEO = "CxErO97xpQgQXYmapDKX"
YOHAN_KOO = "4JJwo477JUAx3HV0T7n7"
Kelee_K = "5DWGv3VDkihNUcbvaonB"
Onyu = "NaQdbkW5gNZD8wfwXeTV"

# 이 계정에서 실제로 소리가 나는 목소리. 유료 라이브러리 목소리는 402로 무음이 된다.
WORKING_VOICE_IDS = (THEO, YOHAN_KOO, Kelee_K, Onyu)
