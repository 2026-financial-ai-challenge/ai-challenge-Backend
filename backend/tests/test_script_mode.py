"""Script call mode: intent classification, routing, prerender cache, eval."""

import asyncio
import json

import pytest

from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()

from ai.scenarios.intents import ACK, INTENTS, classify  # noqa: E402
from ai.scenarios.script import ScriptReply, ScriptRouter  # noqa: E402


@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("네", ACK),
        ("네네 그런데요", ACK),
        ("저는 김민수입니다", ACK),
        ("아니요 그런 적 없어요", "deny"),
        ("누구세요?", "who_is_this"),
        ("대표번호로 다시 걸게요", "callback"),
        ("남편한테 물어볼게요", "consult_other"),
        ("직원번호가 어떻게 되세요?", "verify_request"),
        ("그건 개인정보라 못 알려드려요", "refuse_info"),
        ("얼마라고요?", "ask_detail"),
        ("경찰에 신고할게요", "police"),
        ("이거 보이스피싱이죠?", "scam_accusation"),
        ("장난하세요?", "angry"),
    ],
)
def test_classify_reads_the_common_trainee_moves(utterance, intent):
    assert classify(utterance).intent == intent


def test_ack_needs_a_whole_word():
    """"예전에…" and "어디세요" start with the syllables of "예" and "어"."""
    assert classify("예전에 비슷한 전화 받았어요").intent != ACK
    assert classify("어디세요").intent == "who_is_this"


def test_two_moves_in_one_utterance_are_ambiguous():
    match = classify("아니 그런 적 없는데 누구세요")
    assert match.ambiguous


def _router():
    return ScriptRouter(
        progression=("첫째입니다.", "둘째입니다."),
        replies=(ScriptReply("deny", ("부정 하나.", "부정 둘.")),),
        quick_replies=(("who_is_this", "누구냐고요? 저는 상담원입니다."),),
    )


def test_router_walks_the_progression_on_agreement_and_never_repeats():
    router = _router()
    assert router.route("네").line == "첫째입니다."
    assert router.route("예").line == "둘째입니다."
    assert router.route("네").reason == "exhausted"


def test_router_uses_each_variant_once_then_hands_over_to_the_llm():
    router = _router()
    assert router.route("아니요").line == "부정 하나."
    assert router.route("아닌데요").line == "부정 둘."
    third = router.route("아니요")
    assert not third.hit and third.reason == "exhausted"


def test_router_answers_reflex_intents_from_quick_replies():
    assert _router().route("누구세요").line == "누구냐고요? 저는 상담원입니다."


def test_router_leaves_long_ambiguous_and_unknown_turns_to_the_llm():
    router = _router()
    assert router.route("아니 제가 오늘 하루 종일 집에 있었는데 무슨 결제가 됐다는 건지 설명 좀 해 주세요").reason == "long"
    assert router.route("아니 그런 적 없는데 누구세요").reason == "ambiguous"
    assert router.route("흐음 글쎄").reason == "no_intent"


def test_peek_does_not_consume():
    router = _router()
    assert router.peek("네").line == "첫째입니다."
    assert router.route("네").line == "첫째입니다."


def test_every_library_scenario_scripts_the_core_intents():
    from ai.scenarios import SCENARIOS

    core = {"deny", "verify_request", "callback", "refuse_info", "consult_other", "ask_detail"}
    for scenario in SCENARIOS.values():
        assert len(scenario.progression) >= 4, scenario.id
        scripted = {reply.intent for reply in scenario.script}
        assert core <= scripted, (scenario.id, core - scripted)
        assert scripted <= set(INTENTS), scenario.id


def test_script_lines_are_short_enough_to_be_one_turn():
    from ai.scenarios import SCENARIOS

    for scenario in SCENARIOS.values():
        lines = [*scenario.progression, *(l for r in scenario.script for l in r.lines)]
        for line in lines:
            assert line.endswith((".", "?", "!")), (scenario.id, line)
            sentences = [s for s in line.replace("?", ".").replace("!", ".").split(".") if s.strip()]
            assert len(sentences) <= 3, (scenario.id, line)


# ── prerender cache ─────────────────────────────────────────────────────────


def test_tts_cache_round_trip_and_key_changes_with_settings(tmp_path):
    from ai.prerender import TTSCache, VoiceSpec

    cache = TTSCache(tmp_path)
    spec = VoiceSpec(voice_id="v", model="m")
    assert cache.get(spec, "안녕하세요.") is None
    cache.put(spec, "안녕하세요.", b"\x7f" * 800)
    assert TTSCache(tmp_path).get(spec, "안녕하세요.") == b"\x7f" * 800  # from disk
    assert cache.get(VoiceSpec(voice_id="v", model="other"), "안녕하세요.") is None


def test_ensure_lines_synthesizes_only_what_is_missing(tmp_path):
    from ai.prerender import TTSCache, VoiceSpec, ensure_lines

    class Response:
        status_code = 200
        content = b"\x55" * 160

    class Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, **kwargs):
            self.calls.append(kwargs["json"]["text"])
            assert kwargs["params"] == {"output_format": "ulaw_8000"}
            return Response()

    cache = TTSCache(tmp_path)
    spec = VoiceSpec(voice_id="v", model="eleven_flash_v2_5")
    cache.put(spec, "이미 있음.", b"\x01")
    client = Client()
    result = asyncio.run(
        ensure_lines(spec, ("이미 있음.", "새 줄."), cache=cache, api_key="k", client=client)
    )
    assert result == {"이미 있음.": True, "새 줄.": True}
    assert client.calls == ["새 줄."]


def test_v3_stability_snaps_to_a_preset():
    from ai.prerender import VoiceSpec

    assert VoiceSpec(voice_id="v", model="eleven_v3", stability=0.3).payload("x")["voice_settings"]["stability"] == 0.5
    assert "language_code" not in VoiceSpec(voice_id="v", model="eleven_v3").payload("x")
    assert VoiceSpec(voice_id="v", model="eleven_flash_v2_5").payload("x")["language_code"] == "ko"


# ── hit-rate evaluation ─────────────────────────────────────────────────────


def test_script_eval_reads_shadow_logs(tmp_path):
    from ai.script_eval import from_logs

    log = tmp_path / "app.log"
    rows = [
        {"mode": "shadow", "scenario": "a", "text": "네", "intent": "ack", "reason": "hit", "hit": True, "line": "x"},
        {"mode": "shadow", "scenario": "a", "text": "흠", "intent": None, "reason": "no_intent", "hit": False, "line": None},
    ]
    log.write_text(
        "\n".join(f"2026-09-24 INFO clawops.agent.pipeline SCRIPT {json.dumps(r, ensure_ascii=False)}" for r in rows),
        encoding="utf-8",
    )
    report = from_logs(log)
    assert report.turns == 2 and report.coverage() == 0.5
    assert "COVERAGE" in report.render()


def test_script_eval_replay_skips_hang_ups_and_counts_per_scenario():
    from ai.script_eval import replay

    report = replay({"s1": ["누구세요", "네", "이만 끊겠습니다"]}, "bank_security_hold")
    assert report.hangups == 1
    assert report.turns == 2
    assert report.coverage() == 1.0


def test_precision_from_labels(tmp_path):
    from ai.script_eval import precision

    labels = tmp_path / "labels.csv"
    labels.write_text("scenario,trainee,intent,caller_reply,ok\na,네,ack,x,y\na,응,ack,x,n\na,음,ack,x,\n", encoding="utf-8")
    assert precision(labels) == (1, 2)
