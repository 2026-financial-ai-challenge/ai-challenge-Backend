# `ai` — 훈련 통화의 시나리오 · 대화 규칙 · 채점 기준

보이스피싱 대응 훈련 전화에서 **AI 상담원이 무엇을 말할지**와 **통화가 끝난 뒤 무엇으로 채점할지**를 정하는 패키지입니다.
통화는 ClawOps 매니지드 에이전트가 진행합니다. 백엔드(`backend/app`)는 이 패키지로 시나리오와 에이전트를 고르고, 통화별 지시문을 만들어 발신합니다.
백엔드는 `backend/app/training/scenarios.py`의 `ensure_ai_importable()`로 이 패키지를 찾습니다(저장소 루트, 또는 Docker 이미지의 `/packages`).

## 통화 한 건의 흐름

```
[발신] backend/app/services/call_service.py
  get_runtime_scenario()                시나리오 고르기 (ai/scenarios)
  pick_variant(), resolve_agent_id()    에이전트 spc-<시나리오 id>-<방식> 찾기 (ai/managed_agent.py)
  build_call_context()                  통화별 지시문 만들기 (4,000자 이내)
  ClawOps calls.create                  이후 대화는 ClawOps가 진행
                                        OpenAI Realtime(gpt-realtime-2.1)이 듣고 답을 쓰고, Cartesia(sonic-3.5)가 말합니다

[통화 후] backend/app/services/report_service.py
  identify_agent_speaker()              녹취록에서 AI 화자 가려내기 (ai/transcript.py)
  audit_transcript()                    AI 발화 안전 감사, 로그만 남김 (ai/harness.py)
  LLM 채점                              시나리오의 tactics · red_flags · ideal_trainee_response와 행동 라벨(ai/classifier.py)로 채점
  heuristic_report()                    LLM을 못 쓰면 키워드 간이 채점 (ai/hangup.py)
```

## 지시문은 두 층입니다

| 층 | 들어가는 것 | 코드 | 반영 |
| --- | --- | --- | --- |
| 에이전트 공통 규칙 | 안전 규칙, 말투 규칙, 전화 규칙 (4,861자, 한도 16,000자) | `safety.SAFETY_RULES`, `scenarios/playbook._STYLE_RULES`, `managed_agent._PHONE_RULES` | **sync해야** 반영 |
| 통화별 지시(CallContext) | [첫 마디], 역할 · 사건 · 목표 · 진행 · 받아치기 · 예시, 단계별 대사 예시, 받아치기 예시, 경고와 넘김 말, 통화 길이 (2,524~3,302자, 한도 4,000자) | `scenarios/library.py` → `managed_agent.build_call_context()` | 운영 배포 뒤 **다음 통화부터** |

- sync는 최신 `develop` 코드로만 돌립니다: `python -m ai.managed_agent sync --variant external_tts`.
  sync는 에이전트 설정 전체를 덮어쓰므로, 콘솔에서만 바꾼 값은 다음 sync 때 되돌아갑니다. API에 없는 콘솔 전용 설정만 예외입니다([`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md)).
- 통화별 지시가 4,000자를 넘으면 단계별 대사 예시, 받아치기 예시 순으로 빼고, 그래도 넘으면 오류를 냅니다.
- 실제로 보내는 지시문은 `python -m ai.managed_agent context --scenario <id>`로 볼 수 있습니다.

## 전화 규칙 요약 (`managed_agent._PHONE_RULES`)

- [첫 마디]를 토씨 하나 바꾸지 않고 말하고, 덧붙이지 않고 대답을 기다립니다. 상대가 인사를 청하면 "아, 안녕하세요."로 받습니다.
- [역할]의 말투를 끝까지 지키고, 한국어로만, 한 번에 두 문장까지 말합니다.
- 거절하면 [거절할 때 경고](포기 경고)를 말합니다. 그 뒤 또 거절하거나, 끊겠다고 하거나, 장난을 치면 통화를 끝냅니다.
- 끊겠다는 말을 처음 들으면 [끊으려 할 때 경고]로 붙잡고, 두 번째에 끝냅니다. 포기 경고를 이미 했으면 바로 끝냅니다.
- 마지막 요구를 하겠다고 하면 [승낙받으면 넘김]을 말하고, 상대가 한 마디 더 하면 끝냅니다.
- 엉뚱한 말이나 장난, 겉도는 대답이 세 번 이어지면 거절로 봅니다.
- 끝낼 때는 아무 말 없이 종료 도구만 부릅니다. 종료 도구와 같은 차례에 한 말은 재생되기 전에 끊기기 때문입니다.
- 숫자, 주소, 링크를 지어내지 않습니다. 상대가 실제 번호를 불러 주려 하면 막습니다.
- 통화 비서나 자동 응답에는 소속, 이름, 용건을 한 문장으로 말하고 기다립니다.
- 예외: 받은 사람이 다르면 "죄송합니다, 잘못 걸었습니다."만 말합니다. 위급하다고 하면 역할을 멈추고 훈련 전화였다고 알립니다. 상대가 한 마디도 하지 않았으면 끝내지 않습니다(말 없는 통화는 콘솔의 무응답 처리가 끝냅니다).

규칙마다 어떤 통화에서 드러난 문제를 고친 것인지 `_PHONE_RULES` 위 주석에 통화 id와 함께 적어 두었습니다.

## 시나리오 (`scenarios/library.py`)

| id | 이름 | 유형 · 난이도 | 인물 | 최대 발화 |
| --- | --- | --- | --- | :---: |
| `bank_security_hold` | 해외 결제 승인 가로채기 | 기관사칭형 · 중 | 서동현(가온금융안전원 결제보호팀) | 8 |
| `low_interest_loan` | 대환대출 선상환 요구 | 대출사기형 · 하 | 박수현(미래드림 금융생활지원센터) | 7 |
| `ipo_allocation` | 공모주 우선 배정 투자 권유 | 투자사기형 · 하 | 한지수(온새미투자자문 공모주배정팀) | 8 |
| `card_delivery` | 카드 배송원 사칭 | 배송원사칭형 · 중 | 최준호(한길퀵 카드배송) | 8 |
| `investigation_unit` | 명의도용 수사 협조 압박 | 수사기관사칭형 · 상 | 서재욱(금융범죄 합동대응반 자산보전과) | 9 |

시나리오는 대본이 아니라 지침입니다. 대사는 매 차례 모델이 새로 쓰고, 시나리오는 사건, 목표, 압박 방향을 고정합니다. 그래서 `turn_plan`과 `objection_handling`은 대사가 아니라 행동 지시로 씁니다.

| 필드 | 쓰임 |
| --- | --- |
| `opening_line` | [첫 마디] |
| `role` · `incident` · `goal` · `turn_plan` · `objection_handling` · `examples` | 통화별 지시의 본문 |
| `progression` | [단계별 대사 예시] |
| `script` | 의도별 첫 줄만 [상대 반응별 받아치기 예시] |
| `giveup_line` · `handoff_line` · `hangup_line` · `max_turns` | [거절할 때 경고] · [승낙받으면 넘김] · [끊으려 할 때 경고] · [통화 길이] |
| `tactics` · `red_flags` · `ideal_trainee_response` | 리포트 채점 기준 |
| `quick_replies` · `script`의 두 번째 줄 · `tts_voice_id` | 통화 지시문에는 들어가지 않습니다. 녹취 화자 판정과 이전 코드에서만 씁니다 |

- **고르는 방법:** `CALL_SCENARIO`가 비어 있으면 통화마다 무작위로 고르고, 직전 시나리오는 빼서 연달아 겹치지 않게 합니다(`pick_scenario`). `CALL_SCENARIO`는 모든 통화를, `ANNOUNCED_CALL_SCENARIO`는 1차(예고) 통화만 한 시나리오로 고정합니다.
- **모르는 id:** 기본 시나리오(`bank_security_hold`)로 넘어가되 요청한 id는 유지합니다. 예전 id(`voice_phishing_training`, `delivery_payment_error`, `family_emergency`)는 `_ALIASES`가 지금 id로 잇습니다.
- **문자:** 통화 중에는 문자를 보내지 않습니다(2026-10-07에 제거). 시나리오도 문자를 보냈다고 말하지 않습니다. 카드배송만 사고 접수 번호를 "문자로 보내 드리겠다"고 약속하고, 실제 문자는 가지 않습니다.
- **작성 규칙:** 새 시나리오를 쓰거나 고칠 때는 [`ai/scenarios/scenario_generation_guidelines.md`](ai/scenarios/scenario_generation_guidelines.md)를 따릅니다. 실제 수법과 통계 근거는 [`docs/research/voice-phishing-patterns.md`](docs/research/voice-phishing-patterns.md)에 있습니다.

## 안전 장치

AI가 사기범을 연기하되, 실제 범죄에 다시 쓸 수 있는 내용은 내지 않게 합니다.

| 단계 | 위치 | 하는 일 |
| --- | --- | --- |
| 지시문 | `safety.SAFETY_RULES` (공통 규칙 맨 앞) | 실제 기관 이름 사칭, 계좌 · 카드 · 주민등록 · 인증번호, 전화번호 · URL · 앱 정보 말하기, AI나 훈련임을 드러내기를 금지합니다 |
| 시나리오 문장 검사 | `safety.REAL_ORGS` · `SPOKEN_META` · `UNSAFE_TOKEN` | `backend/tests/test_scenario_library.py`가 모든 시나리오 문장을 검사합니다. 새 시나리오도 자동으로 검사합니다 |
| 통화 후 감사 | `harness.audit_transcript` | 녹취록의 AI 발화에서 실제 기관 이름, 비밀정보 요구, 역할 이탈을 찾아 로그로 남깁니다 |
| 위급 상황 | `_PHONE_RULES` [예외] | 상대가 위급하다고 하면 역할을 멈추고 훈련 전화였다고 알립니다 |

기관 이름은 모두 가상입니다. 상대가 실제 기관 이름을 먼저 말해도 그 기관 직원인 척하지 않습니다.
매니지드 에이전트는 말을 만들자마자 재생하므로 통화 중에 문장을 걸러 낼 수 없습니다. 지시문과 통화 후 감사가 그 몫을 합니다.

## 리포트 채점 기준

점수는 백엔드 `report_service.calculate_response_score()`가 계산합니다. 기본 60점에서 행동마다 가중치(`RISK_SCORE_WEIGHTS`, `DEFENSE_SCORE_WEIGHTS`)를 더하고 빼서 0~100점으로 맞춥니다.
채점 LLM(OpenAI, 없으면 Gemini)이 뽑은 행동 가운데, 훈련자가 실제로 한 말로 근거가 확인된 것만 점수에 넣습니다.

| 위험 행동 (`RISK_LABELS`, 8개) | 방어 행동 (`DEFENSE_LABELS`, 6개) |
| --- | --- |
| 개인정보 제공 · 금융정보 제공 · 상대방 기관명 신뢰 · 송금 의사 표현 · 링크 접근 의사 · 앱 설치 의사 · 지정 번호 전화 의사 · 통화 장시간 지속 | 상대방 신원 확인 · 공식 대표번호 확인 의사 · 개인정보 제공 거절 · 송금 거절 · 전화 종료(빠른 판단) · 신고 의사 표현 |

- `지정 번호 전화 의사`는 카드배송처럼 상대가 알려 주거나 보내 준 번호로 전화하겠다는 대답입니다(2026-10-07 추가).
- 라벨을 더하면 백엔드 `RISK_SCORE_WEIGHTS`나 `DEFENSE_SCORE_WEIGHTS`에도 가중치를 넣어야 합니다. 없으면 리포트에는 나오지만 점수는 바뀌지 않습니다.
- LLM을 쓸 수 없거나 훈련자 발화가 없으면 키워드로 간이 채점합니다. 끊으려 했는지는 `hangup.wants_hang_up()`이 판정합니다.
  - 거절("안 할래요", "됐어요")은 끊겠다는 말로 보지 않습니다.
  - "이만"은 뒤에 끊는다는 말이 올 때만 셉니다. "이만 삼천 원"의 "이만"은 숫자입니다.
  - 발화 끝 30자 안에 있을 때만 인정합니다. 긴 말 앞부분의 같은 표현은 대개 남의 말을 옮긴 것입니다.
- 통화 중에 AI가 언제 끊을지는 `hangup.py`가 아니라 위의 전화 규칙이 정합니다.

## 실행

키는 `backend/.env` 한 파일에만 둡니다. `ai/.env`를 만들면 `config.py`가 그 파일을 먼저 읽어 값이 갈립니다.
Windows에서는 명령 앞에 `PYTHONUTF8=1 PYTHONIOENCODING=utf-8`을 붙입니다.

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

에이전트 운영 절차, 시험 통화 평가표, 콘솔 전용 설정(무응답 처리 등)은 [`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md)에 있습니다.

## 환경변수

AI와 관련된 것만 적었습니다. 전체 목록은 [`backend/.env.example`](backend/.env.example)에 있습니다.

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

매니지드 에이전트로 옮기기 전에는 서버가 직접 음성을 처리했습니다(Deepgram → LLM → ElevenLabs). 그 경로는 2026-10-06(#71)에 백엔드에서 지웠고, `ai/`에는 그때의 대본 모드 코드가 남아 있습니다. 백엔드 테스트 일부가 아직 이 코드를 검사합니다. 고쳐도 지금 통화는 바뀌지 않습니다.

- `scenarios/script.py` · `scenarios/intents.py` · `scenarios/reflex.py`: 대본 모드의 의도 분류와 즉답표. `Playbook`은 `ScriptReply` 타입만 씁니다.
- `prerender.py` · `script_eval.py`: 대본 대사 미리 합성, 적중률 측정 CLI
- `voices.py`: ElevenLabs 목소리 id(`tts_voice_id`). 지금 목소리는 `managed_agent.CARTESIA_VOICES`가 정합니다.
- `harness.py`의 `CallMonitor` · `GuardedLLM`: 통화 중 문장 감시. `OutputGuard`는 `audit_transcript`가 계속 씁니다.
- `config.py`의 ElevenLabs · Deepgram 설정

`docs/architecture.md`, `docs/latency.md`, `docs/harness.md`, `docs/script-mode.md`도 이전 방식을 기준으로 쓴 문서입니다.

## 알려진 제약

| 항목 | 내용 |
| --- | --- |
| 통화 중 필터 없음 | 매니지드 에이전트의 말은 우리 코드를 거치지 않고 재생됩니다. 지시문과 통화 후 감사로만 막습니다 |
| 통화 길이 | [통화 길이]는 모델이 스스로 세는 값이라 정확하지 않습니다 |
| 콘솔 전용 설정 | 무응답 처리, 첫 인사 끝까지 말하기 등은 API에 없어 sync로 관리되지 않습니다 |
| 녹취록 | 겹쳐 말한 부분이 빠지거나, 통화 비서의 말이 받는 사람이나 AI의 말로 붙을 수 있습니다 |
| 시나리오 연속 방지 | `pick_scenario`는 직전 시나리오를 프로세스 메모리에 기억합니다. 서버 워커가 여러 개면 각자 따로 기억합니다 |
| 카드배송 문자 | AI가 사고 접수 번호를 문자로 보내겠다고 하지만 실제로 문자는 가지 않습니다 |

## 관련 문서

- [`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md): 에이전트 운영 절차, 시험 통화, 콘솔 전용 설정
- [`ai/scenarios/scenario_generation_guidelines.md`](ai/scenarios/scenario_generation_guidelines.md): 시나리오 작성 규칙
- [`docs/research/voice-phishing-patterns.md`](docs/research/voice-phishing-patterns.md): 실제 보이스피싱 수법과 통계
