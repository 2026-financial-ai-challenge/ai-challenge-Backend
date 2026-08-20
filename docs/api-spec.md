# Phishing Call Backend API Specification

## 1. Overview

- Base URL: `/`
- Content-Type: `application/json`
- Authentication: None
- API Version: v1
- User Identification: `training_id`

---

## 2. API 목록

| ID | Method | Endpoint | 기능 |
|---|---|---|---|
| API-01 | POST | `/trainings` | 개인정보 동의, 전화번호 및 훈련 가능 시간 등록 |
| API-02 | GET | `/trainings/{training_id}` | 훈련 신청 및 진행 상태 조회 |
| API-03 | POST | `/trainings/{training_id}/calls/announced` | 예고형 모의전화 즉시 시작 |
| API-04 | POST | `/trainings/{training_id}/schedule` | 불시전화 스케줄 생성 |
| API-05 | DELETE | `/trainings/{training_id}` | 훈련 철회 및 예약 취소 |
| API-06 | GET | `/trainings/{training_id}/result` | 최종 훈련 결과 조회 |
| API-07 | POST | `/webhooks/voice` | 전화 연결 후 음성 처리 |
| API-08 | POST | `/webhooks/call-status` | 통화 상태 변경 수신 |