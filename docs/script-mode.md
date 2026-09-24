# 대본 모드 (방안 B) — 운영과 적중률 측정

## 무엇인가

실제 사기 조직처럼 **대본을 들고 거는** 모드입니다. 훈련자 발화의 의도를 규칙으로 읽고,
확실하면 미리 써 두고 **미리 합성해 둔** 대사를 즉시 재생합니다. 확실하지 않으면
기존대로 실시간 LLM이 답합니다.

```
훈련자 말 끝 ─▶ 의도 분류 (정규식, ~0ms) ─┬─ 확실 ─▶ 미리 합성한 대사 재생 (LLM·TTS 0회)
                                         └─ 불확실 ─▶ 실시간 LLM (하네스 경유)
```

| 파일 | 역할 |
| --- | --- |
| `ai/scenarios/intents.py` | 훈련자 의도 14종 분류. 길거나(30자 초과) 두 의도가 섞이면 넘김 |
| `ai/scenarios/script.py` | `ScriptRouter`. 동의(`ack`)면 진행 대사(progression) 다음 줄, 그 외엔 의도별 답변. 한 통화에서 같은 줄 반복 금지 |
| `ai/scenarios/library.py` | 시나리오별 `progression` / `script` 대사 |
| `ai/prerender.py` | 대사를 ElevenLabs REST로 `ulaw_8000`(전화망 원형)으로 합성·디스크 캐시 |
| `ai/script_eval.py` | 적중률(coverage)·정확도(precision) 측정 |

## 켜는 순서

```
1) CALL_SCRIPT_MODE=shadow   (기본값) 실제 통화 수집. 답은 LLM이 하고 대본 판단만 로그로 남김
2) python -m ai.script_eval --logs backend.log         → 적중률
3) python -m ai.script_eval --logs backend.log --sample 100 --out labels.csv
   labels.csv의 ok 열에 y/n 기입 → python -m ai.script_eval --labels labels.csv  → 정확도
4) 기준 통과 시: python -m ai.prerender   (배포 전 전체 대사 미리 합성)
5) CALL_SCRIPT_MODE=on
```

## 적중률은 이렇게 잽니다

### 정의

| 지표 | 식 | 의미 |
| --- | --- | --- |
| **coverage (적중률)** | 대본 적중 턴 ÷ 훈련자 턴 (종료 의사 턴 제외) | 몇 %의 턴이 즉답되는가 → **지연** |
| **precision (정확도)** | 적절한 적중 ÷ 표본 적중 (사람이 판정) | 즉답이 맞는 답이었나 → **자연스러움** |

종료 의사("끊겠습니다")는 원래 고정 경로로 답하므로 분모에서 뺍니다.

**coverage만 보면 안 됩니다.** 정규식을 넓히면 coverage는 오르지만 엉뚱한 즉답이 늘어
오히려 더 기계처럼 들립니다. 반드시 둘을 함께 봅니다.

### 데이터 소스

| 소스 | 명령 | 언제 |
| --- | --- | --- |
| 실통화 shadow 로그 | `--logs FILE` | **기본.** 실제 훈련자 발화에 대한 라우터 판단이 그대로 기록됨. 위험 0 |
| DB 전사 재생 | `--db "$DATABASE_URL" [--scenario ID]` | 패턴·대사를 고친 뒤 과거 통화로 회귀 확인 |
| 임의 JSONL | `--jsonl FILE` | `{"session_id","role","text"}` 행 |

로그 한 줄의 형식: `SCRIPT {"mode":"shadow","scenario":"...","text":"...","intent":"deny","reason":"hit","hit":true,"line":"..."}`

`reason`이 LLM으로 넘어간 이유입니다: `no_intent`(패턴 없음) · `long`(30자 초과) ·
`ambiguous`(의도 둘) · `no_line`(시나리오에 대사 없음) · `exhausted`(대사 소진).

### 개선 루프

리포트 하단의 두 목록이 곧 할 일입니다.

- **top unmatched utterances** → `ai/scenarios/intents.py`에 패턴 추가
- **known intent but no line left** → 해당 시나리오 `script`에 변형 대사 추가

패턴을 고친 뒤에는 `--db`로 과거 통화를 다시 돌려 coverage가 오르고, `--labels`로
precision이 유지되는지 확인합니다.

> 참고: 개발 중 제가 만든 가상 발화 31개로 돌렸을 때 첫 결과는 64.5%였고, 빠진 표현을
> 패턴에 넣은 뒤 96.8%가 됐습니다. 이 숫자는 **튜닝에 쓴 데이터로 잰 것이라 실제 기대치가
> 아닙니다.** 실제 수치는 shadow 로그로만 알 수 있습니다.

## 합격 기준과 B → C 전환 판단

| 결과 (shadow 2주 또는 실통화 100건 이상) | 판단 |
| --- | --- |
| coverage ≥ 70% **그리고** precision ≥ 90% | **B 유지, `on` 전환** |
| coverage 50~70% | 패턴·대사 보강 2회 반복 후 재측정 |
| 보강 2회 후에도 coverage < 50%, 또는 precision < 85% | **C(음성-음성 실시간 모델) 제안 단계** |
| `on`에서 대사↔실시간 음색 차이가 거슬린다는 피드백 | 먼저 `ELEVENLABS_PRERENDER_MODEL_ID`를 비워 같은 모델로 통일. 그래도면 C 검토 |

### C로 갈 경우 미리 알아야 할 것 (clawops 0.57.0 기준 검토)

- 코드는 이미 있습니다: `call_service._make_realtime_agent()` (`OpenAIRealtime` / `GeminiRealtime`).
  clawops 기준 OpenAI Realtime(`gpt-realtime-2`)은 **실통화 검증 완료**, Gemini Live는 검증 전입니다.
- **잃는 것:** ElevenLabs 목소리(복제·선택), 문장 단위 하네스(2층), 즉답·대본 경로.
  하네스는 1층(프롬프트)만 남습니다(docs/harness.md).
- **얻는 것:** 끼어들기·맞장구 처리를 모델이 직접 함, 응답 억양이 문맥을 따라감.
- **중간 경로:** clawops ≥ 0.52의 `LiveKitSession`으로 LiveKit Agents를 돌리면
  `silero.VAD` + 한국어 지원 `TurnDetector` + 원하는 TTS(ElevenLabs 포함)를 쓰면서
  `llm_node`/`tts_node` 오버라이드로 하네스도 유지할 수 있습니다. 다만 clawops 업그레이드
  (0.46.1 → 0.57.0)가 선행돼야 합니다. 그 사이 breaking change(`MessageCreateParams` 생성자,
  수신거부 `number`→`recipient`)는 이 저장소가 쓰는 방식(`messages.create(to=, from_=, body=)`
  키워드 호출)에는 해당하지 않는 것으로 보이지만, 올릴 때 문자 인증 발송을 한 번 실제로 확인하십시오.
