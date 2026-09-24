# 백엔드 · 인프라 구조

보이스피싱 대응 훈련 시뮬레이터의 백엔드/인프라 구성 요약입니다.
참가자에게 AI가 실제 보이스피싱처럼 전화를 걸어 훈련을 진행하고, 통화 내용을 분석해 리포트를 제공합니다.

> 기준 브랜치: `develop` · 작성일: 2026-09-23

---

## 1. 기술 스택

| 영역 | 사용 기술 |
|---|---|
| 웹 프레임워크 | FastAPI (Python 3.13) + Uvicorn |
| DB | PostgreSQL 16 + SQLAlchemy 2.0 + Alembic |
| 전화 발신 / SMS | ClawOps (`clawops[agent,openai,gemini,deepgram,elevenlabs]`) |
| STT (음성→텍스트) | Deepgram (`nova-2`, 한국어) |
| LLM (응답 생성) | OpenAI / Gemini (상호 폴백) |
| TTS (텍스트→음성) | ElevenLabs (`eleven_flash_v2_5`) |
| 번호 확인 | OCTOMO API |
| 배포 | Railway (백엔드 + Postgres) |
| 프론트엔드 | Next.js / Vercel (별도 저장소) |

---

## 2. 저장소 구조

```
ai-challenge-Backend/
├── backend/              # FastAPI 애플리케이션 (배포 단위)
│   ├── app/
│   │   ├── main.py       # 엔트리포인트: CORS, 라우터 등록, 스케줄러 기동
│   │   ├── database.py   # SQLAlchemy 엔진/세션
│   │   ├── models/       # ORM 모델 (10개)
│   │   ├── routers/      # API 라우트
│   │   ├── schemas/      # Pydantic 요청/응답 스키마
│   │   ├── services/     # 비즈니스 로직
│   │   ├── training/     # 통화 중 실시간 AI 파이프라인 커스터마이징
│   │   └── dependencies/ # 인증 등 FastAPI 의존성
│   ├── alembic/          # DB 마이그레이션 (12개)
│   ├── tests/            # pytest
│   ├── Dockerfile
│   ├── compose.yaml      # 로컬 개발용 (backend + Postgres)
│   └── start.sh          # 컨테이너 시작: 마이그레이션 후 uvicorn 기동
├── ai/                   # 통화 AI 패키지 (시나리오, TTS/STT 스트림, 로컬 테스트 하네스)
└── docs/                 # 문서
```

`Dockerfile`은 `backend/`를 `/app`으로, `ai/`를 `/packages/ai`로 복사합니다.

---

## 3. API 엔드포인트

| Method | Endpoint | 설명 |
|---|---|---|
| POST | `/v1/auth/signup/otp` | 가입용 SMS 인증번호 발송 |
| POST | `/v1/auth/signup/verify` | 인증번호 검증 |
| POST | `/v1/auth/signup` | 회원가입 |
| POST | `/v1/auth/login` | 로그인 |
| POST | `/v1/consents` | 훈련 참여 동의 제출 |
| GET | `/v1/sessions` | 내 훈련 세션 목록 |
| GET | `/v1/sessions/{id}` | 훈련 세션 상세 |
| POST | `/v1/sessions/{id}/calls` | 훈련 통화 시작 |
| GET | `/v1/sessions/{id}/report` | 훈련 리포트 조회 |
| POST | `/v1/webhooks/clawops/transcript` | ClawOps 전사 결과 수신 (서명 검증) |

> ⚠️ `docs/api-spec.md`는 `/trainings` 기반의 구버전 명세로, 현재 구현과 일치하지 않습니다.

---

## 4. 핵심 도메인 흐름

```
① 가입      SMS OTP 인증 → 회원가입 (개인정보/불시전화 동의 저장)
                ↓
② 세션 생성  훈련 세션(TrainingSession) 생성 + 동의(Consent) 기록
                ↓
③ 예고 통화  ClawOps로 발신 → AI가 보이스피싱 시나리오 연기
                ↓
④ 리포트    통화 종료 → 전사(transcript) 수집 → 대응 점수 리포트 생성
                ↓
⑤ 불시 통화  동의한 경우 30~60분 뒤 무작위 시점에 2차 훈련 자동 발신
```

세션 상태는 두 축으로 관리됩니다.

- `call_status`: `waiting` → `calling` → `completed` / `missed` / `silent` / `failed`
- `report_status`: `none` / `pending` / `draft` / `final` / `failed`

---

## 5. 통화 AI 파이프라인

통화 시작 시 [`call_service.py`](../backend/app/services/call_service.py)가 **두 가지 경로 중 하나**를 선택합니다.

### 경로 A: 커스텀 파이프라인 (기본)

`DEEPGRAM_API_KEY`와 `ELEVENLABS_API_KEY`가 **모두 설정된 경우** 사용됩니다.

```
전화 음성 → Deepgram STT → OpenAI/Gemini LLM → ElevenLabs TTS → 전화 음성
                                (상호 폴백)
```

`PhonePipelineSession`([`app/training/pipeline_session.py`](../backend/app/training/pipeline_session.py))이 ClawOps의 `PipelineSession`을 상속해 통화 품질 로직을 덧붙입니다.

- **인사말 가드**: 첫 3초간 barge-in(말 끊기) 차단 — 시나리오 도입부가 전달되도록
- **에코 필터**: 자기 TTS 음성이 STT로 되돌아오는 것 차단
- **턴 병합**: 한 문장이 여러 Deepgram final로 쪼개지는 것을 하나의 발화로 합침
- **반사 응답**: "안 들려요", "누구세요?" 등은 LLM 없이 시나리오 고정 답변으로 즉답 (지연 감소)
- **끊기 판정**: 상대가 끊겠다는 의사를 2회 표시하면 마무리 멘트 후 종료
- **재생 대기**: 마지막 멘트가 실제로 재생될 때까지 기다린 후 hangup

### 경로 B: ClawOps Realtime (폴백)

Deepgram/ElevenLabs 키가 없으면 `OpenAIRealtime` 또는 `GeminiRealtime`으로 전환됩니다.
ClawOps 에이전트가 STT·LLM·TTS를 통합 처리하며, 목소리는 provider preset(`marin`, `Kore` 등)을 사용합니다.

> 경로 B로 가면 위에 나열한 통화 품질 로직은 적용되지 않습니다.

### 시나리오

`ai/scenarios/`에 고정 시나리오가 정의돼 있으며, 시나리오마다 목소리(`tts_voice_id`)와 톤(`tts_stability` 등)을 다르게 배정할 수 있습니다.
`DYNAMIC_SCENARIO=true`로 두면 통화 직전에 LLM이 시나리오를 새로 생성합니다(기본값은 `false`, 고정 시나리오 무작위 선택).

---

## 6. 백그라운드 스케줄러

앱 기동 시 `main.py`의 lifespan에서 데몬 스레드로 시작됩니다.

| 스케줄러 | 파일 | 주기 | 역할 |
|---|---|---|---|
| 불시 훈련 | `services/training_scheduler.py` | 30초 | 예약된 불시 통화를 발신. 실패 시 재시도(기본 2회) |
| 개인정보 파기 | `services/data_retention.py` | 6시간 | 가입 30일 경과 참가자의 개인정보 삭제 |

**개인정보 파기 동작**: 참가자(`Participant`) 행을 삭제하면 `training_sessions.participant_id`가 FK의 `ON DELETE SET NULL`로 끊어집니다. 전화번호·비밀번호 해시는 사라지고, 훈련 세션·전사·리포트는 개인 식별 정보 없이 통계용으로 남습니다.

> 📌 개인정보 파기 기능은 현재 **working tree에만 존재하며 아직 커밋되지 않았습니다.**

---

## 7. 데이터 모델

```
Participant (참가자)
   │  phone_number, password_hash, 동의 플래그, created_at
   │
   └─< TrainingSession (훈련 세션)      ※ participant 삭제 시 NULL 처리
          │  call_status, report_status, current_training_type
          │
          ├── Consent           동의 스냅샷
          ├─< Call              ClawOps 통화 ID, 상태
          ├─< TranscriptTurn    발화 단위 전사 (source: live | clawops)
          └─< TrainingReport    대응 점수 리포트 (source: live | clawops)

PhoneVerification    가입 OTP (단기 만료)
ScheduledTraining    불시 훈련 예약 (pending/started/completed/failed/cancelled)
TranscriptEvent      ClawOps 웹훅 원본 (중복 수신 방지용 유니크 제약)
```

---

## 8. 인프라

### 로컬 개발

```bash
cd backend
cp .env.example .env    # 값 채우기
docker compose up --build
```

`compose.yaml`이 두 컨테이너를 띄웁니다.

- `backend` — 8000번 포트, `UVICORN_RELOAD=1`로 코드 변경 시 자동 리로드
- `db` — PostgreSQL 16, 호스트 **5433**번 포트 (로컬 Postgres와 충돌 방지)

테스트는 `DATABASE_URL`이 가리키는 DB 이름 뒤에 `_test`를 붙인 별도 DB에서 실행됩니다.
`conftest.py`가 운영 DB를 가리키고 있으면 실행을 거부하도록 안전장치가 걸려 있습니다.

### 배포 (Railway)

- `Dockerfile` 기반으로 백엔드 서비스 + Postgres 플러그인 구성
- 컨테이너 시작 시 `start.sh`가 `alembic upgrade head`를 최대 10회 재시도한 뒤 uvicorn 기동
- DB 연결은 `DATABASE_PUBLIC_URL` → `DATABASE_URL` 순으로 조회

### 프론트엔드 연동

Next.js 프론트엔드는 Vercel에 배포되며, `next.config.mjs`의 rewrite가 `/v1/*`를 백엔드로 프록시합니다.
백엔드는 `main.py`의 `CORSMiddleware`로 허용 origin을 관리합니다.

> ⚠️ **현재 `allow_origins`에 localhost만 등록돼 있습니다.** 배포된 프론트엔드에서 백엔드를 직접 호출하면 CORS 차단됩니다.
> Vercel preview 배포는 PR마다 URL이 바뀌므로, `allow_origin_regex` 또는 환경변수 주입 방식을 권장합니다.

---

## 9. 환경변수

전체 목록과 설명은 [`backend/.env.example`](../backend/.env.example)에 정리돼 있습니다. 핵심만 추리면:

| 변수 | 용도 |
|---|---|
| `DATABASE_URL` / `DATABASE_PUBLIC_URL` | DB 연결 (둘 중 하나 필수) |
| `JWT_SECRET` | 인증 토큰 서명 |
| `CLAWOPS_API_KEY` / `_ACCOUNT_ID` / `_SMS_FROM` | SMS 발송 |
| `CLAWOPS_PHONE_NUMBER` / `_UNANNOUNCED_PHONE_NUMBER` | 예고/불시 통화 발신번호 (분리 운영) |
| `CLAWOPS_WEBHOOK_SIGNING_SECRET` | 웹훅 서명 검증 |
| `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY` | 통화 파이프라인 (없으면 Realtime 폴백) |
| `OPENAI_API_KEY` / `GEMINI_API_KEY` | LLM (`CALL_LLM_PROVIDER`로 1순위 지정) |
| `PARTICIPANT_RETENTION_DAYS` | 개인정보 보존 기간 (기본 30) |

`backend/.env` 하나가 유일한 env 파일입니다. `ai/.env`를 다시 만들면 진입점마다 값이 갈리므로 만들지 마십시오.

---

## 10. 알려진 이슈

| 이슈 | 상태 |
|---|---|
| Railway 배포에서 `DATABASE_URL is not set`으로 마이그레이션 반복 실패 | 변수는 등록돼 있으나 값이 비었는지 / 환경이 일치하는지 확인 필요 |
| CORS에 배포 프론트엔드 origin 미등록 | `d44e676` 커밋에서 제거됨 |
| `docs/api-spec.md`가 구버전 명세 | 현재 구현과 불일치 |
| 개인정보 파기 기능 미커밋 | working tree에만 존재 |
