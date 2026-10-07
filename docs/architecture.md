# 시스템 구조

보이스피싱 대응 훈련 시뮬레이터의 전체 구조입니다. 참가자가 동의하면 AI가 실제 보이스피싱처럼 전화를 걸고, 통화가 끝나면 녹취록을 채점해 리포트를 보여 줍니다.

> 세부 내용은 [`backend/README.md`](../backend/README.md)(서버 · 배포)와 [`AI_README.md`](../AI_README.md)(시나리오 · 대화 규칙 · 채점)에 있습니다.

## 1. 구성 요소

```
 참가자 브라우저 ──▶ 프론트엔드 (Next.js, Vercel) ──/v1/*──▶ 백엔드 (FastAPI, Lightsail 서울)
                                                              │        │
                                                   발신 · 문자 │        │ PostgreSQL · Redis
                                                              ▼        │
 참가자 전화 ◀──── 통화 ──── ClawOps 매니지드 에이전트             │
                         (OpenAI Realtime + Cartesia TTS)         │
                                  │ 상태 · 녹취록 웹훅             │
                                  └──────────────────────────────┘
```

| 영역 | 사용 기술 |
| --- | --- |
| 웹 서버 | FastAPI (Python 3.13) + Uvicorn |
| DB | PostgreSQL 16 + SQLAlchemy 2 + Alembic |
| 가입 인증 상태 | Redis (OTP, 재발송 대기, 인증 토큰) |
| 전화 · 문자 | ClawOps: 매니지드 에이전트 발신, 가입 인증번호 문자, 상태 · 녹취록 웹훅 |
| 통화 중 AI | ClawOps가 실행. OpenAI Realtime(`gpt-realtime-2.1`)이 듣고 답을 쓰고, Cartesia(`sonic-3.5`)가 말함 |
| 리포트 채점 | OpenAI, 없으면 Gemini(OpenAI 호환 엔드포인트) |
| 배포 | AWS Lightsail 서울, Docker Compose(backend · Postgres · Redis · Caddy) |
| 프론트엔드 | Next.js, Vercel (별도 저장소) |

**통화를 서버가 아니라 ClawOps가 진행합니다.** 백엔드는 시나리오와 에이전트를 고르고 통화별 지시문을 만들어 발신 요청만 합니다. 대화 중의 음성 인식, 응답 생성, 음성 합성, 끼어들기 처리는 모두 ClawOps 안에서 일어납니다.

## 2. 저장소 구조

```
ai-challenge-Backend/
├── backend/            FastAPI 앱 (배포 단위)
│   ├── app/
│   │   ├── routers/    API 라우트 (auth, consent, session, call, report, webhook)
│   │   ├── services/   비즈니스 로직 (인증, 통화, 리포트, 불시 전화 예약, 개인정보 파기, 문자)
│   │   ├── models/     ORM 모델
│   │   ├── schemas/    요청 · 응답 스키마
│   │   ├── training/   ai 패키지 연결과 통화 시나리오 선택
│   │   └── periodic.py 백그라운드 주기 작업의 공통 스레드
│   ├── alembic/        DB 마이그레이션
│   └── tests/
├── ai/                 시나리오, 에이전트 규칙과 sync, 녹취록 화자 판정, 채점 라벨
└── docs/               이 문서와 보이스피싱 수법 조사(research/)
```

Docker 이미지는 `backend/`를 `/app`으로, `ai/`를 `/packages/ai`로 복사합니다. 백엔드는 `ensure_ai_importable()`로 `ai` 패키지를 찾습니다.

## 3. API

| Method | Endpoint | 설명 |
| --- | --- | --- |
| POST | `/v1/auth/signup/otp` | 가입 인증번호 문자 발송 |
| POST | `/v1/auth/signup/verify` | 인증번호 확인 |
| POST | `/v1/auth/signup` | 회원가입 |
| POST | `/v1/auth/login` | 로그인 |
| POST | `/v1/consents` | 훈련 참여 동의 |
| GET | `/v1/sessions`, `/v1/sessions/{id}` | 훈련 세션 목록 · 상세 |
| POST | `/v1/sessions/{id}/calls` | 훈련 통화 시작 |
| GET | `/v1/sessions/{id}/report` | 리포트 조회 |
| POST | `/v1/webhooks/clawops/status` | 통화 상태 (연결 · 종료) |
| POST | `/v1/webhooks/clawops/transcript` | 녹취록 (서명 검증) |

요청 · 응답 형식은 서버를 띄운 뒤 `/docs`(FastAPI 자동 문서)에서 봅니다.

## 4. 훈련 흐름

```
① 가입        문자 인증번호 → 회원가입
② 동의        훈련 참여 · 불시 전화 동의 → 훈련 세션 생성
③ 1차 통화    POST /calls → 시나리오 · 에이전트 선택 → ClawOps 발신
④ 통화 종료   상태 웹훅 → 녹취록 요청 → 녹취록 웹훅 → 화자 구분 → 채점 → 리포트 저장
⑤ 불시 통화   1차 통화 30~60분 뒤 무작위 시점에 다른 번호로 자동 발신 (실패하면 재시도)
⑥ 최종 리포트 1차 · 불시 통화를 비교
```

세션 상태는 두 축으로 관리합니다.

- `call_status`: `waiting` → `calling` → `completed` / `missed` / `silent` / `failed`
- `report_status`: `none` / `pending` / `draft` / `final` / `failed`

## 5. 통화 한 건

1. `call_service`가 시나리오를 고릅니다. 기본은 다섯 편 중 무작위이고, `CALL_SCENARIO`로 고정할 수 있습니다.
2. 에이전트 `spc-<시나리오 id>-external_tts`를 찾고, 통화별 지시문(CallContext, 4,000자 이내)을 만들어 발신합니다.
3. 에이전트에는 모든 시나리오가 같이 쓰는 공통 규칙(안전, 말투, 전화 규칙)만 들어 있습니다. 공통 규칙은 `python -m ai.managed_agent sync`로 반영하고, 시나리오 내용은 통화마다 지시문으로 보냅니다.
4. 통화가 끝나면 `report_service`가 녹취록에서 AI 화자를 가려내고, 시나리오의 위험 신호와 행동 라벨을 기준으로 LLM이 채점합니다. 훈련자가 실제로 한 말로 근거가 확인된 행동만 점수에 넣습니다.

시나리오, 공통 규칙, 채점 기준은 [`AI_README.md`](../AI_README.md)에 정리했습니다.

## 6. 백그라운드 작업

앱이 시작할 때 `main.py`의 lifespan에서 데몬 스레드로 띄웁니다(`periodic.PeriodicWorker`).

| 작업 | 파일 | 주기 | 하는 일 |
| --- | --- | --- | --- |
| 불시 전화 | `services/training_scheduler.py` | 30초 | 시간이 된 예약을 발신. 실패하면 5분 뒤 다시 시도(기본 최대 2번) |
| 개인정보 파기 | `services/data_retention.py` | 6시간 | 가입 30일이 지난 참가자 삭제 |

참가자를 지우면 훈련 세션의 `participant_id`가 `ON DELETE SET NULL`로 끊어집니다. 전화번호와 비밀번호 해시는 사라지고, 세션 · 녹취 · 리포트는 개인을 알 수 없는 상태로 남습니다.

서버 워커가 하나라는 전제로 짠 작업입니다. 워커를 늘리면 예약 발신이 겹치지 않게 잠금부터 바꿔야 합니다.

## 7. 데이터 모델

```
Participant          참가자 (전화번호, 비밀번호 해시, 동의 플래그)
 └─< TrainingSession 훈련 세션 (call_status, report_status, current_training_type)   ※ 참가자 삭제 시 NULL
       ├── Consent          동의 스냅샷
       ├─< Call             ClawOps 통화 id, 상태, 시나리오, 에이전트 방식
       ├─< TranscriptTurn   발화 단위 녹취
       └─< TrainingReport   리포트 (점수, 위험 · 방어 행동, 요약, 코칭)

PhoneVerification    가입 인증번호 기록
ScheduledTraining    불시 전화 예약 (pending / started / completed / failed / cancelled)
TranscriptEvent      ClawOps 웹훅 원본 (중복 수신 방지)
```

## 8. 인프라

- **로컬:** `backend/`에서 `docker compose up --build`. backend(8000), Redis, Postgres(호스트 5433)를 띄우고 코드가 바뀌면 다시 읽습니다.
- **배포:** `develop`에 push하면 GitHub Actions가 Lightsail 서버에 SSH로 들어가 `docker compose -f compose.prod.yaml up -d --build`를 실행합니다. 컨테이너가 시작할 때 `start.sh`가 마이그레이션을 최대 10회 재시도한 뒤 서버를 띄웁니다. Caddy가 HTTPS를 맡습니다.
- **리전:** ClawOps가 국외 IP의 문자 발송을 막으므로 서버는 한국 리전이어야 합니다.
- **CORS:** 운영 · 미리보기 Vercel 주소와 localhost를 허용합니다. 더 필요하면 `CORS_ALLOWED_ORIGINS`에 넣습니다.
- **ClawOps 에이전트:** sync는 최신 `develop` 코드로만 돌립니다. 무응답 처리처럼 API에 없는 설정은 콘솔에서 따로 관리합니다([`ai/MANAGED_AGENT.md`](../ai/MANAGED_AGENT.md)).

## 9. 환경변수

`backend/.env` 하나만 씁니다(`ai/.env`는 만들지 않습니다). 전체 목록은 [`backend/.env.example`](../backend/.env.example)에 있습니다.

| 변수 | 용도 |
| --- | --- |
| `DATABASE_URL` · `REDIS_URL` | DB · Redis (운영에서는 `compose.prod.yaml`이 채움) |
| `JWT_SECRET` | 로그인 토큰 서명 |
| `CLAWOPS_API_KEY` · `CLAWOPS_ACCOUNT_ID` | ClawOps 인증 |
| `CLAWOPS_PHONE_NUMBER` · `CLAWOPS_UNANNOUNCED_PHONE_NUMBER` | 1차 · 불시 통화 발신 번호 |
| `CLAWOPS_SMS_FROM` | 가입 인증번호 발신 번호 |
| `CLAWOPS_WEBHOOK_SIGNING_SECRET` · `PUBLIC_BASE_URL` | 웹훅 서명 검증 · 상태 웹훅 받을 주소 |
| `CALL_AGENT_VARIANT` · `CALL_SCENARIO` | 에이전트 방식 · 시나리오 고정 |
| `OPENAI_API_KEY` · `GEMINI_API_KEY` | 리포트 채점 LLM |
| `UNANNOUNCED_CALL_*` · `PARTICIPANT_RETENTION_DAYS` | 불시 전화 시점 · 재시도, 개인정보 보존 기간 |
