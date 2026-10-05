"""Training calls run by ClawOps managed agents.

Instead of our server running the call (STT -> LLM -> TTS, see
backend/app/training/pipeline_session.py), ClawOps runs it: we hand over a
per-call instruction when dialing and read the transcript afterwards.

Two agent variants are compared:

  external_tts  OpenAI Realtime understands and writes the reply,
                Cartesia sonic-3.5 speaks it in a Korean voice.
  live          GPT-Live, full duplex: listens while it speaks, so
                backchannels ("네", "음") and interruptions are handled by
                the model itself.

A managed agent carries one fixed voice, and each scenario is a different
persona, so there is one agent per (scenario, variant): 5 x 2 = 10 agents,
named ``spc-<scenario_id>-<variant>``. The agent holds only the shared rules
(safety, speaking style, phone rules). The scenario itself travels with each
call as the CallContext instruction, so editing a scenario never requires
touching the agents.

What the backend uses (docs: 백엔드_통합_안내서_v2.pdf, B5):

    variant  = pick_variant()
    agent_id = resolve_agent_id(scenario.id, variant)
    context  = build_call_context(scenario)
    clawops.calls.create(to=..., from_=..., agent_id=agent_id, call_context=context)

What the AI owner runs (no backend needed):

    python -m ai.managed_agent sync                 # create/update the 10 agents
    python -m ai.managed_agent sync --dry-run       # show the payloads only
    python -m ai.managed_agent list --variant external_tts   # ids, unsafe agents, missing names
    python -m ai.managed_agent context --scenario investigation_unit
    python -m ai.managed_agent call --to 010XXXXXXXX --scenario bank_security_hold \\
        --variant live --wait                       # PoC test call
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.safety import SAFETY_RULES  # noqa: E402
from ai.scenarios import SCENARIOS, get_scenario  # noqa: E402
from ai.scenarios.library import DEFAULT_PLAYBOOK_ID, PLAYBOOKS  # noqa: E402
from ai.scenarios.playbook import _STYLE_RULES, Playbook, build_scenario_block  # noqa: E402

__all__ = [
    "AGENT_PREFIX",
    "VARIANTS",
    "agent_name",
    "agent_payload",
    "base_instructions",
    "build_call_context",
    "pick_variant",
    "resolve_agent_id",
]

VARIANTS: tuple[str, ...] = ("external_tts", "live")
AGENT_PREFIX = "spc"
# ClawOps caps a CallContext instruction at 4,000 characters.
CALL_CONTEXT_LIMIT = 4000
_API_BASE = os.getenv("CLAWOPS_API_BASE", "https://api.claw-ops.com").rstrip("/")

# ── casting ────────────────────────────────────────────────────────────────
# One voice per persona. Cartesia ids are ClawOps' Korean voices
# (docs.claw-ops.com/agents, "Cartesia" table). Picked by the voice names;
# listen to a test call and swap freely -- only `sync` has to run afterwards.
CARTESIA_VOICES: dict[str, str] = {
    "bank_security_hold": "e1717dc3-b87b-4720-aa7f-b6db290e0609",      # Taehyun - Friendly Host
    "low_interest_loan": "69c18e1d-fab0-4747-b9da-58617cd8b9e4",       # Soyeon - Bright Companion
    "ipo_allocation": "15628352-2ede-4f1b-89e6-ceda0c983fbc",  # Jiwoo - Service Specialist
    "card_delivery": "537a82ae-4926-4bfb-9aec-aff0b80a12a5",           # Minho (10-01 콘솔에서 고른 목소리)
    "investigation_unit": "89f4372f-1f73-4b85-8e1e-5d24ed8bc826",      # Jaewon - Steady Advisor
}
# GPT-Live also accepts the Realtime voices; these five have known characters.
LIVE_VOICES: dict[str, str] = {
    "bank_security_hold": "cedar",
    "low_interest_loan": "coral",
    "ipo_allocation": "sage",
    "card_delivery": "ballad",
    "investigation_unit": "ash",
}
CARTESIA_SPEED: dict[str, float] = {}
# 모든 시나리오가 상위 모델을 쓴다. mini는 반대 의미 문장과 이름 오인식이 나왔고(10-03 테스트), 응답 지연은 차이가 없었다.
REALTIME_MODEL = "gpt-realtime-2.1"

# 거절 횟수를 세게 했더니 모델이 세지 못해 여섯 번 거절에도 경고하지 않았다(10-05 통화 CA948a3a8cc86b541ecaa4956fc6699065).
# 그래서 거절 바로 뒤에 포기 경고, 그다음 거절에 종료한다. '끊을게요'처럼 셀 필요 없이 자기가 한 말만 보면 된다.
_PHONE_RULES = """
[전화 규칙]
- 전화가 연결되면 통화별 지시의 [첫 마디]를 토씨 하나 바꾸지 않고 그대로 말한다. 그 뒤로 다시 인사하지 않는다.
- [첫 마디]의 마지막 문장에서 말을 멈추고 상대 대답을 기다린다. "여보세요?", "들리십니까?" 같은 말을 이어 붙이지 않는다. 사건 설명은 상대가 대답한 다음에 시작한다.
- [첫 마디] 앞에는 어떤 말도 붙이지 않는다. "이제 ~처럼 말해 볼게요", "시작하겠습니다" 같은 연기 예고나 역할 설명을 절대 하지 않는다. 너는 연기자가 아니라 그 사람이다.
- [역할]에 정한 말투(반말 또는 존댓말)를 통화 끝까지 바꾸지 않는다. 반말 역할이면 "잠깐만요", "말씀", "죄송합니다" 같은 존댓말 표현도 쓰지 않고 "잠깐만", "미안해"처럼 말한다. 이 규칙에 적힌 예시 문구도 [역할]의 말투로 바꿔 말한다.
- 통화 끝까지 한국어로만 말한다. 상대가 영어나 다른 언어로 말해도 따라 바꾸지 않고 한국어로 답한다.
- 한 번에 문장 둘까지만 말하고 상대에게 차례를 넘긴다.
- 앞에서 한 문장을 다시 쓰지 않는다. 상대가 되묻거나 망설이거나 의심하면 같은 요구는 두 번까지만 하고, 그 뒤로는 새로운 이유나 사정을 대거나 [진행]의 다음 단계로 넘어간다. 거절에는 아래 거절 규칙을 따른다.
- 상대가 "네", "음", "아" 처럼 짧게 맞장구만 치면 멈추지 말고 하던 말을 이어 간다.
- 상대가 말을 끊고 들어오면 바로 멈추고, 방금 들은 말에 답한다.
- 끊겠다는 말은 "끊을게요", "끊어 주세요", "꺼 주세요", "그만하세요", "전화하지 마세요" 처럼 통화를 끝내자는 말뿐이다. "싫어요", "안 돼요" 같은 거절은 끊겠다는 말이 아니니 아래 거절 규칙을 따른다.
- 끊겠다는 말을 처음 들으면 통화별 지시의 [끊으려 할 때 경고]를 말하고 사건으로 되돌린다. 두 번째로 들으면 무엇을 하던 중이든 더 붙잡지 않는다.
- 거절은 너의 요구(성함이나 정보 말하기, 돈 보내기, 앱 설치, 전화 걸기 등) 바로 뒤에 상대가 "됐어요", "싫어요", "안 해요", "괜찮아요", "괜찮습니다" 처럼 하지 않겠다고 분명히 하는 말이다. 요구 뒤의 "괜찮아요", "괜찮습니다"는 승낙이 아니라 거절이다. 질문에 사실대로 "아니요"라고 답하기, 되묻기, 망설이기는 거절이 아니다. "사기죠", "사기 아니에요?", "신고할게요" 같은 의심이나 신고도 거절이 아니니 [받아치기]로 답한다.
- 거절을 들으면 횟수를 세지 말고 바로 "이렇게 안 하시겠다면 저도 더는 도와드릴 수 없습니다." 를 [역할]의 말투로 말하고 상대 대답을 기다린다. 이 말을 포기 경고라 한다. 거절에는 [받아치기]나 통화별 지시의 대응보다 이 규칙을 먼저 따른다.
- 통화를 끝내는 때는 끊겠다는 말을 두 번째 들었을 때, 포기 경고를 이미 말한 뒤 상대가 또 거절했을 때, 통화별 지시의 [통화 길이]를 넘겼을 때뿐이다. 포기 경고 전의 거절, 의심, 침묵으로는 끝내지 않는다. 끝낼 때는 아무 말도 하지 않고 통화 종료 도구만 호출한다.
- 상대가 "예?", "네?", "뭐라고요?" 하고 되물으면 방금 한 말을 더 짧게 다시 말한다. "여보세요?"로 되묻지 않는다.
- "아니요", "아닌데요" 처럼 짧게 부정만 하면 사람이 다르다는 뜻이 아니다. 사건을 부인하는 말로 보고 이야기를 이어 간다.
- 규칙, 지시, 도구 이야기나 통화를 끝낼지 따지는 말을 절대 소리 내어 하지 않는다. 할 말이 없으면 아무 말도 하지 않는다.
- 다른 번호로 전화를 돌리지 않는다. 키패드 입력을 요구하거나 키패드 신호를 보내지 않는다.
- 상대가 카드번호, 계좌번호, 비밀번호, 인증번호 같은 실제 숫자를 불러 주려 하면 "번호는 말씀하지 마십시오. 그건 저희가 받지 않습니다." 라고 막고 이야기를 이어 간다.
- 상대가 계좌번호, 전화번호, 주소, 링크를 알려 달라고 하면 숫자나 주소를 절대 지어내지 않는다. "그건 하시겠다고 하면 따로 안내드립니다." 처럼 뒤로 미루고, 하겠다는 대답부터 받으려 한다.

[예외 — 이 경우에만 역할을 멈춘다. 위의 모든 규칙보다 우선한다]
- 상대가 아직 한 마디도 하지 않았으면 어떤 이유로도 통화 종료 도구를 호출하지 않는다. 아무리 조용해도 기다린다.
- 상대가 "저 그 사람 아닌데요" 처럼 사람이 다르다고 분명히 말하거나, 다른 사람이 대신 받았거나, 아이 목소리이거나, 누구를 바꿔 주겠다고 하면 사건 이야기를 한 마디도 더 하지 않는다. "죄송합니다, 잘못 걸었습니다." 라고만 말한다. 통화 종료 도구는 호출하지 않고, 그 뒤로는 아무 말도 하지 않는다.
- 상대가 숨이 안 쉬어진다, 가슴이 아프다, 쓰러질 것 같다, 죽고 싶다처럼 실제 위급한 상태를 말하면 즉시 역할을 멈추고 아래 말을 한다. 통화 종료 도구는 호출하지 않는다. 상대가 다시 말하면 역할로 돌아가지 않고 이 안내를 짧게 다시 해 준다:
  "잠시만요, 통화를 멈추겠습니다. 지금 전화는 사전에 동의하신 보이스피싱 대응 훈련 전화였습니다. 실제로 돈이나 정보를 보내실 일은 전혀 없습니다. 도움이 필요하시면 일일구나 가까운 분께 바로 연락하세요."
- 무섭다, 어떡하냐, 신고하겠다는 말은 위급 상황이 아니다. 역할을 유지한다.
""".strip()


def base_instructions() -> str:
    """What every agent carries: safety, speaking style, phone rules."""
    return "\n\n".join([SAFETY_RULES, _STYLE_RULES, _PHONE_RULES])


# ── per call ───────────────────────────────────────────────────────────────


def _playbook_for(scenario) -> Playbook:
    """The playbook behind a scenario (unknown ids resolve like get_scenario)."""
    wanted = getattr(scenario, "id", scenario)
    by_id = {pb.id: pb for pb in PLAYBOOKS}
    if wanted in by_id:
        return by_id[wanted]
    resolved = get_scenario(str(wanted))
    for pb in PLAYBOOKS:
        if pb.name == resolved.name:
            return pb
    return by_id[DEFAULT_PLAYBOOK_ID]


def _reply_examples(playbook: Playbook) -> str:
    """One pre-written answer per trainee move, as reference for the model.

    These were the script mode's verbatim lines. Here they only show the
    model what a good answer to each kind of push-back sounds like.
    """
    labels = {
        "deny": "아니라고 하면", "verify_request": "신원을 확인하려 하면", "callback": "다시 걸겠다고 하면",
        "refuse_info": "정보를 거부하면", "consult_other": "가족·은행에 물어보겠다고 하면",
        "ask_detail": "자세히 물으면", "angry": "화를 내면", "police": "신고하겠다고 하면",
    }
    lines = [f"- {labels.get(reply.intent, reply.intent)}: {reply.lines[0]}"
             for reply in playbook.script if reply.lines]
    return "[상대 반응별 받아치기 예시]\n" + "\n".join(lines) if lines else ""


def _progression_examples(playbook: Playbook) -> str:
    """단계별 대사 예시. 낭독용이 아니라 단계마다 압박 강도를 보여 주는 참고용."""
    if not playbook.progression:
        return ""
    lines = [f"{i}. {line}" for i, line in enumerate(playbook.progression, 1)]
    return "[단계별 대사 예시 — 그대로 읽지 말고 상대 말에 맞춰 바꿔 말한다]\n" + "\n".join(lines)


def build_call_context(scenario) -> dict[str, Any]:
    """CallContext for one call: {"instruction": str, "variables": dict}.

    The shape matches clawops' ``calls.create(call_context=...)``.
    """
    playbook = _playbook_for(scenario)
    head = f"[첫 마디]\n{playbook.opening_line}"
    tail = (
        # 종료 도구와 같은 차례에 한 말은 재생 전에 끊기므로(ClawOps 매니지드 에이전트), 끊기 직전이 아니라 첫 번째 끊겠다는 말에 쓴다.
        f"[끊으려 할 때 경고]\n{playbook.hangup_line}\n\n"
        f"[통화 길이]\n상대 발화 기준 최대 {playbook.max_turns}번이다."
    )
    block = build_scenario_block(playbook)
    # 예시는 선택 항목이다. 한도를 넘으면 단계별 예시 → 받아치기 예시 순으로 뺀다.
    for extras in (
        [_progression_examples(playbook), _reply_examples(playbook)],
        [_reply_examples(playbook)],
        [],
    ):
        instruction = "\n\n".join(p for p in [head, block, *extras, tail] if p)
        if len(instruction) <= CALL_CONTEXT_LIMIT:
            break
    if len(instruction) > CALL_CONTEXT_LIMIT:
        raise ValueError(f"call context for {playbook.id} is {len(instruction)} chars (> {CALL_CONTEXT_LIMIT})")
    return {
        "instruction": instruction,
        "variables": {
            "scenario_id": getattr(scenario, "id", playbook.id),
            "max_turns": playbook.max_turns,
        },
    }


def pick_variant() -> str:
    """Which agent variant this call uses.

    CALL_AGENT_VARIANT = external_tts (default) | live | ab (random per call).
    The backend stores the result on the call so the two can be compared.
    """
    mode = os.getenv("CALL_AGENT_VARIANT", "external_tts").strip().lower()
    if mode == "ab":
        return secrets.choice(VARIANTS)
    return mode if mode in VARIANTS else "external_tts"


# ── agents ─────────────────────────────────────────────────────────────────


def agent_name(scenario_id: str, variant: str) -> str:
    return f"{AGENT_PREFIX}-{scenario_id}-{variant}"


def agent_payload(playbook: Playbook, variant: str) -> dict[str, Any]:
    """Create/update body for one agent."""
    if variant == "external_tts":
        configuration: dict[str, Any] = {
            "outputMode": "external_tts",
            "language": "ko",
            "llm": {
                "provider": "openai-realtime",
                "model": os.getenv("MANAGED_AGENT_REALTIME_MODEL") or REALTIME_MODEL,
                # 전화 회선은 far_field 권장(문서). 생략하면 노이즈 감소를 안 한다.
                "input_audio_noise_reduction": "far_field",
            },
            # 기본값. 0.6에서는 짧은 "아니요"를 놓쳤다.
            "vad": {"provider": "silero", "activation_threshold": 0.5},
            # 말 끝 판정 대기(min_silence 0.4, endpointing 0.2)를 줄여도 응답 지연 2.4초가 줄지 않아 기본값으로 둔다.
            # 0.6초보다 짧은 소리(주변 잡음)에는 AI가 말을 멈추지 않는다.
            "session": {"allow_interruptions": True, "min_interruption_duration": 0.6},
            "tts": {
                "provider": "cartesia",
                "model": "sonic-3.5",
                "voice": CARTESIA_VOICES[playbook.id],
                "speed": CARTESIA_SPEED.get(playbook.id, 1.0),
                "volume": 1.0,
            },
        }
    elif variant == "live":
        configuration = {
            "outputMode": "live",
            "language": "ko",
            "llm": {
                "provider": "openai-live",
                "model": "gpt-live-1",
                "voice": LIVE_VOICES[playbook.id],
                "backend_model": os.getenv("MANAGED_AGENT_LIVE_BACKEND", "gpt-5.6-luna"),
                # Role-play needs no deliberation; reasoning is latency.
                "backend_reasoning_effort": "none",
            },
        }
    else:
        raise ValueError(f"unknown variant: {variant}")
    return {
        "name": agent_name(playbook.id, variant),
        "instructions": base_instructions(),
        # False로 두면 첫 마디를 아예 건너뛰어 사건 맥락이 사라졌다(10-03 테스트).
        "greeting": True,
        "configuration": configuration,
    }


class ClawOpsREST:
    """The few REST calls this module needs.

    The agents API is not in the clawops Python SDK, and plain JSON keeps the
    transcript readable whatever speaker format the server sends.
    """

    def __init__(self, *, api_key: str | None = None, account_id: str | None = None, client=None) -> None:
        self.api_key = (api_key or os.getenv("CLAWOPS_API_KEY", "")).strip()
        self.account_id = (account_id or os.getenv("CLAWOPS_ACCOUNT_ID", "")).strip()
        if not self.api_key or not self.account_id:
            raise RuntimeError("CLAWOPS_API_KEY and CLAWOPS_ACCOUNT_ID must be set")
        if client is None:
            import httpx

            client = httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
        self._http = client

    def _url(self, path: str) -> str:
        return f"{_API_BASE}/v1/accounts/{self.account_id}{path}"

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self._http.request(
            method, self._url(path),
            headers={"Authorization": f"Bearer {self.api_key}"}, **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClawOps {method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    def list_agents(self) -> list[dict]:
        return list((self._request("GET", "/agents") or {}).get("data", []))

    def create_agent(self, body: dict) -> dict:
        return self._request("POST", "/agents", json=body)

    def update_agent(self, agent_id: str, body: dict) -> dict:
        return self._request("PATCH", f"/agents/{agent_id}", json=body)

    def create_call(self, *, to: str, from_: str, agent_id: str, call_context: dict, timeout: int = 30) -> dict:
        body = {
            "To": to, "From": from_, "AgentId": agent_id, "Timeout": timeout,
            "CallContext": {"Instruction": call_context["instruction"],
                            "Variables": call_context.get("variables") or {}},
        }
        return self._request("POST", "/calls", json=body)

    def get_call(self, call_id: str) -> dict:
        return self._request("GET", f"/calls/{call_id}")

    def request_transcript(self, call_id: str) -> dict:
        return self._request("POST", f"/calls/{call_id}/transcript")

    def get_transcript(self, call_id: str) -> dict:
        return self._request("GET", f"/calls/{call_id}/transcript")

    def get_summary(self, call_id: str) -> dict:
        return self._request("GET", f"/calls/{call_id}/summary")


_AGENT_IDS: dict[str, str] = {}


def resolve_agent_id(scenario_id: str, variant: str, *, rest: ClawOpsREST | None = None) -> str:
    """Agent id for (scenario, variant), looked up by name and cached.

    An unknown scenario id (CALL_SCENARIO naming a type with no playbook)
    uses the playbook get_scenario() falls back to.
    """
    playbook = _playbook_for(scenario_id)
    name = agent_name(playbook.id, variant)
    if name not in _AGENT_IDS:
        rest = rest or ClawOpsREST()
        _AGENT_IDS.clear()
        _AGENT_IDS.update({a.get("name"): a.get("agentId") for a in rest.list_agents() if a.get("name")})
    agent_id = _AGENT_IDS.get(name)
    if not agent_id:
        raise LookupError(f"no ClawOps agent named {name!r}; run `python -m ai.managed_agent sync`")
    return agent_id


def sync_agents(*, rest: ClawOpsREST | None, variants=VARIANTS, scenario_ids=None, dry_run: bool = False) -> dict[str, str]:
    """Create or update every agent. Returns {name: agentId}."""
    playbooks = [pb for pb in PLAYBOOKS if not scenario_ids or pb.id in scenario_ids]
    existing = {} if dry_run else {a.get("name"): a.get("agentId") for a in rest.list_agents()}
    result: dict[str, str] = {}
    for playbook in playbooks:
        for variant in variants:
            body = agent_payload(playbook, variant)
            name = body["name"]
            if dry_run:
                print(json.dumps(body, ensure_ascii=False, indent=2)[:1200])
                result[name] = "(dry-run)"
                continue
            if name in existing:
                rest.update_agent(existing[name], body)
                result[name] = existing[name]
                print(f"updated {name} -> {existing[name]}")
            else:
                created = rest.create_agent(body)
                result[name] = created.get("agentId", "")
                print(f"created {name} -> {result[name]}")
    _AGENT_IDS.clear()
    return result


_SAFETY_MARK = "[교육용 시뮬레이션 안전 규칙"


def agent_listing(agents: list[dict], variants=VARIANTS) -> list[str]:
    """One line per agent, flagging ones without the safety rules, then the
    spc- names sync would create that do not exist yet."""
    lines = []
    for a in agents:
        name = a.get("name") or ""
        mode = (a.get("configuration") or {}).get("outputMode", "?")
        flag = "" if _SAFETY_MARK in (a.get("instructions") or "") else "  [안전 규칙 없음]"
        lines.append(f"{a.get('agentId')}  {name}  [{mode}]{flag}")
    present = {a.get("name") for a in agents}
    missing = [agent_name(pb.id, v) for pb in PLAYBOOKS for v in variants if agent_name(pb.id, v) not in present]
    lines += [f"(없음) {name}  -> sync 필요" for name in missing]
    return lines


# ── CLI ────────────────────────────────────────────────────────────────────

_FINAL = {"completed", "failed", "busy", "no-answer", "canceled", "rejected"}


def _main(argv: list[str] | None = None) -> int:
    import ai.config  # noqa: F401 -- importing it loads backend/.env

    parser = argparse.ArgumentParser(description="ClawOps managed agents for training calls")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="create/update the agents")
    # --variant가 없으면 지금 통화에 쓰는 external_tts만 만듭니다(live는 쓸 때 직접 지정)
    p_sync.add_argument("--variant", action="append", choices=VARIANTS)
    p_sync.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    p_sync.add_argument("--dry-run", action="store_true")

    p_list = sub.add_parser("list", help="show every agent in the account with its id")
    p_list.add_argument("--variant", action="append", choices=VARIANTS)

    p_ctx = sub.add_parser("context", help="print the CallContext for a scenario")
    p_ctx.add_argument("--scenario", required=True)

    p_call = sub.add_parser("call", help="place a test call (no backend needed)")
    p_call.add_argument("--to", required=True)
    p_call.add_argument("--from", dest="from_", default=os.getenv("CLAWOPS_PHONE_NUMBER", ""))
    p_call.add_argument("--scenario", required=True)
    p_call.add_argument("--variant", choices=VARIANTS, default="external_tts")
    p_call.add_argument("--wait", action="store_true", help="poll until the call ends, then request its transcript")
    args = parser.parse_args(argv)

    if args.cmd == "context":
        ctx = build_call_context(get_scenario(args.scenario))
        print(ctx["instruction"])
        print(f"\n--- {len(ctx['instruction'])} chars, variables={ctx['variables']}")
        return 0

    if args.cmd == "sync":
        rest = None if args.dry_run else ClawOpsREST()
        mapping = sync_agents(rest=rest, variants=tuple(args.variant or ("external_tts",)),
                              scenario_ids=args.scenario, dry_run=args.dry_run)
        print(json.dumps(mapping, ensure_ascii=False, indent=2))
        return 0

    rest = ClawOpsREST()
    if args.cmd == "list":
        print("\n".join(agent_listing(rest.list_agents(), tuple(args.variant or VARIANTS))))
        return 0

    if not args.from_:
        parser.error("--from or CLAWOPS_PHONE_NUMBER is required")
    scenario = get_scenario(args.scenario)
    call = rest.create_call(
        to=args.to, from_=args.from_,
        agent_id=resolve_agent_id(scenario.id, args.variant, rest=rest),
        call_context=build_call_context(scenario),
    )
    call_id = call.get("callId")
    print(f"callId={call_id} status={call.get('status')} scenario={scenario.id} variant={args.variant}")
    if not args.wait:
        return 0
    deadline = time.monotonic() + 20 * 60
    status = call.get("status")
    while status not in _FINAL and time.monotonic() < deadline:
        time.sleep(4)
        status = rest.get_call(call_id).get("status")
        print(f"  status={status}")
    if status == "completed":
        rest.request_transcript(call_id)
        print(f"transcript requested. In a few minutes: python -m ai.transcript show {call_id} --scenario {scenario.id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
