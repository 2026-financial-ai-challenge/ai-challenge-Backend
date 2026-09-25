"""Measure how many trainee turns the script mode can answer (the hit rate).

Two numbers decide whether the script mode is worth turning on:

  coverage   hits / routable turns
             "what share of turns get an instant, pre-synthesized answer"
  precision  appropriate hits / hits (human-labelled sample)
             "of those, how many were the right answer"

Coverage alone is easy to inflate -- widen a regex and it goes up while the
answers get worse -- so it is only meaningful next to precision. The target
used in docs/script-mode.md is coverage >= 70% at precision >= 90%.

Sources, from best to cheapest:

  --logs FILE    backend logs from real calls in CALL_SCRIPT_MODE=shadow.
                 Each trainee turn logged the router's decision (`SCRIPT {...}`),
                 so this is the router on real traffic with zero risk.
  --db URL       replay the stored transcripts (transcript_turns) through the
                 current router. Use after changing a pattern or a line.
  --jsonl FILE   replay any {"session_id", "role", "text"} rows.

Then label precision:

  python -m ai.script_eval --db "$DATABASE_URL" --sample 100 --out labels.csv
  (fill the `ok` column with y / n)
  python -m ai.script_eval --labels labels.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.hangup import wants_hang_up  # noqa: E402
from ai.scenarios.script import RouteDecision, ScriptRouter  # noqa: E402

_SCRIPT_LOG = re.compile(r"SCRIPT (\{.*\})\s*$")


@dataclass
class Report:
    decisions: list[dict] = field(default_factory=list)
    hangups: int = 0

    def add(self, decision: dict) -> None:
        self.decisions.append(decision)

    @property
    def turns(self) -> int:
        return len(self.decisions)

    @property
    def hits(self) -> list[dict]:
        return [d for d in self.decisions if d.get("hit")]

    def coverage(self) -> float:
        return len(self.hits) / self.turns if self.turns else 0.0

    def render(self, *, top: int = 15) -> str:
        reasons = Counter(d.get("reason") for d in self.decisions)
        intents = Counter(d.get("intent") or "(none)" for d in self.decisions)
        by_scenario: dict[str, list[dict]] = defaultdict(list)
        for d in self.decisions:
            by_scenario[d.get("scenario") or "-"].append(d)

        out = [
            f"trainee turns (routable) : {self.turns}",
            f"  + hang-up turns        : {self.hangups} (always answered by the fixed hang-up path)",
            f"script hits              : {len(self.hits)}",
            f"COVERAGE                 : {self.coverage():.1%}",
            "",
            "why turns went to the LLM:",
        ]
        for reason, count in reasons.most_common():
            if reason != "hit":
                out.append(f"  {reason:<10} {count:>5}  ({count / max(1, self.turns):.0%})")
        out += ["", "intent distribution:"]
        for intent, count in intents.most_common():
            out.append(f"  {intent:<16} {count:>5}")
        if len(by_scenario) > 1:
            out += ["", "coverage by scenario:"]
            for scenario, rows in sorted(by_scenario.items()):
                hits = sum(1 for r in rows if r.get("hit"))
                out.append(f"  {scenario:<24} {hits:>4}/{len(rows):<4} {hits / max(1, len(rows)):.0%}")
        unmatched = Counter(d["text"] for d in self.decisions if d.get("reason") == "no_intent")
        if unmatched:
            out += ["", "top unmatched utterances (add patterns to ai/scenarios/intents.py):"]
            for text, count in unmatched.most_common(top):
                out.append(f"  {count:>3} × {text}")
        missing = Counter(
            d.get("intent") for d in self.decisions if d.get("reason") in {"no_line", "exhausted"}
        )
        if missing:
            out += ["", "known intent but no line left (add lines to the scenario's script):"]
            for intent, count in missing.most_common():
                out.append(f"  {intent:<16} {count:>5}")
        return "\n".join(out)


# ── sources ────────────────────────────────────────────────────────────────


def from_logs(path: Path) -> Report:
    report = Report()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _SCRIPT_LOG.search(line)
        if not match:
            continue
        try:
            report.add(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return report


def replay(sessions: dict[str, list[str]], scenario_id: str | None) -> Report:
    from ai.scenarios import SCENARIOS, get_scenario

    targets = [scenario_id] if scenario_id else list(SCENARIOS)
    report = Report()
    for target in targets:
        scenario = get_scenario(target)
        for session_id, utterances in sessions.items():
            router = ScriptRouter.for_scenario(scenario)
            for text in utterances:
                if wants_hang_up(text):
                    report.hangups += 1
                    continue
                decision: RouteDecision = router.route(text)
                report.add({"scenario": target, "session": session_id, **decision.as_log()})
    return report


def sessions_from_jsonl(path: Path) -> dict[str, list[str]]:
    sessions: dict[str, list[str]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("role", "user") == "user" and str(row.get("text", "")).strip():
            sessions[str(row.get("session_id", "-"))].append(str(row["text"]).strip())
    return sessions


def sessions_from_db(url: str) -> dict[str, list[str]]:
    import psycopg

    url = url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    sessions: dict[str, list[str]] = defaultdict(list)
    with psycopg.connect(url) as conn:
        rows = conn.execute(
            "SELECT session_id, text FROM transcript_turns "
            "WHERE role = 'user' AND source = 'live' AND text <> '' "
            "ORDER BY session_id, sequence"
        ).fetchall()
    for session_id, text in rows:
        sessions[str(session_id)].append(str(text).strip())
    return sessions


# ── precision labelling ────────────────────────────────────────────────────


def write_sample(report: Report, out: Path, size: int, seed: int = 7) -> int:
    hits = report.hits
    random.Random(seed).shuffle(hits)
    sample = hits[:size]
    with out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["scenario", "trainee", "intent", "caller_reply", "ok"])
        for d in sample:
            writer.writerow([d.get("scenario", ""), d["text"], d.get("intent", ""), d.get("line", ""), ""])
    return len(sample)


def precision(labels: Path) -> tuple[int, int]:
    good = total = 0
    with labels.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            mark = (row.get("ok") or "").strip().lower()
            if mark in {"y", "yes", "1", "o", "ok"}:
                good += 1
                total += 1
            elif mark in {"n", "no", "0", "x"}:
                total += 1
    return good, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--logs", type=Path, help="backend log file from CALL_SCRIPT_MODE=shadow calls")
    source.add_argument("--db", help="DATABASE_URL to replay transcript_turns from")
    source.add_argument("--jsonl", type=Path, help="rows of {session_id, role, text}")
    parser.add_argument("--scenario", help="replay against one scenario id (default: every scenario)")
    parser.add_argument("--sample", type=int, default=0, help="write N random hits for precision labelling")
    parser.add_argument("--out", type=Path, default=Path("script_labels.csv"))
    parser.add_argument("--labels", type=Path, help="labelled CSV → precision")
    args = parser.parse_args(argv)

    if args.labels:
        good, total = precision(args.labels)
        rate = good / total if total else 0.0
        print(f"PRECISION {good}/{total} = {rate:.1%}")
        return 0

    if args.logs:
        report = from_logs(args.logs)
    elif args.db:
        report = replay(sessions_from_db(args.db), args.scenario)
    elif args.jsonl:
        report = replay(sessions_from_jsonl(args.jsonl), args.scenario)
    else:
        parser.error("give one of --logs, --db, --jsonl or --labels")
        return 2

    print(report.render())
    if args.sample:
        written = write_sample(report, args.out, args.sample)
        print(f"\nwrote {written} hits to {args.out} — fill the `ok` column (y/n), then --labels {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
