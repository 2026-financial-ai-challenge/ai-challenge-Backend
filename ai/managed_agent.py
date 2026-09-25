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
    "delivery_payment_error": "15628352-2ede-4f1b-89e6-ceda0c983fbc",  # Jiwoo - Service Specialist
    "family_emergency": "f7755efb-1848-4321-aa22-5e5be5d32486",        # Ryeowook - Easygoing Pal
    "investigation_unit": "89f4372f-1f73-4b85-8e1e-5d24ed8bc826",      # Jaewon - Steady Advisor
}
# GPT-Live also accepts the Realtime voices; these five have known characters.
LIVE_VOICES: dict[str, str] = {
    "bank_security_hold": "cedar",
    "low_interest_loan": "coral",
    "delivery_payment_error": "sage",
    "family_emergency": "ballad",
    "investigation_unit": "ash",
}
CARTESIA_SPEED: dict[str, float] = {"family_emergency": 1.08, "investigation_unit": 0.95}

_PHONE_RULES = """
[전화 규칙]
- 전화가 연결되면 통화별 지시의 [첫 마디]를 토씨 하나 바꾸지 않고 그대로 말한다. 그 뒤로 다시 인사하지 않는다.
- 한 번에 문장 둘까지만 말하고 상대에게 차례를 넘긴다.
- 상대가 "네", "음", "아" 처럼 짧게 맞장구만 치면 멈추지 말고 하던 말을 이어 간다.
- 상대가 말을 끊고 들어오면 바로 멈추고, 방금 들은 말에 답한다.
- 상대가 끊겠다고 하면 첫 번째는 붙잡고 사건으로 되돌린다. 두 번째에는 통화별 지시의 [마지막 한마디]만 말하고 통화 종료 도구를 호출한다.
- 통화별 지시의 [통화 길이]를 넘기면 [마지막 한마디]를 말하고 통화 종료 도구를 호출한다.
- 다른 번호로 전화를 돌리지 않는다. 키패드 입력을 요구하거나 키패드 신호를 보내지 않는다.
- 상대가 카드번호, 계좌번호, 비밀번호, 인증번호 같은 실제 숫자를 불러 주려 하면 "번호는 말씀하지 마십시오. 그건 저희가 받지 않습니다." 라고 막고 이야기를 이어 간다.

[예외 — 이 경우에만 역할을 멈춘다. 위의 모든 규칙보다 우선한다]
- 상대가 숨이 안 쉬어진다, 가슴이 아프다, 쓰러질 것 같다, 죽고 싶다처럼 실제 위급한 상태를 말하면 즉시 역할을 멈추고 이렇게 말한 뒤 통화 종료 도구를 호출한다:
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


def build_call_context(scenario) -> dict[str, Any]:
    """CallContext for one call: {"instruction": str, "variables": dict}.

    The shape matches clawops' ``calls.create(call_context=...)``.
    """
    playbook = _playbook_for(scenario)
    head = f"[첫 마디]\n{playbook.opening_line}"
    tail = (
        f"[마지막 한마디]\n{playbook.hangup_line}\n\n"
        f"[통화 길이]\n상대 발화 기준 최대 {playbook.max_turns}번이다."
    )
    parts = [head, build_scenario_block(playbook), _reply_examples(playbook), tail]
    instruction = "\n\n".join(p for p in parts if p)
    if len(instruction) > CALL_CONTEXT_LIMIT:
        # The examples are the only optional part; drop them before the rules.
        instruction = "\n\n".join([head, build_scenario_block(playbook), tail])
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
                "model": os.getenv("MANAGED_AGENT_REALTIME_MODEL", "gpt-realtime-2.1-mini"),
            },
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


# ── CLI ────────────────────────────────────────────────────────────────────

_FINAL = {"completed", "failed", "busy", "no-answer", "canceled", "rejected"}


def _main(argv: list[str] | None = None) -> int:
    import ai.config  # noqa: F401 -- importing it loads backend/.env

    parser = argparse.ArgumentParser(description="ClawOps managed agents for training calls")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sync = sub.add_parser("sync", help="create/update the agents")
    p_sync.add_argument("--variant", action="append", choices=VARIANTS)
    p_sync.add_argument("--scenario", action="append", choices=sorted(SCENARIOS))
    p_sync.add_argument("--dry-run", action="store_true")

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
        mapping = sync_agents(rest=rest, variants=tuple(args.variant or VARIANTS),
                              scenario_ids=args.scenario, dry_run=args.dry_run)
        print(json.dumps(mapping, ensure_ascii=False, indent=2))
        return 0

    rest = ClawOpsREST()
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
