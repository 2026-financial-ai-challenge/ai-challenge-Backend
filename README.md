# 안심피싱 — AI 보이스피싱 대응 훈련 (Backend)

AI가 실제 보이스피싱범처럼 참가자에게 **진짜 전화를 걸고**, 통화가 끝나면 참가자의 대응을 분석해 리포트로 돌려주는 훈련 서비스의 백엔드입니다.

- 1차(예고) 전화로 훈련하고, 동의한 참가자에게는 30~60분 뒤 **불시 전화**를 한 번 더 걸어 실제 상황에서도 대응이 유지되는지 확인합니다.
- 통화 녹취록에서 위험 행동(개인정보 제공, 송금 의사 등)과 방어 행동(신원 확인, 전화 종료 등)을 찾아 점수와 코칭을 만들고, 두 통화를 비교한 최종 리포트를 제공합니다.
- AI는 가상의 기관명만 쓰고 계좌·인증번호 같은 실제 범죄에 쓸 수 있는 정보를 말하지 않도록 안전 규칙을 지킵니다.

## 전체 구조

```
참가자 휴대폰 ◀──── 전화 ────▶ ClawOps 매니지드 음성 에이전트
                                  (실시간 음성 대화: OpenAI Realtime + Cartesia TTS)
     ▲                                   │ 통화 상태·녹취록 웹훅
     │ 웹                                 ▼
Next.js 프론트엔드 ──── /v1 API ────▶ FastAPI 백엔드 ──── PostgreSQL / Redis
 (Vercel, 별도 저장소)                     │
                                          ├ 발신 요청 (시나리오를 통화별 지시문으로 전달)
                                          ├ 녹취록 화자 분리 → Gemini/OpenAI 채점 → 리포트 저장
                                          └ 불시 전화 스케줄러 · 개인정보 파기 작업
```

통화 중 대화는 ClawOps 서버의 매니지드 에이전트가 진행합니다. 백엔드는 **어떤 시나리오로 누구에게 걸지 정하고, 통화가 끝난 뒤 결과를 채점**합니다.

## 저장소 구조

```
ai-challenge-Backend/
├── backend/        FastAPI 서버 (배포 단위) — API, 통화 발신, 채점, 스케줄러
├── ai/             시나리오 · 안전 규칙 · 행동 라벨 · 매니지드 에이전트 설정 (백엔드가 import)
├── docs/           구조 설명, API 명세, 보이스피싱 수법 조사 자료
└── .github/        develop 브랜치 자동 배포 (AWS Lightsail)
```

## 빠른 실행

```bash
cd backend
cp .env.example .env      # ClawOps 키, Gemini 키, JWT_SECRET 등 채우기
docker compose up --build # backend(8000) + Postgres(5433) + Redis(6379)
```

API 문서는 서버 실행 후 `http://localhost:8000/docs`(Swagger UI)에서 볼 수 있습니다.
테스트: `cd backend && pytest` (로컬 Postgres 필요, 운영 DB를 가리키면 실행을 거부합니다)

## 문서

| 문서 | 내용 |
| --- | --- |
| [`backend/README.md`](backend/README.md) | 백엔드 실행·배포 방법 |
| [`docs/architecture.md`](docs/architecture.md) | 통화 흐름, 채점 방식, 안전 장치, 데이터 모델 |
| [`docs/api-spec.md`](docs/api-spec.md) | REST API 명세 |
| [`AI_README.md`](AI_README.md) · [`ai/MANAGED_AGENT.md`](ai/MANAGED_AGENT.md) | 시나리오 엔진과 매니지드 에이전트 |
| [`docs/research/voice-phishing-patterns.md`](docs/research/voice-phishing-patterns.md) | 시나리오 설계 근거가 된 국내 보이스피싱 수법 조사 |

## 기술 스택

| 영역 | 사용 기술 |
| --- | --- |
| 서버 | FastAPI (Python 3.13), Uvicorn |
| 데이터 | PostgreSQL 16, SQLAlchemy 2.0, Alembic, Redis 7 |
| 전화·문자 | ClawOps (매니지드 음성 에이전트, 발신, SMS, 웹훅) |
| 통화 AI | OpenAI Realtime (`gpt-realtime-2.1`) + Cartesia TTS (`sonic-3.5`) |
| 리포트 채점 | Gemini / OpenAI (키가 없으면 키워드 기반 간이 채점) |
| 배포 | AWS Lightsail 서울, Docker Compose, Caddy(HTTPS), GitHub Actions |
