# 매니지드 에이전트 PoC 실행 절차

훈련 전화를 ClawOps 매니지드 에이전트로 돌리는 방식(`external_tts`, `live`)을 **백엔드 없이** 시험하는 절차입니다.
코드: `ai/managed_agent.py`, `ai/transcript.py`, `ai/harness.py`의 `audit_transcript`.

## 준비

`backend/.env`에 아래 값이 있어야 합니다. `ai/.env`는 만들지 마십시오.

| 변수 | 용도 |
| --- | --- |
| `CLAWOPS_API_KEY`, `CLAWOPS_ACCOUNT_ID` | 에이전트 생성, 발신, 녹취록 조회 |
| `CLAWOPS_PHONE_NUMBER` | 발신 번호 (`--from`으로 대신 지정 가능) |
| `MANAGED_AGENT_REALTIME_MODEL` (선택) | `external_tts`의 두뇌. 기본 `gpt-realtime-2.1-mini`, 품질 우선이면 `gpt-realtime-2.1` |
| `MANAGED_AGENT_LIVE_BACKEND` (선택) | `live`의 업무 모델. 기본 `gpt-5.6-luna` |

## 1. 에이전트 만들기

```bash
python -m ai.managed_agent sync --dry-run    # 보낼 내용만 확인
python -m ai.managed_agent sync              # 시나리오 5 x 방식 2 = 10개 생성/갱신
```

이름 규칙은 `spc-<시나리오 id>-<방식>`입니다. 백엔드는 이 이름으로 에이전트를 찾습니다.
에이전트에는 공통 규칙(안전, 말투, 전화 규칙)만 들어갑니다. 시나리오는 통화마다 CallContext로 보내므로,
**시나리오를 고쳐도 다시 sync할 필요가 없습니다.** 목소리, 모델, 공통 규칙을 바꿨을 때만 sync합니다.
콘솔에서 에이전트를 손으로 고치지 마십시오. 다음 sync에서 덮어씁니다.

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
- `agent_speaker=None`이면 화자를 판정하지 못한 것입니다. 원본 화자 표기 그대로 출력되니 판정 규칙 보강에 쓰십시오.

## 4. 평가표 (통화마다 기록)

| 항목 | 기준 |
| --- | --- |
| 체감 지연 | 말을 마치고 상담원이 답하기까지. 1초 안쪽이면 합격 |
| 목소리 | 사람 같은가, 배역(나이, 성별, 태도)과 맞는가 |
| 맞장구 | "네", "음"에 말을 멈추지 않는가 |
| 끼어들기 | "잠깐만요"에 바로 멈추고 답하는가 |
| 첫 대사 | [첫 마디]를 그대로 말했는가 |
| 사건 유지 | 시각, 금액, 기관명이 통화 내내 같은가 |
| 끊기 규칙 | 첫 번째는 붙잡고, 두 번째에 마지막 한마디 후 끊는가 |
| 안전 감사 | `audit_transcript` 결과 (실명 기관, 비밀정보 요구, 역할 이탈) |
| 화자 판정 | `agent_speaker`가 맞았는가 |

시나리오 5편 x 방식 2개를 테스터 3~5명이 받아 보고, 방식별로 합산해 비교합니다.

## 5. 조정할 수 있는 곳

| 무엇 | 어디 | 반영 |
| --- | --- | --- |
| 목소리 배정 | `CARTESIA_VOICES`, `LIVE_VOICES`, `CARTESIA_SPEED` | sync 필요 |
| 공통 전화 규칙 | `_PHONE_RULES` | sync 필요 |
| 시나리오 내용 | `ai/scenarios/library.py` | 바로 반영 (sync 불필요) |
| 화자 판정 | `ai/transcript.py` (`_MIN_OPENING_MATCH`, `_MIN_MARGIN`) | 바로 반영 |

## 6. 결정 후

`CALL_AGENT_VARIANT`(`external_tts` / `live` / `ab`)를 정해 백엔드에 전달하고, 백엔드 2단계(B5~B9)를 시작합니다.
백엔드가 쓰는 함수: `pick_variant`, `resolve_agent_id`, `build_call_context`, `identify_agent_speaker`, `label_roles`, `audit_transcript`.

테스트: `python -m pytest ai/tests`
