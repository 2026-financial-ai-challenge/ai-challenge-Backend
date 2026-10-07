# ai: 훈련 통화 시나리오, 에이전트 규칙, 채점 기준

훈련 통화에서 AI가 할 말(시나리오, 에이전트 규칙)과 통화 후 채점 기준을 담은 패키지입니다.
통화 자체는 ClawOps 매니지드 에이전트가 진행하고, 백엔드는 이 패키지로 시나리오·에이전트를 고르고 통화별 지시문을 만들어 발신합니다.
백엔드에서는 `backend/app/training/scenarios.py`의 `ensure_ai_importable()`로 import합니다(저장소 루트 또는 Docker의 `/packages`).

## 통화 한 건의 흐름

```
[발신] backend/app/services/call_service.py
  get_runtime_scenario()                시나리오 고르기 (ai/scenarios)
  pick_variant(), resolve_agent_id()    에이전트 spc-<시나리오 id>-<방식> 찾기 (ai/managed_agent.py)
  build_call_context()                  통화별 지시문 만들기 (4,000자 이내)
  ClawOps calls.create                  이후 대화는 ClawOps가 진행
                                        (OpenAI Realtime gpt-realtime-2.1 + Cartesia sonic-3.5)

[통화 후] backend/app/services/report_service.py
  identify_agent_speaker()              녹취록에서 AI 화자 가려내기 (ai/transcript.py)
  audit_transcript()                    AI 발화 안전 감사, 로그만 남김 (ai/harness.py)
  LLM 채점                              시나리오의 tactics · red_flags · ideal_trainee_response와 행동 라벨(ai/classifier.py)로 채점
  heuristic_report()                    LLM을 못 쓰면 키워드 간이 채점 (ai/hangup.py)
```

## 지시문 구성

| 층 | 들어가는 것 | 코드 | 반영 |
| --- | --- | --- | --- |
| 에이전트 공통 규칙 | 안전 규칙, 말투 규칙, 전화 규칙 (4,861자, 한도 16,000자) | `safety.SAFETY_RULES`, `scenarios/playbook._STYLE_RULES`, `managed_agent._PHONE_RULES` | sync 후 반영 |
| 통화별 지시(CallContext) | [첫 마디], 역할 · 사건 · 목표 · 진행 · 받아치기 · 예시, 단계별 대사 예시, 받아치기 예시, 경고와 넘김 말, 통화 길이 (2,524~3,302자, 한도 4,000자) | `scenarios/library.py` → `managed_agent.build_call_context()` | 배포 후 다음 통화부터 |

- sync는 최신 `develop`에서만 실행합니다: `python -m ai.managed_agent sync --variant external_tts`
- sync는 에이전트 설정 전체를 덮어씁니다. 콘솔에서 바꾼 값은 되돌아가고, API에 없는 콘솔 전용 설정만 유지됩니다([`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md)).
- 통화별 지시가 4,000자를 넘으면 단계별 대사 예시, 받아치기 예시 순으로 빼고, 그래도 넘으면 예외가 납니다.
- 실제 지시문 확인: `python -m ai.managed_agent context --scenario <id>`

## 전화 규칙 요약 (`managed_agent._PHONE_RULES`)

- [첫 마디]는 그대로 말하고 덧붙이지 않는다. 인사를 청하면 "아, 안녕하세요."로 받는다.
- [역할] 말투 유지, 한국어만, 한 번에 두 문장까지.
- 거절하면 [거절할 때 경고](포기 경고). 그 뒤 또 거절·끊기·장난이 나오면 종료.
- 첫 "끊을게요"에는 [끊으려 할 때 경고], 두 번째에 종료. 포기 경고를 이미 했으면 바로 종료.
- 마지막 요구를 승낙하면 [승낙받으면 넘김], 상대가 한 마디 더 하면 종료.
- 장난이나 겉도는 대답이 세 번 이어지면 거절로 본다.
- 종료할 때는 말없이 종료 도구만 호출(같은 턴의 발화는 재생 전에 끊김).
- 숫자·주소·링크를 지어내지 않고, 상대가 실제 번호를 부르려 하면 막는다.
- 통화 비서·자동 응답에는 소속, 이름, 용건 한 문장만 말하고 기다린다.
- 예외: 다른 사람이 받으면 "죄송합니다, 잘못 걸었습니다."만. 위급 상황이면 역할을 멈추고 훈련 전화임을 알림. 상대가 아무 말도 안 했으면 종료하지 않음(무응답은 콘솔 설정이 처리).

## 시나리오 (`scenarios/library.py`)

| id | 이름 | 유형 · 난이도 | 인물 | 최대 발화 |
| --- | --- | --- | --- | :---: |
| `bank_security_hold` | 해외 결제 승인 가로채기 | 기관사칭형 · 중 | 서동현(가온금융안전원 결제보호팀) | 8 |
| `low_interest_loan` | 대환대출 선상환 요구 | 대출사기형 · 하 | 박수현(미래드림 금융생활지원센터) | 7 |
| `ipo_allocation` | 공모주 우선 배정 투자 권유 | 투자사기형 · 하 | 한지수(온새미투자자문 공모주배정팀) | 8 |
| `card_delivery` | 카드 배송원 사칭 | 배송원사칭형 · 중 | 최준호(한길퀵 카드배송) | 8 |
| `investigation_unit` | 명의도용 수사 협조 압박 | 수사기관사칭형 · 상 | 서재욱(금융범죄 합동대응반 자산보전과) | 9 |

시나리오는 대본이 아닙니다. 대사는 모델이 쓰고 시나리오는 사건·목표·압박 방향만 정하므로, `turn_plan`과 `objection_handling`은 행동 지시로 씁니다.

| 필드 | 쓰임 |
| --- | --- |
| `opening_line` | [첫 마디] |
| `role` · `incident` · `goal` · `turn_plan` · `objection_handling` · `examples` | 통화별 지시의 본문 |
| `progression` | [단계별 대사 예시] |
| `script` | 의도별 첫 줄만 [상대 반응별 받아치기 예시] |
| `giveup_line` · `handoff_line` · `hangup_line` · `max_turns` | [거절할 때 경고] · [승낙받으면 넘김] · [끊으려 할 때 경고] · [통화 길이] |
| `tactics` · `red_flags` · `ideal_trainee_response` | 리포트 채점 기준 |
| `quick_replies` · `script`의 두 번째 줄 · `tts_voice_id` | CallContext에 안 들어감. 녹취 화자 판정과 이전 코드에서만 사용 |

- 선택: `CALL_SCENARIO`가 비어 있으면 무작위(직전 시나리오 제외). `CALL_SCENARIO`는 전체 통화, `ANNOUNCED_CALL_SCENARIO`는 1차 통화만 고정합니다.
- 모르는 id는 기본 시나리오(`bank_security_hold`)로 대체하고 id 값은 유지합니다. 이전 id(`voice_phishing_training`, `delivery_payment_error`, `family_emergency`)는 `_ALIASES`로 연결합니다.
- 통화 중 문자는 보내지 않습니다. 카드배송은 사고 접수 번호를 "문자로 보내 드리겠다"고만 하고 실제 발송은 없습니다.
- 작성 규칙은 [`ai/scenarios/scenario_generation_guidelines.md`](ai/scenarios/scenario_generation_guidelines.md), 근거 자료는 [`docs/research/voice-phishing-patterns.md`](docs/research/voice-phishing-patterns.md).

## 안전 장치

실제 범죄에 쓰일 수 있는 내용(번호, URL, 실제 기관명)이 나오지 않게 막습니다.

| 단계 | 위치 | 하는 일 |
| --- | --- | --- |
| 지시문 | `safety.SAFETY_RULES` (공통 규칙 맨 앞) | 실제 기관 이름 사칭, 계좌 · 카드 · 주민등록 · 인증번호, 전화번호 · URL · 앱 정보 말하기, AI나 훈련임을 드러내기를 금지합니다 |
| 시나리오 문장 검사 | `safety.REAL_ORGS` · `SPOKEN_META` · `UNSAFE_TOKEN` | `backend/tests/test_scenario_library.py`가 모든 시나리오 문장을 검사합니다. 새 시나리오도 자동으로 검사합니다 |
| 통화 후 감사 | `harness.audit_transcript` | 녹취록의 AI 발화에서 실제 기관 이름, 비밀정보 요구, 역할 이탈을 찾아 로그로 남깁니다 |
| 위급 상황 | `_PHONE_RULES` [예외] | 상대가 위급하다고 하면 역할을 멈추고 훈련 전화였다고 알립니다 |

기관 이름은 모두 가상입니다. 상대가 실제 기관 이름을 말해도 그 기관을 사칭하지 않습니다.
매니지드 에이전트 발화는 우리 코드를 거치지 않아 통화 중 필터링은 할 수 없고, 지시문과 통화 후 감사로 대신합니다.

## 리포트 채점 기준

점수는 백엔드 `report_service.calculate_response_score()`에서 계산합니다. 기본 60점에 행동별 가중치(`RISK_SCORE_WEIGHTS`, `DEFENSE_SCORE_WEIGHTS`)를 더하고 0~100으로 자릅니다.
채점 LLM(OpenAI, 없으면 Gemini)이 뽑은 행동 중 근거가 훈련자 발화에 실제로 있는 것만 반영합니다.

| 위험 행동 (`RISK_LABELS`, 8개) | 방어 행동 (`DEFENSE_LABELS`, 6개) |
| --- | --- |
| 개인정보 제공 · 금융정보 제공 · 상대방 기관명 신뢰 · 송금 의사 표현 · 링크 접근 의사 · 앱 설치 의사 · 지정 번호 전화 의사 · 통화 장시간 지속 | 상대방 신원 확인 · 공식 대표번호 확인 의사 · 개인정보 제공 거절 · 송금 거절 · 전화 종료(빠른 판단) · 신고 의사 표현 |

- `지정 번호 전화 의사`: 상대가 알려 준 번호로 전화하겠다는 답(카드배송).
- 라벨을 추가하면 백엔드 가중치 표에도 넣어야 합니다. 빠지면 리포트에만 나오고 점수에는 반영되지 않습니다.
- LLM 호출이 실패하거나 훈련자 발화가 없으면 키워드 간이 채점을 씁니다. 종료 의사는 `hangup.wants_hang_up()`으로 판정합니다(거절은 제외, 발화 끝 30자 안만 인정).
- 통화 중 AI의 종료 시점은 `hangup.py`가 아니라 전화 규칙이 정합니다.

## 실행

키는 `backend/.env`에만 둡니다(`ai/.env`가 있으면 그쪽이 먼저 읽힘).
Windows에서는 `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`을 붙여 실행합니다.

```bash
# 저장소 루트에서
python -m pytest ai/tests                                                    # AI 테스트
python -m ai.managed_agent context --scenario investigation_unit             # 통화별 지시문과 글자 수
python -m ai.managed_agent sync --dry-run                                    # 에이전트에 보낼 내용만 보기
python -m ai.managed_agent sync --variant external_tts                       # 에이전트 갱신 (최신 develop에서만)
python -m ai.managed_agent list --variant external_tts                       # 에이전트 id, 안전 규칙 누락 여부
python -m ai.managed_agent call --to 010XXXXXXXX --scenario card_delivery --wait   # 백엔드 없이 시험 통화
python -m ai.transcript show <callId> --scenario card_delivery --summary          # 녹취록, 화자 판정, 안전 감사
```

운영 절차와 콘솔 전용 설정은 [`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md) 참고.

## 환경변수

AI 관련 항목만 적었습니다. 전체는 [`backend/.env.example`](backend/.env.example).

| 변수 | 쓰임 | 기본값 |
| --- | --- | --- |
| `CLAWOPS_API_KEY` · `CLAWOPS_ACCOUNT_ID` | 에이전트 sync, 발신, 녹취록 | 필수 |
| `CLAWOPS_PHONE_NUMBER` | CLI 시험 통화의 발신 번호 | |
| `CALL_AGENT_VARIANT` | `external_tts` · `live` · `ab`(통화마다 무작위) | `external_tts` |
| `CALL_SCENARIO` · `ANNOUNCED_CALL_SCENARIO` | 모든 통화 / 1차 통화를 한 시나리오로 고정 | 비우면 무작위 |
| `MANAGED_AGENT_REALTIME_MODEL` | `external_tts` 모델을 강제로 지정 | `gpt-realtime-2.1` |
| `MANAGED_AGENT_LIVE_BACKEND` | `live` 방식의 업무 모델 | `gpt-5.6-luna` |
| `OPENAI_API_KEY` · `GEMINI_API_KEY` (`OPENAI_MODEL` · `GEMINI_MODEL`) | 리포트 채점 LLM (백엔드가 씀) | |

## 통화에 쓰이지 않는 이전 코드

예전 서버 음성 처리 경로(Deepgram → LLM → ElevenLabs)의 대본 모드 코드입니다. 현재 통화에는 영향이 없고, `backend/tests`가 검사하고 있어 테스트에 필요한 부분만 남겼습니다.

- `scenarios/script.py` · `intents.py` · `reflex.py`: 의도 분류, 고정 답 선택 (`ScriptReply`는 `Playbook.script` 타입으로 사용 중)
- `prerender.py` · `script_eval.py`: 대본 대사 미리 합성, 적중률 계산
- `voices.py`: ElevenLabs 목소리 id(`tts_voice_id`). 현재 목소리는 `managed_agent.CARTESIA_VOICES`
- `harness.py`의 `CallMonitor` · `GuardedLLM` (`OutputGuard`는 `audit_transcript`에서 사용 중)

## 알려진 제약

| 항목 | 내용 |
| --- | --- |
| 통화 중 필터 없음 | 지시문과 통화 후 감사로만 막는다 |
| 통화 길이 | [통화 길이]는 모델이 세는 값이라 정확하지 않다 |
| 콘솔 전용 설정 | 무응답 처리 등은 API에 없어 sync로 관리되지 않는다 |
| 녹취록 | 겹친 발화가 빠지거나 통화 비서 발화가 다른 화자로 붙을 수 있다 |
| 시나리오 연속 방지 | 프로세스 메모리 기준이라 워커가 여럿이면 각자 따로 동작 |
| 카드배송 문자 | 문자로 번호를 보내겠다고 하지만 실제 발송은 없다 |

## 관련 문서

- [`docs/architecture.md`](docs/architecture.md): 전체 시스템 구조
- [`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md): 에이전트 운영 절차, 시험 통화, 콘솔 전용 설정
- [`ai/scenarios/scenario_generation_guidelines.md`](ai/scenarios/scenario_generation_guidelines.md): 시나리오 작성 규칙
- [`docs/research/voice-phishing-patterns.md`](docs/research/voice-phishing-patterns.md): 실제 보이스피싱 수법과 통계
