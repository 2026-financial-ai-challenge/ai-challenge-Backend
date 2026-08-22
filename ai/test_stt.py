"""Mic-only smoke test: prove Deepgram streams Korean text in real time.

Run from the repo root:

    python -m ai.test_stt

Speaks nothing. Prints interim (live) and final utterances plus timing.
Ctrl+C to stop.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai.audio_io import LocalMicSource
from ai.config import stt_sample_rate
from ai.stt_stream import stream_stt


def _print_live(label: str, text: str) -> None:
    sys.stdout.write(f"\r\033[K[{label}] {text}")
    sys.stdout.flush()


async def _async_main() -> int:
    source = LocalMicSource(sample_rate=stt_sample_rate())
    source.start()
    print("Deepgram 스트리밍 STT — 한국어로 말해 보세요.")
    print("중간 결과(partial)가 바로 뜨고, 잠시 멈추면 final 이 확정됩니다. Ctrl+C 종료.")
    print()

    first_partial_at: float | None = None
    pending: list[str] = []
    try:
        async for event in stream_stt(source):
            if event.utterance_end:
                text = " ".join(pending).strip()
                pending.clear()
                if text:
                    lag = (
                        f"{(time.perf_counter() - first_partial_at) * 1000:.0f} ms after first partial"
                        if first_partial_at is not None
                        else "n/a"
                    )
                    sys.stdout.write("\r\033[K")
                    print(f"[final] {text}  ({lag})")
                    first_partial_at = None
                continue

            if not event.text:
                continue

            if event.is_final:
                pending.append(event.text)
                if event.speech_final:
                    text = " ".join(pending).strip()
                    pending.clear()
                    lag = (
                        f"{(time.perf_counter() - first_partial_at) * 1000:.0f} ms after first partial"
                        if first_partial_at is not None
                        else "n/a"
                    )
                    sys.stdout.write("\r\033[K")
                    print(f"[final] {text}  ({lag})")
                    first_partial_at = None
                continue

            if first_partial_at is None:
                first_partial_at = time.perf_counter()
            preview = " ".join([*pending, event.text]).strip()
            _print_live("partial", preview)
        return 0
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        source.close()


def main() -> None:
    try:
        raise SystemExit(asyncio.run(_async_main()))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
