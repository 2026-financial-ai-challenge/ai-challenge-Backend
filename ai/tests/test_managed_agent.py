import json

import pytest

from ai import managed_agent as ma
from ai.safety import REAL_ORGS, UNSAFE_TOKEN
from ai.scenarios import SCENARIOS, get_scenario
from ai.scenarios.library import PLAYBOOKS


def test_base_instructions_carry_safety_style_and_phone_rules():
    text = ma.base_instructions()
    assert "[교육용 시뮬레이션 안전 규칙" in text
    assert "[말하는 방식]" in text
    assert "[전화 규칙]" in text
    # the emergency exit is the only place the exercise may be named, and it
    # has to say it outranks the no-disclosure rule
    assert "모든 규칙보다 우선한다" in text


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_call_context_fits_and_carries_the_scenario(scenario_id):
    scenario = get_scenario(scenario_id)
    ctx = ma.build_call_context(scenario)
    instruction = ctx["instruction"]
    assert len(instruction) <= ma.CALL_CONTEXT_LIMIT
    assert instruction.startswith("[첫 마디]\n" + scenario.opening_line)
    assert scenario.hangup_line in instruction
    assert f"최대 {scenario.max_turns}번" in instruction
    assert "[사건" in instruction and "[받아치기]" in instruction
    assert ctx["variables"]["scenario_id"] == scenario_id
    assert not REAL_ORGS.search(instruction)
    assert not UNSAFE_TOKEN.search(instruction)


def test_call_context_for_an_unknown_id_uses_the_fallback_playbook():
    ctx = ma.build_call_context(get_scenario("some_future_type"))
    default = get_scenario("bank_security_hold")
    assert ctx["instruction"].startswith("[첫 마디]\n" + default.opening_line)
    assert ctx["variables"]["scenario_id"] == "some_future_type"


@pytest.mark.parametrize("variant", ma.VARIANTS)
def test_agent_payload_per_playbook(variant):
    names = set()
    for playbook in PLAYBOOKS:
        body = ma.agent_payload(playbook, variant)
        names.add(body["name"])
        assert body["name"] == f"spc-{playbook.id}-{variant}"
        assert len(body["name"]) <= 80
        assert body["greeting"] is True
        assert len(body["instructions"]) <= 16000
        cfg = body["configuration"]
        assert cfg["outputMode"] == variant
        if variant == "external_tts":
            assert cfg["tts"]["provider"] == "cartesia"
            assert cfg["tts"]["voice"] == ma.CARTESIA_VOICES[playbook.id]
            assert 0.6 <= cfg["tts"]["speed"] <= 1.5
        else:
            assert cfg["llm"]["provider"] == "openai-live"
            assert cfg["llm"]["voice"] == ma.LIVE_VOICES[playbook.id]
    assert len(names) == len(PLAYBOOKS)


def test_every_scenario_has_a_voice_in_both_variants():
    ids = {pb.id for pb in PLAYBOOKS}
    assert ids == set(ma.CARTESIA_VOICES) == set(ma.LIVE_VOICES)


def test_pick_variant(monkeypatch):
    monkeypatch.setenv("CALL_AGENT_VARIANT", "live")
    assert ma.pick_variant() == "live"
    monkeypatch.setenv("CALL_AGENT_VARIANT", "nonsense")
    assert ma.pick_variant() == "external_tts"
    monkeypatch.setenv("CALL_AGENT_VARIANT", "ab")
    assert {ma.pick_variant() for _ in range(40)} == set(ma.VARIANTS)


class FakeREST:
    def __init__(self, agents=()):
        self.agents = [dict(a) for a in agents]
        self.list_calls = 0
        self.created, self.updated = [], []

    def list_agents(self):
        self.list_calls += 1
        return self.agents

    def create_agent(self, body):
        self.created.append(body)
        agent = {"agentId": f"ag{len(self.agents)}", "name": body["name"]}
        self.agents.append(agent)
        return agent

    def update_agent(self, agent_id, body):
        self.updated.append((agent_id, body))
        return {"agentId": agent_id, "name": body["name"]}


def test_resolve_agent_id_caches_and_explains_a_missing_agent():
    ma._AGENT_IDS.clear()
    rest = FakeREST([{"agentId": "a1", "name": "spc-bank_security_hold-live"}])
    assert ma.resolve_agent_id("bank_security_hold", "live", rest=rest) == "a1"
    assert ma.resolve_agent_id("bank_security_hold", "live", rest=rest) == "a1"
    assert rest.list_calls == 1
    # an unknown scenario id resolves through the fallback playbook
    assert ma.resolve_agent_id("voice_phishing_training", "live", rest=rest) == "a1"
    with pytest.raises(LookupError, match="sync"):
        ma.resolve_agent_id("investigation_unit", "external_tts", rest=rest)


def test_sync_creates_missing_and_updates_existing_agents():
    rest = FakeREST([{"agentId": "old", "name": "spc-investigation_unit-live"}])
    result = ma.sync_agents(rest=rest)
    assert len(result) == len(PLAYBOOKS) * len(ma.VARIANTS)
    assert [agent_id for agent_id, _ in rest.updated] == ["old"]
    assert len(rest.created) == len(result) - 1


def test_create_call_sends_the_pascal_case_body():
    sent = {}

    class Response:
        status_code = 201
        content = b"{}"

        def json(self):
            return {"callId": "CA1", "status": "queued"}

    class Http:
        def request(self, method, url, headers, **kwargs):
            sent.update(method=method, url=url, headers=headers, body=kwargs.get("json"))
            return Response()

    rest = ma.ClawOpsREST(api_key="k", account_id="AC1", client=Http())
    ctx = ma.build_call_context(get_scenario("family_emergency"))
    assert rest.create_call(to="01000000000", from_="07000000000", agent_id="ag", call_context=ctx)["callId"] == "CA1"
    assert sent["url"].endswith("/v1/accounts/AC1/calls")
    assert sent["headers"]["Authorization"] == "Bearer k"
    body = sent["body"]
    assert body["AgentId"] == "ag" and body["To"] == "01000000000" and body["From"] == "07000000000"
    assert body["CallContext"]["Instruction"] == ctx["instruction"]
    assert json.dumps(body["CallContext"]["Variables"])
