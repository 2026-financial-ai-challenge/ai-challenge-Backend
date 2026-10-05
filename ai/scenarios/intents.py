"""What the trainee is doing with one utterance, without an LLM.

The script router (ai/scenarios/script.py) answers a trainee turn from a
pre-written line only when it knows what kind of turn it is. This module is
that knowledge: an ordered table of regular expressions over the handful of
moves a trainee actually makes on these calls -- agreeing, denying, asking who
is calling, wanting to call back, refusing to give information, and so on.

It is deliberately a classifier you can read. Every miss in a real call log is
fixed by adding one alternative to one pattern, and `python -m ai.script_eval`
prints the utterances that matched nothing so you know which ones to add.

Precision matters more than recall here. A turn that matches nothing goes to
the live LLM and costs a beat of latency; a turn that matches the wrong intent
gets a confident answer to a question nobody asked, which is exactly what makes
a caller sound like a machine. So:

- anything longer than a short answer is not routed (`MAX_ROUTABLE_CHARS`),
- an utterance that matches two different intents is `ambiguous` and not
  routed either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ai.scenarios.reflex import REFLEX_TRIGGERS

__all__ = [
    "ACK",
    "INTENT_PATTERNS",
    "INTENTS",
    "MAX_ROUTABLE_CHARS",
    "IntentMatch",
    "classify",
]

# Long answers carry more than one move ("아니 그런 적 없는데 누구시라고요?"),
# and a pre-written line can only answer one of them. Past this length the
# live LLM, which reads the whole thing, answers instead.
MAX_ROUTABLE_CHARS = 30

ACK = "ack"

# The reflex triggers are reused as-is so the two layers can never disagree on
# what "누구세요" means.
_REFLEX = dict(REFLEX_TRIGGERS)

# Ordered by priority: when two intents match, the earlier one is reported as
# the intent (and the turn is flagged ambiguous). Specific moves come before
# generic ones -- "경찰에 신고할게요" is a threat to report, not a question.
INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # The reflex table only fires on the word 피싱/사기 itself; open doubt
        # ("좀 이상한데", "못 믿겠어요") gets the same answer from a scammer.
        "scam_accusation",
        re.compile(
            _REFLEX["scam_accusation"].pattern
            + r"|이상한데|이상하네|이상해요|수상|의심(스러|이\s*가|되)|못\s*믿|믿을\s*수\s*없|사기\s*같"
        ),
    ),
    (
        "police",
        re.compile(
            r"경찰|112|일일이|신고\s*(할|하겠|해야|했|하러|할게|해요|합니다)|수사\s*의뢰"
        ),
    ),
    (
        # A lone "여보세요" mid-call is checking the line, not a greeting.
        "not_audible",
        re.compile(_REFLEX["not_audible"].pattern + r"|^\s*여보세요\s*[?.!]?\s*$"),
    ),
    ("repeat_that", _REFLEX["repeat_that"]),
    (
        "callback",
        re.compile(
            r"다시\s*(걸|전화|연락)|대표\s*번호|직접\s*(전화|확인|가|방문|알아|연락)|"
            r"(은행|카드사|회사|본사|지점|창구)에\s*(가|전화|확인|물어|연락)|"
            r"확인해\s*보고|알아보고|알아볼게|알아보겠"
        ),
    ),
    (
        "consult_other",
        re.compile(
            r"(남편|아내|와이프|엄마|아빠|어머니|아버지|아들|딸|가족|자식|친구|형|누나|언니|오빠|동생)"
            r"\s*(한테|에게|이랑|하고|와|과|랑)?\s*(물어|상의|얘기|이야기|확인|말해|말하)|"
            r"상의\s*(해|하고|를|좀)"
        ),
    ),
    (
        "verify_request",
        re.compile(
            r"직원\s*번호|사번|소속이|성함이?\s*(어떻게|뭐)|어떻게\s*믿|증명|증거|"
            r"진짜(예요|에요|인지|야|로)|공문|신분증|확인할\s*(수|방법)|이름\s*(말해|대\s*봐|뭐)"
        ),
    ),
    (
        "refuse_info",
        re.compile(
            r"(말|얘기)\s*(못|안)\s*(해|하|드|합)|(못|안)\s*(알려|가르쳐|말해)|"
            r"알려\s*(드릴\s*수|줄\s*수)\s*없|개인\s*정보|말하기\s*싫|"
            r"왜\s*(제|내)\s*(이름|정보|성함|재산|돈)|왜\s*알려|알려\s*줘야|말해야\s*(돼|되|하)|"
            r"왜\s*(궁금|필요|물어|묻)|싫(어|은데|습니다|다)|^\s*됐(어|습니다|거든|고)|"
            r"돈\s*(이|은)?\s*없|(못|안)\s*(보내|줘|드려|드립)"
        ),
    ),
    (
        "angry",
        re.compile(
            r"짜증|장난(해|치|하|전화)|미쳤|웃기(네|시|고)|어이(가)?\s*없|귀찮|"
            r"화(가|나)|열받|그만\s*좀|뭐\s*하는\s*(거|짓)"
        ),
    ),
    ("busy_now", _REFLEX["busy_now"]),
    ("who_is_this", _REFLEX["who_is_this"]),
    (
        "deny",
        re.compile(
            r"^\s*(아니(요|오|에요|예요|야)?|아뇨|아닌데(요)?)(?![가-힣])|"
            r"그런\s*(거|것)\s*(이\s*)?없|[가-힣]\s*적\s*(이|은)?\s*없|안\s*했|"
            r"모르(는|겠)|처음\s*듣|저\s*아닌"
        ),
    ),
    (
        "ask_detail",
        re.compile(
            r"얼마|언제|어디(서|에서|로)|무슨\s*(결제|건|일|대출|사건|계좌|돈)|"
            r"뭐(가|를|예요|에요|죠|라고|요)|어떤|어떻게\s*(하|된|해)|왜(요)?\s*\??$"
        ),
    ),
    (
        ACK,
        # "네"/"예" only as whole words: "예전에…" and "어디세요" start with
        # the same syllable and are not agreement.
        re.compile(
            r"^\s*(?:(?:네|예|응|음|어)+(?![가-힣])|그래(?:요)?|그렇(?:죠|네요|습니다)|"
            r"맞(?:아요|습니다|아)|알겠(?:어요|습니다|어)|그럴게(?:요)?|좋아요|괜찮아요|"
            r"그런데(?:요)?(?![가-힣])|말씀하세요|제\s*이름은|"
            r"저는\s*[가-힣]{2,4}(?:이에요|예요|입니다))|"
            # surprise that keeps listening: "아 진짜요?", "정말요?"
            r"^\s*(?:아\s*)?(?:진짜|정말)(?:요)?\s*[?!.]*\s*$"
        ),
    ),
)

INTENTS: tuple[str, ...] = tuple(name for name, _pattern in INTENT_PATTERNS)


@dataclass(frozen=True)
class IntentMatch:
    """How one utterance was read.

    `intent` is the highest-priority match (or None). `matched` holds every
    intent that matched, which is how ambiguity is decided: an agreement plus
    a question ("네 근데 얼마라고요?") is two moves, not one.
    """

    text: str
    intent: str | None
    matched: tuple[str, ...]

    @property
    def ambiguous(self) -> bool:
        # A leading "네"/"아니" in front of a real move is how people start a
        # sentence, not a second intent -- only count the non-ack ones.
        substantive = [name for name in self.matched if name != ACK]
        return len(set(substantive)) > 1

    @property
    def routable_length(self) -> bool:
        return len(_compact(self.text)) <= MAX_ROUTABLE_CHARS


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def classify(text: str) -> IntentMatch:
    cleaned = (text or "").strip()
    if not cleaned:
        return IntentMatch(text=cleaned, intent=None, matched=())
    matched = tuple(name for name, pattern in INTENT_PATTERNS if pattern.search(cleaned))
    substantive = [name for name in matched if name != ACK]
    if substantive:
        intent = substantive[0]
    elif matched:
        intent = ACK
    else:
        intent = None
    return IntentMatch(text=cleaned, intent=intent, matched=matched)
