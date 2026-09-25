"""Runtime harness around the caller persona: what the model may say, and when
the call has to stop.

The system prompt (SAFETY_RULES in ai/safety.py) asks the model to behave. A
prompt is a request, not a guarantee -- a model that drifts, gets jailbroken by
the trainee, or simply hallucinates a number will say it out loud, and on a
phone call there is no taking it back. So the rules the prompt states are also
enforced here, in code, on the only path audio can take:

  layer 1  prompt           SAFETY_RULES + scenario        (asks)
  layer 2  OutputGuard      every sentence, before TTS     (blocks / rewrites)
  layer 3  CallMonitor      every turn, both speakers      (steers / ends)

Layer 2 and 3 are plain regular expressions and counters. They add
microseconds to a turn, never a network round trip, which is the reason they
exist as code instead of as a second "supervisor" LLM on the critical path.
(A supervisor model is still useful *off* the critical path -- see
docs/harness.md.)

What each layer owns:

- OutputGuard drops a sentence that breaks character, names a real
  institution, or asks the trainee for a secret (password, OTP, card or
  account number); it rewrites reusable tokens (URLs, phone numbers, long
  digit runs) and strips markup. It never touches the scenario's own
  pre-written lines -- those are checked once, in the test suite.
- CallMonitor watches the trainee as well. If the trainee starts reading out
  a long number (a real card or account number, on a recorded line), the
  caller cuts them off in character. If the trainee sounds like they are in
  real distress, the persona is dropped, the call is named as the consented
  exercise it is, and the line is closed. If the model keeps tripping the
  guard, the call is ended in character.
- GuardedLLM connects the two to the live LLM: it filters the token stream
  sentence by sentence and feeds the monitor's corrections back into the next
  turn's system prompt.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ai.safety import REAL_ORGS, sanitize_spoken_text

__all__ = [
    "CallMonitor",
    "GuardVerdict",
    "GuardedLLM",
    "MonitorAction",
    "OutputGuard",
    "SAFETY_EXIT_LINE",
    "SECRET_REDIRECT_LINE",
    "audit_transcript",
    "redact_numbers",
]

log = logging.getLogger("ai.harness")


# ── layer 2: per-sentence output guard ─────────────────────────────────────

# Anything that tells the trainee the caller is not a person. "훈련" and
# "시뮬레이션" are here because the persona must never name the exercise --
# only the monitor's safety exit may, deliberately.
_META = re.compile(
    r"(?<![A-Za-z])(AI|GPT|LLM)(?![A-Za-z])|인공\s*지능|언어\s*모델|챗봇|프롬프트|"
    r"시뮬레이션|훈련|가상의\s*(인물|상황|기관)|역할\s*극|롤\s*플레이|"
    r"(저는|나는)\s*(사람이\s*아니|기계)",
    re.IGNORECASE,
)

# One list for both the scenario text (checked in tests) and what the model
# actually says (checked here), so the two can never drift apart.
_REAL_ORGS = REAL_ORGS

# Asking the trainee to say a secret out loud. On a recorded training line a
# real password or card number is a real leak, whatever the scenario wants.
_SECRET = re.compile(
    r"비밀\s*번호|인증\s*번호|OTP|보안\s*카드|카드\s*번호|계좌\s*번호|"
    r"주민\s*(등록)?\s*번호|CVC|CVV|유효\s*기간|공인\s*인증|"
    r"(카드|통장)\s*(뒷|앞)면",
    re.IGNORECASE,
)
_ASK = re.compile(
    r"알려|불러|말씀해|말해|읽어|입력|눌러|주세요|주십시오|주시|확인해|적어|보내"
)
# "저희는 비밀번호는 절대 안 여쭙니다" names a secret without asking for it --
# and it is one of the most effective lines a scammer has, so it must survive.
_NEGATED = re.compile(r"않|안\s*(여쭙|묻|받)|말고|마세요|마십시오|절대|필요\s*없|묻지|여쭙지")

_MARKUP = re.compile(r"[*#_`>|\[\]{}]|^\s*[-•·]\s*|^\s*\d+[.)]\s+")
_STAGE_DIRECTION = re.compile(r"\([^)]*\)|（[^）]*）")
_SENTENCE_END = re.compile(r"[.!?。！？]\s*$")
_ANY_SENTENCE_END = re.compile(r"[.!?。！？](?=\s|$)")


@dataclass(frozen=True)
class GuardVerdict:
    text: str  # what may be spoken ("" = drop the sentence)
    violations: tuple[str, ...] = ()

    @property
    def dropped(self) -> bool:
        return not self.text.strip()


class OutputGuard:
    """Check one sentence of caller speech before it reaches TTS."""

    def check(self, sentence: str) -> GuardVerdict:
        raw = sentence or ""
        violations: list[str] = []

        if _META.search(raw):
            violations.append("persona_break")
        if _REAL_ORGS.search(raw):
            violations.append("real_org")
        if _SECRET.search(raw) and _ASK.search(raw) and not _NEGATED.search(raw):
            violations.append("secret_request")
        if violations:
            return GuardVerdict(text="", violations=tuple(violations))

        cleaned = _STAGE_DIRECTION.sub("", raw)
        cleaned = _MARKUP.sub("", cleaned)
        sanitized = sanitize_spoken_text(cleaned)
        if sanitized != cleaned:
            violations.append("reusable_token")
        sanitized = re.sub(r"\s{2,}", " ", sanitized).strip()
        return GuardVerdict(text=sanitized, violations=tuple(violations))


# ── layer 3: per-call monitor ──────────────────────────────────────────────

SAFETY_EXIT_LINE = os.getenv(
    "CALL_SAFETY_EXIT_LINE",
    "잠시만요, 통화를 멈추겠습니다. 지금 전화는 사전에 동의하신 보이스피싱 대응 훈련 전화였습니다. "
    "실제로 돈이나 정보를 보내실 일은 전혀 없습니다. 도움이 필요하시면 일일구나 가까운 분께 바로 연락하세요.",
)
SECRET_REDIRECT_LINE = os.getenv(
    "CALL_SECRET_REDIRECT_LINE",
    "아니요, 번호는 말씀하지 마십시오. 그건 저희가 받지 않습니다.",
)

# The trainee may be in real trouble, not playing along. Nothing about the
# exercise outranks this. Only physical or self-harm signals: fear and panic
# ("무서워요", "어떡해요") are what these scenarios are built to provoke, and
# ending the call on them would end most calls at their most useful moment.
_DISTRESS = re.compile(
    r"죽고\s*싶|자살|숨이\s*(안\s*쉬|막)|쓰러(졌|질\s*것)|구급차|119|일일구|"
    r"심장이\s*(아파|너무\s*뛰)|가슴이\s*(아파|조여)"
)
# A long run of digits in what the trainee says is a card, account or ID
# number being read out. Deepgram writes Korean numbers as digits, and spaces
# or dashes inside the run are how people read them in groups.
_SPOKEN_NUMBER = re.compile(r"(?:\d[\s-]*){6,}")


def redact_numbers(text: str) -> str:
    """What the trainee said, minus any number long enough to be a real one.

    Used for the history the model sees and the transcript the report keeps:
    a real card number said on a training call must not end up stored.
    """
    return _SPOKEN_NUMBER.sub("[번호 생략] ", text or "").strip()


@dataclass(frozen=True)
class MonitorAction:
    """What the pipeline must do with a trainee turn.

    kind:
      continue  -- answer normally
      redirect  -- speak `line` (in character) instead of answering
      exit      -- speak `line` and hang up
    """

    kind: str
    line: str = ""
    reason: str = ""


CONTINUE = MonitorAction("continue")


@dataclass
class CallMonitor:
    """Per-call state for layer 3. One instance per call, never shared."""

    scenario_id: str = ""
    hangup_line: str = ""
    max_violations: int = field(
        default_factory=lambda: int(os.getenv("CALL_GUARD_MAX_VIOLATIONS", "3"))
    )
    events: list[dict[str, Any]] = field(default_factory=list)
    _violations: int = 0
    _secret_disclosures: int = 0
    _corrections: list[str] = field(default_factory=list)
    _last_assistant: str = ""

    # trainee side ------------------------------------------------------
    def observe_user(self, text: str) -> MonitorAction:
        cleaned = (text or "").strip()
        if not cleaned:
            return CONTINUE
        if _DISTRESS.search(cleaned):
            self._record("distress", cleaned)
            return MonitorAction("exit", SAFETY_EXIT_LINE, "distress")
        if _SPOKEN_NUMBER.search(cleaned):
            self._secret_disclosures += 1
            self._record("secret_disclosure", "<redacted>")
            if self._secret_disclosures >= 2:
                return MonitorAction("exit", SAFETY_EXIT_LINE, "repeated_secret_disclosure")
            return MonitorAction("redirect", SECRET_REDIRECT_LINE, "secret_disclosure")
        if self._violations >= self.max_violations:
            self._record("guard_budget_spent", str(self._violations))
            return MonitorAction(
                "exit",
                self.hangup_line or "확인이 어려워서 이만 통화 마치겠습니다.",
                "guard_budget_spent",
            )
        return CONTINUE

    # caller side -------------------------------------------------------
    def observe_violation(self, violations: tuple[str, ...], sentence: str) -> None:
        if not violations:
            return
        blocking = [v for v in violations if v != "reusable_token"]
        self._record("guard", ",".join(violations), sentence=sentence[:80])
        if blocking:
            self._violations += 1
        for violation in violations:
            correction = _CORRECTIONS.get(violation)
            if correction and correction not in self._corrections:
                self._corrections.append(correction)

    def observe_assistant(self, text: str) -> None:
        spoken = (text or "").strip()
        if not spoken:
            return
        if self._last_assistant and _similar(spoken, self._last_assistant):
            self._record("repetition", spoken[:80])
            note = _CORRECTIONS["repetition"]
            if note not in self._corrections:
                self._corrections.append(note)
        self._last_assistant = spoken

    def take_corrections(self) -> list[str]:
        """Notes for the next LLM turn only; consumed once read."""
        out, self._corrections = self._corrections, []
        return out

    @property
    def violations(self) -> int:
        return self._violations

    def _record(self, kind: str, detail: str, **extra: Any) -> None:
        event = {"kind": kind, "detail": detail, "scenario": self.scenario_id, **extra}
        self.events.append(event)
        log.warning("HARNESS %s", event)


_CORRECTIONS = {
    "persona_break": "너는 끝까지 역할 속 인물이다. 자신이 무엇인지, 이 통화가 무엇인지 설명하지 않는다.",
    "real_org": "실제 기관 이름을 쓰지 않는다. 시나리오의 가상 기관명만 쓴다.",
    "secret_request": "비밀번호, 인증번호, 카드번호, 계좌번호를 말하라고 요구하지 않는다. 성함 같은 시나리오의 요구만 한다.",
    "reusable_token": "숫자를 길게 나열하거나 주소나 전화번호를 말하지 않는다.",
    "repetition": "직전과 같은 말을 반복했다. 다른 표현으로 한 단계 더 밀어붙인다.",
}


def _similar(a: str, b: str) -> bool:
    ca, cb = re.sub(r"\W", "", a), re.sub(r"\W", "", b)
    if not ca or not cb:
        return False
    return ca == cb or SequenceMatcher(None, ca, cb, autojunk=False).ratio() >= 0.85


# ── wiring: the live LLM behind both layers ────────────────────────────────

_MAX_SENTENCES_PER_TURN = int(os.getenv("CALL_MAX_SENTENCES", "3"))


class GuardedLLM:
    """Wrap any clawops-shaped LLM (`generate(messages, tools)` → tokens).

    Tokens are held only until a sentence ends -- which is also where the
    pipeline splits text for TTS, so this adds no audible delay -- then the
    sentence is checked and either passed, rewritten or dropped.
    """

    def __init__(self, inner: Any, *, guard: OutputGuard | None = None, monitor: CallMonitor | None = None) -> None:
        self._inner = inner
        self._guard = guard or OutputGuard()
        self._monitor = monitor
        # Sentences actually released in the current turn. The pipeline reads
        # this to record how much of an interrupted answer was heard.
        self.turn_sentences: list[str] = []

    # PhonePipelineSession, call_service and the tests read these off self._llm.
    @property
    def model(self) -> str:
        return self._inner.model

    @property
    def provider(self) -> str:
        return getattr(self._inner, "provider", "")

    @property
    def _max_tokens(self) -> int:
        return getattr(self._inner, "_max_tokens", 0)

    @property
    def inner(self) -> Any:
        return self._inner

    async def warm(self) -> None:
        warm = getattr(self._inner, "warm", None)
        if callable(warm):
            await warm()

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        self.turn_sentences = []
        released = 0
        buffer = ""

        async def release(sentence: str) -> AsyncIterator[str]:
            nonlocal released
            if released >= _MAX_SENTENCES_PER_TURN:
                return
            verdict = self._guard.check(sentence)
            if verdict.violations and self._monitor is not None:
                self._monitor.observe_violation(verdict.violations, sentence)
            if verdict.dropped:
                log.warning("HARNESS dropped sentence: %s", sentence[:80])
                return
            released += 1
            self.turn_sentences.append(verdict.text)
            yield verdict.text + ("" if _SENTENCE_END.search(verdict.text) else ".") + " "

        async for token in self._inner.generate(_with_corrections(messages, self._monitor), tools=tools):
            if token.startswith('{"type":"tool_calls"') or token.startswith('{"type": "tool_calls"'):
                if buffer.strip():
                    async for out in release(buffer):
                        yield out
                    buffer = ""
                yield token
                continue
            buffer += token
            # A token can close one sentence and open the next ("다. 그"), so
            # split on every boundary inside the buffer, not just its end.
            while True:
                match = _ANY_SENTENCE_END.search(buffer)
                if not match:
                    break
                sentence, buffer = buffer[: match.end()], buffer[match.end():]
                if sentence.strip():
                    async for out in release(sentence.strip()):
                        yield out
        if buffer.strip():
            async for out in release(buffer.strip()):
                yield out

        if self._monitor is not None and self.turn_sentences:
            self._monitor.observe_assistant(" ".join(self.turn_sentences))


# ── post-call audit (managed agents) ───────────────────────────────────────

# The one line allowed to name the exercise: the emergency exit in
# ai/managed_agent.py. Seeing it is an event to record, not a violation.
_SAFETY_EXIT_MARK = re.compile(r"사전에\s*동의하신\s*보이스피싱\s*대응\s*훈련")


def audit_transcript(agent_texts: list[str]) -> list[dict[str, Any]]:
    """Check what a managed agent actually said, after the call.

    A managed agent's words never pass through our code on the way out, so
    OutputGuard cannot block them. Running the same checks over the
    transcript at least records every slip, so the instructions can be
    fixed and the report can flag the call.

    Returns [{"index", "kind", "text"}]. kind is a guard violation
    (persona_break, real_org, secret_request, reusable_token) or
    "safety_exit" when the agent used the emergency exit.
    """
    guard = OutputGuard()
    findings: list[dict[str, Any]] = []
    for index, text in enumerate(agent_texts):
        spoken = (text or "").strip()
        if not spoken:
            continue
        if _SAFETY_EXIT_MARK.search(spoken):
            findings.append({"index": index, "kind": "safety_exit", "text": spoken})
            continue
        # Sentence by sentence, like the live guard: one bad sentence should
        # not hide behind a harmless one in the same segment.
        for sentence in re.split(r"(?<=[.!?。！？])\s+", spoken):
            for violation in guard.check(sentence).violations:
                findings.append({"index": index, "kind": violation, "text": sentence})
    return findings


def _with_corrections(messages: list[dict[str, Any]], monitor: CallMonitor | None) -> list[dict[str, Any]]:
    """Fold the monitor's notes into the existing system message.

    Merged into the first system message rather than added as a second one:
    some clients (AnthropicLLM among them) keep only the last system message,
    which would silently drop the whole scenario prompt.
    """
    if monitor is None:
        return messages
    notes = monitor.take_corrections()
    if not notes:
        return messages
    block = "\n\n[감독 지시 — 이번 응답에 반드시 반영]\n" + "\n".join(f"- {n}" for n in notes)
    out = [dict(m) for m in messages]
    for message in out:
        if message.get("role") == "system":
            message["content"] = f"{message.get('content') or ''}{block}"
            return out
    return [{"role": "system", "content": block.strip()}, *out]
