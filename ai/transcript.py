"""Reading a ClawOps call transcript: who said what.

ClawOps transcripts label speakers ``speaker_0``, ``speaker_1``... since
2026-08 (earlier ones used AGENT / CUSTOMER), and the SDK states outright that
the mapping from speaker to role is not guaranteed. On a managed-agent call the
transcript is the only record of the conversation, so getting the roles wrong
would score the trainee on the caller's words.

We do know exactly what the caller was scripted to say: the scenario's opening
line is spoken verbatim, and the rest of its lines are close paraphrases. So
the speaker whose words match the script is the agent.

Backend use (docs: 백엔드_통합_안내서_v2.pdf, B4):

    agent = identify_agent_speaker(segments, scenario)
    if agent is None: ...                     # do not guess
    for seg in segments:
        role = "assistant" if seg.speaker == agent else "user"

CLI for the PoC:

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

# How much better the winner has to match than the runner-up. Below this the
# two speakers are too alike to call, and a wrong call is worse than none.
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
    """The speaker id that is the AI caller, or None when it cannot be told.

    1. Old format: an ``AGENT`` speaker is taken as is.
    2. Whoever said the opening line (verbatim by instruction) is the agent.
       The opening is split across segments sometimes, so a speaker's first
       three segments are joined before comparing.
    3. Otherwise, whoever shares the most wording with the scenario's lines.
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
        return None  # one voice only: nothing to tell apart by

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
            # The joined head can be much longer than the opening; compare
            # against its opening-length prefix as well.
            prefix = _compact(head)[: len(_compact(opening))]
            opening_scores[sp] = max(best_single, _similarity(prefix, opening))
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
    """[{"role": "assistant"|"user", "text": ...}] in transcript order."""
    return [
        {"role": "assistant" if segment_speaker(s) == agent_speaker else "user", "text": segment_text(s).strip()}
        for s in segments
        if segment_text(s).strip()
    ]


# ── CLI ────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    import ai.config  # noqa: F401 -- loads backend/.env
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
