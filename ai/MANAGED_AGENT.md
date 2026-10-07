# 매니지드 에이전트 운영 절차

에이전트를 만들고 갱신하는 방법과, 백엔드 없이 시험 통화를 거는 방법입니다.
코드: `ai/managed_agent.py`, `ai/transcript.py`, `ai/harness.py`의 `audit_transcript`.

## 준비

`backend/.env`에 아래 값이 있어야 합니다. `ai/.env`는 만들지 마십시오.

| 변수 | 용도 |
| --- | --- |
| `CLAWOPS_API_KEY`, `CLAWOPS_ACCOUNT_ID` | 에이전트 생성, 발신, 녹취록 조회 |
| `CLAWOPS_PHONE_NUMBER` | 발신 번호 (`--from`으로 대신 지정 가능) |
| `MANAGED_AGENT_REALTIME_MODEL` (선택) | `external_tts` 모델 강제 지정. 없으면 `REALTIME_MODEL`(`gpt-realtime-2.1`) |
| `MANAGED_AGENT_LIVE_BACKEND` (선택) | `live`의 업무 모델. 기본 `gpt-5.6-luna` |

## 1. 에이전트 만들기

```bash
python -m ai.managed_agent sync --dry-run    # 보낼 내용만 확인
python -m ai.managed_agent sync              # external_tts 5개 생성/갱신 (live는 --variant live로 따로)
```

이름 규칙은 `spc-<시나리오 id>-<방식>`입니다. 백엔드는 이 이름으로 에이전트를 찾습니다.
에이전트에는 공통 규칙(안전, 말투, 전화 규칙)만 들어가고 시나리오는 통화마다 CallContext로 보냅니다.
목소리, 모델, 공통 규칙을 바꿨을 때만 sync하면 됩니다.
콘솔에서 에이전트를 직접 고치면 다음 sync 때 덮어써집니다. API에 없는 [콘솔 전용 설정](#콘솔-전용-설정)만 예외입니다.

## 2. 테스트 전화

```bash
python -m ai.managed_agent context --scenario investigation_unit      # 통화별 지시문 확인 (4,000자 한도)
python -m ai.managed_agent call --to 010XXXXXXXX --scenario investigation_unit --variant external_tts --wait
python -m ai.managed_agent call --to 010XXXXXXXX --scenario investigation_unit --variant live --wait
```

`--wait`을 주면 통화가 끝날 때까지 상태를 조회하고, 끝나면 녹취록을 요청합니다.

## 3. 녹취록 확인

```bash
python -m ai.transcript show <callId> --scenario investigation_unit --summary
```

- 상담원/훈련자로 나눈 대화, 화자 판정 결과, 안전 감사 결과를 출력합니다.
- `agent_speaker=None`이면 판정 실패입니다. 원본 화자 표기로 출력됩니다.

## 4. 시험 통화 확인 항목

| 항목 | 기준 |
| --- | --- |
| 체감 지연 | 말을 마치고 상담원이 답하기까지. 1초 안쪽이면 합격 |
| 목소리 | 사람 같은가, 배역(나이, 성별, 태도)과 맞는가 |
| 맞장구 | "네", "음"에 말을 멈추지 않는가 |
| 끼어들기 | "잠깐만요"에 바로 멈추고 답하는가 |
| 첫 대사 | [첫 마디]를 그대로 말했는가 |
| 사건 유지 | 시각, 금액, 기관명이 통화 내내 같은가 |
| 끊기 규칙 | 첫 "끊을게요"에 [끊으려 할 때 경고], 두 번째에 말없이 종료하는가. 거절에 포기 경고, 다음 거절에 종료하는가(의심·신고는 거절 아님) |
| 안전 감사 | `audit_transcript` 결과 (실명 기관, 비밀정보 요구, 역할 이탈) |
| 화자 판정 | `agent_speaker`가 맞았는가 |

## 5. 조정할 수 있는 곳

| 무엇 | 어디 | 반영 |
| --- | --- | --- |
| 목소리 배정 | `CARTESIA_VOICES`, `LIVE_VOICES` | sync 필요 |
| 모델 | `REALTIME_MODEL` | sync 필요 |
| 공통 전화 규칙 | `_PHONE_RULES` | sync 필요 |
| 시나리오 내용 | `ai/scenarios/library.py` | 백엔드 배포 후 반영 (sync 불필요) |
| 화자 판정 | `ai/transcript.py` (`_MIN_OPENING_MATCH`, `_MIN_MARGIN`) | 백엔드 배포 후 반영 |

### 콘솔 전용 설정

API에 없어서 sync로 관리되지 않고, sync해도 지워지지 않습니다. 에이전트를 새로 만들면 콘솔(대화 탭 > 통화 흐름)에서 직접 맞춥니다.
현재 `external_tts` 에이전트 5개에 적용돼 있습니다.

| 설정 | 값 | 이유 |
| --- | --- | --- |
| 에이전트가 먼저 인사하기 | 켜짐 | |
| 첫 인사는 끝까지 말하기 | 켜짐 | 받는 사람의 "여보세요?"에 첫 마디가 끊기지 않게 |
| 무응답 처리 | 켜짐 | 끄지 않음 |
| └ 조용할 때 기다리는 시간 | 6초 | 3초면 상대가 생각하는 중에 "들리십니까?"가 끼어듦 |
| └ 다시 여쭤보는 횟수 | 2회 | 1회면 다시 묻는 말이 재생되기 전에 통화가 끊김 |
| └ 예시 문구 | "여보세요?" | |
| 키패드 | 꺼짐 | |

## 6. 백엔드 연결

- 발신: `pick_variant`, `resolve_agent_id`, `build_call_context` (`call_service.py`)
- 리포트: `identify_agent_speaker`, `audit_transcript` (`report_service.py`)
- 방식은 `CALL_AGENT_VARIANT`(`external_tts` / `live` / `ab`)로 고릅니다. 기본은 `external_tts`입니다.

테스트: `python -m pytest ai/tests`
