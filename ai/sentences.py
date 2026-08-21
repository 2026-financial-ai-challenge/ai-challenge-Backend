"""Cut a streamed Korean reply into speakable sentences as soon as possible."""

from __future__ import annotations

from ai.safety import sanitize_spoken_text

_HARD_ENDINGS = set(".!?。！？\n")
_SOFT_BREAKS = set(",，、; ")
_SOFT_FLUSH_LEN = 64
_HARD_FLUSH_LEN = 96


def split_complete_text(text: str) -> list[str]:
    """Split an already-complete string into speakable sentences."""
    sentences: list[str] = []
    buffer = ""
    for char in text:
        flushed, buffer = feed_sentence_buffer(buffer, char)
        sentences.extend(flushed)
    sentences.extend(flush_sentence_buffer(buffer))
    return [s for s in sentences if s]


def feed_sentence_buffer(buffer: str, piece: str) -> tuple[list[str], str]:
    """Consume a streamed token. Return (complete sentences, leftover buffer)."""
    buffer += piece
    sentences: list[str] = []

    while True:
        end_index = _find_hard_end(buffer)
        if end_index is not None:
            sentence = sanitize_spoken_text(buffer[: end_index + 1].strip())
            buffer = buffer[end_index + 1 :]
            if sentence:
                sentences.append(sentence)
            continue

        if len(buffer) >= _HARD_FLUSH_LEN:
            cut = _find_last_break(buffer, _SOFT_BREAKS) or _SOFT_FLUSH_LEN
            sentence = sanitize_spoken_text(buffer[:cut].strip())
            buffer = buffer[cut:]
            if sentence:
                sentences.append(sentence)
            continue

        if len(buffer) >= _SOFT_FLUSH_LEN:
            cut = _find_last_break(buffer, _SOFT_BREAKS)
            if cut is not None and cut >= 24:
                sentence = sanitize_spoken_text(buffer[:cut].strip())
                buffer = buffer[cut:]
                if sentence:
                    sentences.append(sentence)
                    continue
        break

    return sentences, buffer


def flush_sentence_buffer(buffer: str) -> list[str]:
    leftover = sanitize_spoken_text(buffer.strip())
    return [leftover] if leftover else []


def _find_hard_end(buffer: str) -> int | None:
    for i, char in enumerate(buffer):
        if char not in _HARD_ENDINGS:
            continue
        if char == "." and _is_decimal_dot(buffer, i):
            continue
        return i
    return None


def _is_decimal_dot(buffer: str, index: int) -> bool:
    prev_digit = index > 0 and buffer[index - 1].isdigit()
    next_digit = index + 1 < len(buffer) and buffer[index + 1].isdigit()
    return prev_digit and next_digit


def _find_last_break(buffer: str, breaks: set[str]) -> int | None:
    for i in range(len(buffer) - 1, 7, -1):
        if buffer[i] in breaks:
            return i + 1
    return None
