"""대본 모드가 답할 수 있는 훈련자 발화 비율(coverage)과 정확도(precision)를 잰다.

예전 대본 모드 코드. 현재 통화에서는 안 쓰고 backend/tests 때문에 남겨 둠.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ai.hangup import wants_hang_up
from ai.scenarios.script import RouteDecision, ScriptRouter

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
