# API 명세

- Base URL: `https://<DOMAIN>` (로컬: `http://localhost:8000`)
- 모든 경로는 `/v1`로 시작하고, 요청·응답은 `application/json`입니다.
- 인증이 필요한 API는 `Authorization: Bearer <accessToken>` 헤더를 보냅니다.
- 서버 실행 중에는 `/docs`(Swagger UI)에서 스키마를 직접 확인할 수 있습니다.

## 목록

| Method | Endpoint | 인증 | 설명 |
| --- | --- | :---: | --- |
| POST | `/v1/auth/signup/otp` | | 가입 인증번호 문자 발송 |
| POST | `/v1/auth/signup/verify` | | 인증번호 확인 → 인증 토큰 발급 |
| POST | `/v1/auth/signup` | | 회원가입 |
| POST | `/v1/auth/login` | | 로그인 |
| POST | `/v1/consents` | ○ | 훈련 동의 → 세션 생성 + 1차 전화 발신 |
| GET | `/v1/sessions` | ○ | 내 훈련 세션 목록 |
| GET | `/v1/sessions/{sessionId}` | ○ | 훈련 세션 상세 |
| POST | `/v1/sessions/{sessionId}/calls` | ○ | 훈련 전화 다시 걸기 |
| GET | `/v1/sessions/{sessionId}/report` | ○ | 회차 리포트 |
| POST | `/v1/webhooks/clawops/status` | 서명 | ClawOps 통화 상태 수신 |
| POST | `/v1/webhooks/clawops/transcript` | 서명 | ClawOps 녹취록 완료 수신 |

## 오류 형식

```json
{ "message": "사용자에게 보여 줄 문구", "code": "ERROR_CODE" }
```

| HTTP | code | 상황 |
| --- | --- | --- |
| 400 | `INVALID_PHONE` | 010으로 시작하는 11자리가 아님 |
| 400 | `OTP_NOT_REQUESTED` / `OTP_INVALID` | 인증번호 미요청 / 불일치 |
| 400 | `INVALID_VERIFICATION_TOKEN` / `VERIFICATION_TOKEN_USED` | 인증 토큰 오류 / 재사용 |
| 400 | `WEAK_PASSWORD` | 영문+숫자 8자 이상이 아님 |
| 400 | `CONSENT_REQUIRED` | 필수 동의 미체크 |
| 401 | `AUTH_REQUIRED` / `INVALID_ACCESS_TOKEN` | 토큰 없음 / 만료·위조 |
| 401 | `INVALID_CREDENTIALS` | 전화번호 또는 비밀번호 불일치 |
| 404 | `SESSION_NOT_FOUND` | 세션이 없거나 본인 세션이 아님 |
| 409 | `PHONE_ALREADY_REGISTERED` | 이미 가입된 번호 |
| 409 | `CALL_IN_PROGRESS` / `PHONE_REQUIRED` | 이미 거는 중 / 번호 없음 |
| 429 | `OTP_COOLDOWN` / `OTP_LOCKED` | 재발송 대기 중 / 5회 실패로 잠김 |
| 502 | `SMS_SEND_FAILED` | 문자 발송 실패 |

---

## 인증

### POST `/v1/auth/signup/otp`

```json
// 요청
{ "phoneNumber": "01012345678" }
// 응답 200
{ "phoneNumberMasked": "010-****-5678", "expiresInSec": 300, "resendAvailableInSec": 60, "devCode": null }
```

인증번호는 6자리, 5분간 유효하며 60초 뒤 재발송할 수 있습니다. `devCode`는 `SMS_EXPOSE_DEV_CODE=true`인 개발 환경에서만 채워집니다.

### POST `/v1/auth/signup/verify`

```json
// 요청
{ "phoneNumber": "01012345678", "code": "123456" }
// 응답 200
{ "verificationToken": "…", "expiresInSec": 600 }
```

### POST `/v1/auth/signup`

```json
// 요청
{ "verificationToken": "…", "password": "abcd1234", "privacy": true, "unannouncedTraining": true }
// 응답 201
{
  "accessToken": "…", "tokenType": "bearer", "expiresInSec": 3600,
  "participant": { "id": 1, "phoneNumberMasked": "010-****-5678", "hasConsented": true }
}
```

### POST `/v1/auth/login`

```json
// 요청
{ "phoneNumber": "01012345678", "password": "abcd1234" }
```

응답은 회원가입과 같은 `AuthResponse`입니다(200).

---

## 훈련

### POST `/v1/consents`

동의를 기록하고 새 훈련 세션을 만든 뒤 곧바로 1차 전화를 겁니다. 이미 동의한 참가자는 본문 없이 호출해도 됩니다.

```json
// 요청
{ "privacy": true, "unannouncedTraining": true }
// 응답 200
{ "sessionId": "…" }
```

### GET `/v1/sessions` · GET `/v1/sessions/{sessionId}`

```json
// GET /v1/sessions/{sessionId} 응답 200  (목록은 { "sessions": [ … ] })
{
  "session": {
    "id": "…",
    "phoneNumberMasked": "010-****-5678",
    "callStatus": "completed",          // waiting | calling | completed | missed | failed
    "callId": "CA…",
    "reportStatus": "draft",            // none | pending | draft | final | failed
    "currentTrainingType": "announced", // announced(1차) | unannounced(불시)
    "consents": { "privacy": true, "unannouncedTraining": true, "consentedAt": "…" },
    "createdAt": "…",
    "updatedAt": "…"
  }
}
```

프론트엔드는 이 API를 주기적으로 조회해 통화 진행 상태를 보여 줍니다.

### POST `/v1/sessions/{sessionId}/calls`

부재중이거나 실패한 회차에서 전화를 다시 걸 때 씁니다.

```json
// 응답 200
{ "callId": null, "status": "waiting" }
```

### GET `/v1/sessions/{sessionId}/report`

```json
{
  "sessionId": "…",
  "callId": "CA…",
  "status": "final",
  "turns": [ { "role": "assistant", "text": "…" }, { "role": "user", "text": "…" } ],
  "draftTurns": [ … ],        // 1차 전화 녹취
  "unannouncedTurns": [ … ],  // 불시 전화 녹취
  "draft": TrainingReport,       // 1차 전화 리포트
  "unannounced": TrainingReport, // 불시 전화 리포트
  "final": TrainingReport,       // 두 통화 비교 (둘 다 있을 때만)
  "clawopsSummary": { … }
}
```

`TrainingReport`

| 필드 | 타입 | 설명 |
| --- | --- | --- |
| `score` | int (0~100) | 대응 점수 |
| `suspected` / `gaveName` / `triedHangup` | bool | 의심 표현 / 이름 제공 / 끊으려 시도 |
| `summary` / `coaching` | string | 통화 요약 / 다음 대응을 위한 조언 |
| `riskBehaviors` / `defenseBehaviors` | `{label, evidence}[]` | 위험·방어 행동과 근거 발화 |
| `source` | `clawops` \| `comparison` | 통화 리포트 / 비교 리포트 |

행동 라벨과 점수는 [`architecture.md`](architecture.md#32-행동-라벨과-가중치)를 참고하세요.

---

## 웹훅 (ClawOps → 백엔드)

`application/x-www-form-urlencoded`로 받고, `CLAWOPS_WEBHOOK_SIGNING_SECRET`이 설정돼 있으면 서명이 맞지 않는 요청을 401로 거부합니다. 처리 중 오류가 나거나 처리하지 않는 이벤트여도 204를 돌려줍니다. 실패 응답이 쌓이면 ClawOps가 웹훅을 꺼 버리기 때문입니다.

| Endpoint | 받는 값 | 처리 |
| --- | --- | --- |
| `/v1/webhooks/clawops/status` | `CallId` | API로 통화 상태를 다시 조회해 세션 상태 반영. 종료면 녹취록 요청·불시 전화 예약 |
| `/v1/webhooks/clawops/transcript` | `CallId`, `Event` | 원본을 통화·이벤트별 한 건으로 저장. `transcript.completed`면 녹취록을 받아 채점·리포트 저장 |
