"""Standalone smoke test for report_service.py's _grounded_flag / _normalize_evidence.

Does NOT import the real module (it needs sqlalchemy/fastapi/app.* which aren't
installed in this sandbox) -- instead it extracts the actual function source
straight out of backend/app/services/report_service.py via `ast` and execs it,
so the test runs against the real, current code rather than a re-typed copy.
"""

import ast
import sys
from pathlib import Path

SRC_PATH = Path("backend/app/services/report_service.py")
source = SRC_PATH.read_text(encoding="utf-8")
tree = ast.parse(source)

wanted = {"_grounded_flag", "_normalize_evidence"}
found = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in wanted:
        found[node.name] = ast.get_source_segment(source, node)

missing = wanted - found.keys()
if missing:
    print(f"[FAIL] could not locate function source for: {missing}")
    sys.exit(1)

namespace = {"re": __import__("re")}
for name, src in found.items():
    exec(compile(src, filename=f"<{name}>", mode="exec"), namespace)

_grounded_flag = namespace["_grounded_flag"]

cases = [
    (True, "성함이 김철수입니다", "네 성함이 김철수입니다 맞아요", True,
     "evidence literally appears in trainee text -> stays True"),
    (True, "성함이 김철수입니다", "저는 잘 모르겠는데요 그런 전화 처음 받아요", False,
     "evidence NOT in trainee text (hallucinated quote) -> downgraded to False"),
    (False, "성함이 김철수입니다", "네 성함이 김철수입니다 맞아요", False,
     "flag already False -> stays False regardless of evidence"),
    (True, "", "아무 말이나 하는 훈련자 발화", False,
     "flag True but evidence blank -> downgraded to False (no proof offered)"),
    (True, "  성함이   김철수 입니다!!", "음... 성함이 김철수입니다 확인했어요", True,
     "punctuation/spacing differences don't break normalized substring match"),
    (True, "Yes I am", "yes, i am the account holder", True,
     "case-insensitive match across English text"),
    (True, "010-1234-5678 불러줬어요", "저는 번호를 불러준 적이 없습니다", False,
     "superficially related but NOT an actual substring -> downgraded to False"),
]

failures = 0
for flag, evidence, trainee_text, expected, desc in cases:
    actual = _grounded_flag(flag, evidence, trainee_text)
    status = "OK" if actual is expected else "FAIL"
    if status == "FAIL":
        failures += 1
    print(f"[{status}] expected={expected!r} actual={actual!r} :: {desc}")

print()
if failures:
    print(f"{failures}/{len(cases)} FAILED")
    sys.exit(1)
print(f"ALL {len(cases)} CASES PASSED")
