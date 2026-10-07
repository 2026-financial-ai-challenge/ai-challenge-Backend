# 백엔드 구조

AI 보이스피싱 대응 훈련 서비스의 백엔드가 어떻게 동작하는지 정리한 문서입니다.
실행·배포 방법은 [`backend/README.md`](../backend/README.md), API 형식은 [`api-spec.md`](api-spec.md)에 있습니다.

---

## 1. 한눈에 보기

```
① 가입      휴대폰 인증(SMS OTP) → 회원가입 (개인정보·불시 전화 동의)
② 동의      훈련 세션 생성 → 곧바로 1차(예고) 전화 발신
③ 통화      ClawOps 매니지드 에이전트가 시나리오대로 사기범을 연기
④ 리포트    통화 종료 → 녹취록 수신 → 화자 분리 → LLM 채점 → 리포트 저장
⑤ 불시 전화  30~60분 뒤 무작위 시점, 다른 발신번호로 2차 전화
⑥ 최종 리포트 1차와 불시 전화를 비교해 개선·반복된 행동을 정리
```

| 구성 요소 | 역할 |
| --- | --- |
| **FastAPI 백엔드** (`backend/`) | 인증, 세션, 발신 요청, 웹훅 수신, 채점, 스케줄러 |
| **ai 패키지** (`ai/`) | 시나리오 5편, 안전 규칙, 행동 라벨, 매니지드 에이전트 설정·지시문 생성, 녹취록 화자 판정·안전 감사 |
| **ClawOps** | 매니지드 음성 에이전트(실제 통화 진행), 발신, SMS, 통화 상태·녹취록 웹훅 |
| **PostgreSQL** | 참가자, 세션, 통화, 녹취, 리포트, 불시 전화 예약 |
| **Redis** | 가입 인증 상태(OTP, 재발송 대기, 인증 토큰) — TTL로 자동 만료 |

---

## 2. 통화

### 2.1 매니지드 에이전트

통화 중 음성 인식·응답 생성·음성 합성은 모두 ClawOps 서버의 **매니지드 에이전트**가 처리합니다. 백엔드는 실시간 오디오를 다루지 않습니다.

- 에이전트는 시나리오 × 방식마다 하나씩 만들어 두고(`spc-<시나리오>-<방식>`), `python -m ai.managed_agent sync`로 생성·갱신합니다. 에이전트에는 공통 규칙(안전 규칙, 말투, 전화 규칙)만 들어갑니다.
- **시나리오 내용은 통화마다 지시문(CallContext)으로 보냅니다.** 그래서 시나리오를 고쳐도 에이전트를 다시 만들 필요가 없습니다.
- 방식은 `CALL_AGENT_VARIANT`로 고릅니다.

| 방식 | 구성 | 비고 |
| --- | --- | --- |
| `external_tts` (기본) | OpenAI Realtime(`gpt-realtime-2.1`) + Cartesia TTS(`sonic-3.5`) | 시나리오별 목소리·말 속도 배정 |
| `live` | OpenAI Live(`gpt-live-1`) 음성-음성 | |
| `ab` | 통화마다 위 둘 중 무작위 | 방식 비교용. 어느 방식이었는지 통화 기록에 저장 |

자세한 절차와 콘솔 전용 설정은 [`ai/MANAGED_AGENT.md`](../ai/MANAGED_AGENT.md)에 있습니다.

### 2.2 시나리오

`ai/scenarios/library.py`에 고정 시나리오 5편이 있고, 통화마다 직전과 겹치지 않게 무작위로 고릅니다(`CALL_SCENARIO`로 하나를 고정할 수 있습니다).

| id | 시나리오 | 유형 | 난이도 |
| --- | --- | --- | :---: |
| `bank_security_hold` | 해외 결제 승인 가로채기 | 기관사칭형 | 중 |
| `low_interest_loan` | 대환대출 선상환 요구 | 대출사기형 | 하 |
| `ipo_allocation` | 공모주 우선 배정 투자 권유 | 투자사기형 | 하 |
| `card_delivery` | 카드 배송원 사칭 | 배송원사칭형 | 중 |
| `investigation_unit` | 명의도용 수사 협조 압박 | 수사기관사칭형 | 상 |

시나리오는 대사 원문이 아니라 **사건·목표·압박 방향**을 정한 지침입니다. 인물, 기관, 사건의 시각·금액은 통화 내내 바뀌지 않게 고정하고, 실제 문장은 통화 중 AI가 상대 반응에 맞춰 만듭니다. 시나리오 유형은 국내 피해 통계와 수법 조사([`research/voice-phishing-patterns.md`](research/voice-phishing-patterns.md))를 근거로 골랐습니다.

### 2.3 발신과 상태 처리

[`call_service.py`](../backend/app/services/call_service.py)가 발신을 맡습니다.

1. 시나리오와 에이전트 방식을 고르고, ClawOps에 발신을 요청합니다(에이전트 id, 통화별 지시문, 상태 웹훅 주소).
2. ClawOps가 `ringing` · `answered` · `completed` 이벤트마다 상태 웹훅(`/v1/webhooks/clawops/status`)을 보냅니다. 웹훅 본문 형식이 문서화돼 있지 않아, 백엔드는 통화 id만 읽고 상태는 API로 다시 조회합니다.
3. 상태에 따라 세션을 바꿉니다.
   - 울림·통화 중 → `calling`
   - 정상 종료 → `completed`, 녹취록 요청, 불시 전화 예약
   - 부재·거절·통화 중 → `missed` / 그 외 실패 → `failed` (불시 전화였다면 재시도 예약)

1차 전화와 불시 전화는 **서로 다른 발신번호**(`CLAWOPS_PHONE_NUMBER`, `CLAWOPS_UNANNOUNCED_PHONE_NUMBER`)를 씁니다. 같은 번호로 다시 오면 참가자가 훈련 전화임을 바로 알아채기 때문입니다.

---

## 3. 리포트

### 3.1 만드는 과정

통화가 끝나면 ClawOps에 녹취록을 요청하고, 준비되면 녹취록 웹훅(`/v1/webhooks/clawops/transcript`)이 옵니다. [`report_service.py`](../backend/app/services/report_service.py)가 다음 순서로 리포트를 만듭니다.

1. **화자 분리** — 녹취록의 화자 표기만으로는 누가 AI인지 알 수 없어서, 시나리오의 첫 마디와 가장 비슷하게 말한 쪽을 AI로 판정합니다(`ai/transcript.py`).
2. **안전 감사** — AI 발화에 실제 기관명, 비밀정보 요구, 역할 이탈이 있었는지 검사해 로그로 남깁니다(`ai/harness.audit_transcript`).
3. **채점** — LLM이 참가자의 행동을 정해진 라벨로 분류하고 근거 문장을 붙입니다.
4. **근거 검증** — LLM이 낸 근거가 **참가자가 실제로 한 말에 있는 경우만** 점수에 반영합니다. LLM이 없는 말을 지어내 점수가 바뀌는 것을 막습니다.
5. **점수 계산** — 기본 60점에서 위험 행동은 빼고 방어 행동은 더합니다(0~100점). 같은 라벨은 한 번만 셉니다.

채점 LLM은 OpenAI → Gemini 순서로 키가 있는 쪽을 시도하고, 둘 다 실패하거나 키가 없으면 키워드 기반 간이 채점으로 대신합니다. 리포트 생성이 막혀 참가자가 결과를 못 받는 일은 없습니다.

### 3.2 행동 라벨과 가중치

| 위험 행동 | 점수 | 방어 행동 | 점수 |
| --- | ---: | --- | ---: |
| 금융정보 제공 | -25 | 송금 거절 | +20 |
| 송금 의사 표현 | -20 | 공식 대표번호 확인 의사 | +15 |
| 앱 설치 의사 | -20 | 개인정보 제공 거절 | +15 |
| 개인정보 제공 | -15 | 전화 종료(빠른 판단) | +15 |
| 링크 접근 의사 | -15 | 신고 의사 표현 | +12 |
| 상대방 기관명 신뢰 | -10 | 상대방 신원 확인 | +8 |
| 통화 장시간 지속 | -10 | | |

라벨 정의는 `ai/classifier.py`, 가중치와 점수 계산은 `report_service.calculate_response_score()`에 있습니다. 라벨링과 점수 계산을 나눠, 같은 행동 목록이면 항상 같은 점수가 나오게 했습니다.

### 3.3 회차 리포트

리포트 조회(`GET /v1/sessions/{id}/report`)는 한 회차를 세 부분으로 돌려줍니다.

| 필드 | 내용 |
| --- | --- |
| `draft` | 1차 전화 리포트 |
| `unannounced` | 불시 전화 리포트 |
| `final` | 두 통화 비교 — 점수는 평균, 1차에서 나왔다가 사라진 위험 행동(개선), 두 번 모두 나온 위험 행동(반복), 새로 생긴 방어 행동을 정리 |

`status`는 불시 전화가 남아 있으면 `draft`, 회차가 끝나면 `final`입니다. 불시 전화가 끝내 실패하면 1차 리포트를 최종으로 확정해, 참가자가 새 훈련을 시작할 수 있게 합니다.

---

## 4. 안전 장치

AI가 사기범을 연기하되 실제 범죄에 쓸 수 있는 내용을 말하지 않도록 여러 단계로 막습니다.

| 단계 | 위치 | 내용 |
| --- | --- | --- |
| 프롬프트 규칙 | `ai/safety.py` → 모든 에이전트 공통 지시문 | 실재 기관 사칭 금지(기관명은 모두 가상), 계좌·카드·주민번호·인증번호 요구 금지, 전화번호·URL 발화 금지, 역할 유지 |
| 시나리오 검증 | `backend/tests/test_scenario_library.py` | 모든 시나리오 문장에서 실재 기관명·위험 표현을 테스트로 검사. 새 시나리오도 자동으로 검사 대상 |
| 통화 후 감사 | `ai/harness.audit_transcript` | 실제 통화의 AI 발화를 검사해 위반을 로그로 남김 |
| 채점 근거 검증 | `report_service._keep_supported` | 참가자가 실제로 한 말로 확인된 행동만 점수에 반영 |

---

## 5. 백그라운드 작업

서버가 시작될 때 `main.py`의 lifespan에서 함께 시작됩니다.

| 작업 | 파일 | 주기 | 역할 |
| --- | --- | --- | --- |
| 불시 전화 | `services/training_scheduler.py` | 30초 | 시각이 된 예약을 발신. 실패하면 `UNANNOUNCED_CALL_RETRY_DELAY_SEC` 뒤 재시도(최대 `UNANNOUNCED_CALL_MAX_ATTEMPTS`회) |
| 개인정보 파기 | `services/data_retention.py` | 6시간 | 가입 후 30일(`PARTICIPANT_RETENTION_DAYS`)이 지난 참가자 삭제 |

- **불시 전화 예약**은 `scheduled_trainings` 테이블에 저장됩니다. 1차 통화가 끝날 때 `지금 + MIN~MAX초 사이 무작위`로 예약 시각을 정하므로, 간격 설정을 바꾸면 그 뒤에 잡히는 예약부터 적용됩니다.
- **개인정보 파기**는 참가자 행을 지우면 훈련 세션의 참가자 연결이 FK(`ON DELETE SET NULL`)로 끊기는 방식입니다. 전화번호·비밀번호 해시는 사라지고, 세션·녹취·리포트는 개인을 식별할 수 없는 상태로 통계용으로 남습니다.

---

## 6. 데이터 모델

```
Participant (참가자)                전화번호, 비밀번호 해시, 동의 여부
   └─< TrainingSession (훈련 회차)   call_status, report_status, current_training_type
          ├── Consent              동의 스냅샷
          ├─< Call                 ClawOps 통화 id, 시나리오, 에이전트 방식, 상태
          ├─< TranscriptTurn       발화 단위 녹취 (AI / 참가자)
          └─< TrainingReport       점수, 행동 목록, 요약, 코칭

ScheduledTraining    불시 전화 예약 (1차 세션 → 불시 전화 세션 연결, 시도 횟수, 실패 사유)
TranscriptEvent      녹취록 웹훅 원본 (통화·이벤트별 한 건)
```

세션 상태는 두 축으로 관리합니다.

- `call_status`: `waiting` → `calling` → `completed` / `missed` / `failed`
- `report_status`: `none` → `pending` → `draft`(불시 전화 대기) → `final` (실패 시 `failed`)

---

## 7. 인증

- **가입**: 휴대폰 번호로 인증번호(6자리)를 문자로 받고(유효 5분, 재발송 60초 대기, 5회 틀리면 잠금), 인증되면 10분짜리 인증 토큰으로 비밀번호를 정해 가입합니다. 인증 상태는 Redis에 TTL로 저장해 따로 정리할 필요가 없습니다.
- **로그인**: 전화번호 + 비밀번호로 JWT(유효 1시간)를 받고, 이후 요청은 `Authorization: Bearer <토큰>`으로 보냅니다.
- **세션 접근**: 세션·통화·리포트 API는 로그인한 참가자 본인의 세션만 조회할 수 있습니다.
- **웹훅**: ClawOps 웹훅은 `CLAWOPS_WEBHOOK_SIGNING_SECRET`으로 HMAC 서명을 검증합니다.

---

## 8. 배포 구성

```
GitHub (develop push) ──Actions──▶ Lightsail 서울 서버
                                    └ docker compose (compose.prod.yaml)
                                        ├ caddy     HTTPS(자동 인증서) → backend:8000
                                        ├ backend   start.sh: alembic upgrade head → uvicorn
                                        ├ db        PostgreSQL 16 (외부 비공개)
                                        └ redis     Redis 7 (외부 비공개)
```

- ClawOps가 국외 IP의 문자 발송을 거부하므로 서버는 한국 리전에 둡니다.
- 프론트엔드(Next.js, Vercel)는 `/v1/*` 요청을 rewrite로 백엔드에 넘깁니다. 허용 origin은 `main.py`에 등록돼 있고, 추가 주소는 `CORS_ALLOWED_ORIGINS`로 넣습니다.
