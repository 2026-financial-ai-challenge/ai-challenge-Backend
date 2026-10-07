"""ClawOps 녹취록에서 누가 AI이고 누가 훈련자인지 가려낸다.

녹취록 화자는 speaker_0, speaker_1처럼만 붙고 역할은 보장되지 않는다. 역할을 잘못 붙이면 AI의 말로
훈련자를 채점하게 되므로, 시나리오 대사와 가장 비슷하게 말한 쪽을 AI로 본다.
첫 마디는 그대로 말하므로 가장 강한 단서다.

    python -m ai.transcript show <callId> --scenario bank_security_hold
"""

from __future__ import annotations

import argparse
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

__all__ = ["identify_agent_speaker", "label_roles", "segment_speaker", "segment_text"]

# 차이가 이보다 작으면 판정하지 않는다. 틀린 판정이 판정 없음보다 나쁘다.
_MIN_OPENING_MATCH = 0.45
_MIN_MARGIN = 0.12


def segment_speaker(segment: Any) -> str:
    return str(segment.get("speaker") if isinstance(segment, dict) else getattr(segment, "speaker", "")) or ""


def segment_text(segment: Any) -> str:
    return str(segment.get("text") if isinstance(segment, dict) else getattr(segment, "text", "")) or ""


def _compact(text: str) -> str:
    return re.sub(r"[\s.,?!~…·'\"“”‘’]", "", text or "")


def _similarity(a: str, b: str) -> float:
    a, b = _compact(a), _compact(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _best_window(text: str, target: str) -> float:
    """text 안에서 target 길이의 구간 중 target과 가장 비슷한 곳의 유사도."""
    t, o = _compact(text), _compact(target)
    n = len(o)
    if not t or not o:
        return 0.0
    if len(t) <= n:
        return _similarity(t, o)
    return max(SequenceMatcher(None, t[i : i + n], o, autojunk=False).ratio() for i in range(len(t) - n + 1))


def _bigrams(text: str) -> set[str]:
    t = _compact(text)
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _reference_lines(scenario) -> tuple[str, list[str]]:
    opening = getattr(scenario, "opening_line", "") or ""
    lines = [getattr(scenario, "hangup_line", "") or ""]
    lines += [reply for _t, reply in getattr(scenario, "quick_replies", ()) or ()]
    lines += list(getattr(scenario, "progression", ()) or ())
    for reply in getattr(scenario, "script", ()) or ():
        lines += list(reply.lines)
    return opening, [line for line in lines if line]


def identify_agent_speaker(segments: Iterable[Any], scenario) -> str | None:
    """AI 화자 id. 가려낼 수 없으면 None.

    1. 예전 형식의 AGENT 화자는 그대로 쓴다.
    2. 첫 마디를 말한 쪽이 AI다. 첫 마디가 여러 조각으로 나뉠 수 있어 처음 세 조각을 합쳐 본다.
    3. 그래도 모르면 시나리오 대사와 겹치는 말이 가장 많은 쪽.
    """
    segs = [s for s in segments if segment_text(s).strip()]
    speakers: list[str] = []
    for s in segs:
        sp = segment_speaker(s)
        if sp and sp not in speakers:
            speakers.append(sp)
    if not speakers:
        return None
    upper = {sp.upper(): sp for sp in speakers}
    if "AGENT" in upper:
        return upper["AGENT"]
    if len(speakers) == 1:
        return None

    opening, lines = _reference_lines(scenario)
    by_speaker: dict[str, list[str]] = {sp: [] for sp in speakers}
    for s in segs:
        by_speaker[segment_speaker(s)].append(segment_text(s))

    def ranked(scores: dict[str, float]) -> tuple[str, float, float]:
        order = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return order[0][0], order[0][1], order[1][1] if len(order) > 1 else 0.0

    if opening:
        opening_scores = {}
        for sp, texts in by_speaker.items():
            head = " ".join(texts[:3])
            best_single = max((_similarity(t, opening) for t in texts[:3]), default=0.0)
            # 첫 마디 앞에 군말이 붙을 수 있어 앞부분 안에서 구간을 밀어 가며 본다.
            opening_scores[sp] = max(best_single, _best_window(head, opening))
        winner, top, second = ranked(opening_scores)
        if top >= _MIN_OPENING_MATCH and top - second >= _MIN_MARGIN:
            return winner

    reference = set().union(*(_bigrams(line) for line in lines)) if lines else set()
    if not reference:
        return None
    overlap = {}
    for sp, texts in by_speaker.items():
        grams = _bigrams(" ".join(texts))
        overlap[sp] = len(grams & reference) / len(grams) if grams else 0.0
    winner, top, second = ranked(overlap)
    if top - second >= _MIN_MARGIN:
        return winner
    return None


def label_roles(segments: Iterable[Any], agent_speaker: str) -> list[dict[str, str]]:
    """녹취 순서대로 [{"role": "assistant"|"user", "text": ...}]."""
    return [
        {"role": "assistant" if segment_speaker(s) == agent_speaker else "user", "text": segment_text(s).strip()}
        for s in segments
        if segment_text(s).strip()
    ]


def _main(argv: list[str] | None = None) -> int:
    import ai.config  # noqa: F401  backend/.env 로딩
    from ai.harness import audit_transcript
    from ai.managed_agent import ClawOpsREST
    from ai.scenarios import get_scenario

    parser = argparse.ArgumentParser(description="Show a ClawOps call transcript with roles")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_show = sub.add_parser("show")
    p_show.add_argument("call_id")
    p_show.add_argument("--scenario", required=True)
    p_show.add_argument("--summary", action="store_true", help="also print the ClawOps summary")
    args = parser.parse_args(argv)

    rest = ClawOpsREST()
    data = rest.get_transcript(args.call_id)
    status = data.get("status")
    if status == "not_requested":
        rest.request_transcript(args.call_id)
        print("transcript requested; run this again in a few minutes")
        return 0
    if status != "completed":
        print(f"transcript status: {status} {data.get('stage') or ''} {data.get('error') or ''}")
        return 0

    scenario = get_scenario(args.scenario)
    segments = data.get("segments") or []
    agent = identify_agent_speaker(segments, scenario)
    print(f"segments={len(segments)} agent_speaker={agent}")
    if agent is None:
        for s in segments:
            print(f"  [{segment_speaker(s)}] {segment_text(s)}")
        print("could not tell the caller apart; roles not assigned")
        return 1
    turns = label_roles(segments, agent)
    for turn in turns:
        who = "상담원" if turn["role"] == "assistant" else "훈련자"
        print(f"  {who}: {turn['text']}")
    findings = audit_transcript([t["text"] for t in turns if t["role"] == "assistant"])
    print(f"\nsafety audit: {len(findings)} finding(s)")
    for f in findings:
        print(f"  - {f['kind']}: {f['text'][:80]}")
    if args.summary:
        print("\nsummary:", rest.get_summary(args.call_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
