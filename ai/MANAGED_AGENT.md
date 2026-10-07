# 매니지드 에이전트 운영

훈련 전화를 거는 ClawOps 매니지드 에이전트를 만들고, 백엔드 없이 시험 전화를 걸고, 녹취록을 확인하는 방법입니다.
코드: `ai/managed_agent.py`, `ai/transcript.py`, `ai/harness.py`의 `audit_transcript`.

## 준비

`backend/.env`에 아래 값이 있어야 합니다. `ai/.env`는 만들지 마십시오.

| 변수 | 용도 |
| --- | --- |
| `CLAWOPS_API_KEY`, `CLAWOPS_ACCOUNT_ID` | 에이전트 생성, 발신, 녹취록 조회 |
| `CLAWOPS_PHONE_NUMBER` | 발신 번호 (`--from`으로 대신 지정 가능) |
| `MANAGED_AGENT_REALTIME_MODEL` (선택) | `external_tts`의 모델을 모든 시나리오에 강제로 지정. 없으면 `REALTIME_MODEL`(`gpt-realtime-2.1`) |
| `MANAGED_AGENT_LIVE_BACKEND` (선택) | `live`의 업무 모델. 기본 `gpt-5.6-luna` |

## 1. 에이전트 만들기

```bash
python -m ai.managed_agent sync --dry-run    # 보낼 내용만 확인
python -m ai.managed_agent sync --variant external_tts   # 시나리오 5개 생성/갱신 (live는 --variant live)
```

이름 규칙은 `spc-<시나리오 id>-<방식>`입니다. 백엔드는 이 이름으로 에이전트를 찾습니다.
에이전트에는 공통 규칙(안전, 말투, 전화 규칙)만 들어갑니다. 시나리오는 통화마다 CallContext로 보내므로,
**시나리오를 고쳐도 다시 sync할 필요가 없습니다.** 목소리, 모델, 공통 규칙을 바꿨을 때만 sync합니다.
콘솔에서 에이전트를 손으로 고치지 마십시오. 다음 sync에서 덮어씁니다. 예외는 API에 없는 [콘솔 전용 설정](#콘솔-전용-설정)입니다.

## 2. 시험 전화

```bash
python -m ai.managed_agent context --scenario investigation_unit      # 통화별 지시문 확인 (4,000자 한도)
python -m ai.managed_agent call --to 010XXXXXXXX --scenario investigation_unit --wait
```

`--wait`을 주면 통화가 끝날 때까지 상태를 조회하고, 끝나면 녹취록을 요청합니다. `live` 방식은 `--variant live`를 붙입니다.

## 3. 녹취록 확인

```bash
python -m ai.transcript show <callId> --scenario investigation_unit --summary
```

- 상담원/훈련자로 나눈 대화, 화자 판정 결과, 안전 감사 결과를 출력합니다.
- `agent_speaker=None`이면 화자를 판정하지 못한 것입니다. 원본 화자 표기 그대로 출력되니 판정 규칙 보강에 쓰십시오.

## 4. 시험 전화에서 확인할 것

| 항목 | 기준 |
| --- | --- |
| 첫 마디 | [첫 마디]를 그대로 말하고 상대 대답을 기다리는가 |
| 사건 유지 | 시각, 금액, 기관명이 통화 내내 같은가 |
| 목소리 | 배역(나이, 성별, 태도)과 맞는가 |
| 끼어들기 | 0.9초보다 짧은 소리(맞장구, 기침)에는 이어 말하고, 그보다 길게 끼어들면 멈추고 답하는가 |
| 거절·끊기 | 거절하면 포기 경고를 하고, 또 거절하면 말없이 끊는가. 처음 끊겠다고 하면 [끊으려 할 때 경고]를 하고(포기 경고를 이미 했으면 바로 끊음), 두 번째에는 말없이 끊는가. "잠깐만요", 의심, 신고 말에는 끊지 않는가 |
| 안전 감사 | `audit_transcript` 결과가 비었는가 (실명 기관, 비밀정보 요구, 역할 이탈) |
| 화자 판정 | `agent_speaker`가 맞았는가 |

## 5. 조정할 수 있는 곳

| 무엇 | 어디 | 반영 |
| --- | --- | --- |
| 목소리 배정 | `CARTESIA_VOICES`, `LIVE_VOICES`, `CARTESIA_SPEED` | sync 필요 |
| 모델 | `REALTIME_MODEL` | sync 필요 |
| 끼어들기·음성 감지 기준 | `agent_payload`의 `session`, `vad` | sync 필요 |
| 공통 전화 규칙 | `_PHONE_RULES` | sync 필요 |
| 시나리오 내용 | `ai/scenarios/library.py` | 바로 반영 (sync 불필요) |
| 화자 판정 | `ai/transcript.py` (`_MIN_OPENING_MATCH`, `_MIN_MARGIN`) | 바로 반영 |

### 콘솔 전용 설정

API에 없어 sync로 관리되지 않는 값입니다. sync해도 지워지지 않습니다.
에이전트를 새로 만들면(`live` 포함) 콘솔의 대화 탭 > 통화 흐름에서 직접 맞춥니다. 지금 `external_tts` 에이전트 5개에는 적용돼 있습니다.

| 설정 | 값 | 이유 |
| --- | --- | --- |
| 에이전트가 먼저 인사하기 | 켜짐 | |
| 첫 인사는 끝까지 말하기 | 켜짐 | 받는 사람의 "여보세요?"에 첫 마디가 끊기지 않게 |
| 무응답 처리 | 켜짐 | 말이 없는 통화는 콘솔이 끝낸다. 전화 규칙은 침묵으로 끊지 않는다 |
| └ 조용할 때 기다리는 시간 | 6초 | 3초면 생각하는 사이에 "들리십니까?"를 끼워 넣는다 |
| └ 다시 여쭤보는 횟수 | 2회 | 1회면 무음 뒤 "여보세요?"가 나오기도 전에 통화가 끊긴다 |
| └ 예시 문구 | "여보세요?" | |
| 키패드 | 꺼짐 | |

## 6. 백엔드가 쓰는 것

백엔드는 `CALL_AGENT_VARIANT`(기본 `external_tts`, `live`, `ab`=통화마다 무작위)로 방식을 고릅니다.
`ai/`를 고칠 때 아래 이름은 바꾸지 않습니다.

| 백엔드 파일 | `ai/`에서 가져다 쓰는 것 |
| --- | --- |
| `services/call_service.py` | `pick_variant`, `resolve_agent_id`, `build_call_context`, `link_for` |
| `services/report_service.py` | `identify_agent_speaker`, `audit_transcript`, `wants_hang_up`, `get_scenario`, `RISK_LABELS`, `DEFENSE_LABELS`, `GEMINI_OPENAI_BASE_URL` |
| `training/scenarios.py` | `get_scenario`, `pick_scenario` |

테스트: `python -m pytest ai/tests`
