"""Measure pipeline latency.

Typed mode (default):

    python -m ai.test_latency --scenario institution_impersonation

Mic mode (streaming STT → LLM → TTS):

    python -m ai.test_stt
    python -m ai.test_latency --mic --scenario institution_impersonation

Target after the user finishes speaking: first TTS audio byte < 1000 ms.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import httpx
from openai import AsyncOpenAI

from ai.audio_io import AudioSink, LocalMicSource, LocalSpeakerSink, NullAudioSink
from ai.classifier import classify_behaviors
from ai.config import elevenlabs_sample_rate, openai_api_key, stt_sample_rate
from ai.conversation_pipeline import (
    ChatMessage,
    LatencyMetrics,
    iter_user_utterances,
    reached_max_turns,
    run_turn,
    speak_text,
)
from ai.scenarios import SCENARIOS, get_scenario

_ECHO_TAIL_SECONDS = 0.25


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safety Phishing Call — latency test (text or mic STT → LLM → TTS)"
    )
    parser.add_argument(
        "--scenario",
        default="institution_impersonation",
        choices=sorted(SCENARIOS),
        help="Scenario id to run",
    )
    parser.add_argument(
        "--mic",
        action="store_true",
        help="Use the local microphone + Deepgram streaming STT",
    )
    parser.add_argument(
        "--no-play",
        action="store_true",
        help="Run TTS but do not open the local speaker",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Run the post-call classifier when the session ends",
    )
    return parser.parse_args()


def _print_metrics(label: str, metrics: LatencyMetrics) -> None:
    def fmt(value: float | None) -> str:
        return f"{value:7.0f} ms" if value is not None else "     n/a"

    e2e = metrics.e2e_to_audio_ms
    flag = ""
    if e2e is not None:
        flag = "  OK" if e2e < 1000 else "  OVER 1s"
    print(f"  [{label}]")
    print(f"    LLM first token : {fmt(metrics.llm_first_token_ms)}")
    print(f"    First sentence  : {fmt(metrics.first_sentence_ms)}")
    print(f"    TTS first byte  : {fmt(metrics.tts_first_byte_ms)}")
    print(f"    E2E to audio    : {fmt(e2e)}{flag}")


async def _readline(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def _make_sink(no_play: bool) -> AudioSink:
    if no_play:
        return NullAudioSink(sample_rate=elevenlabs_sample_rate())
    return LocalSpeakerSink(sample_rate=elevenlabs_sample_rate())


def _print_partial(text: str) -> None:
    sys.stdout.write(f"\r\033[K당신> {text}")
    sys.stdout.flush()


async def _async_main(args: argparse.Namespace) -> int:
    scenario = get_scenario(args.scenario)
    sink = _make_sink(args.no_play)
    history: list[ChatMessage] = [
        {"role": "assistant", "content": scenario.opening_line},
    ]

    print()
    print("Safety Phishing Call — 교육용 시뮬레이션")
    print(f"시나리오: {scenario.name} ({scenario.id})")
    print(f"최대 턴: {scenario.max_turns}")
    if args.mic:
        print("입력: 로컬 마이크 (Deepgram 스트리밍 STT). 종료는 Ctrl+C")
        print("목표: 발화 종료 → 첫 TTS 오디오 바이트 < 1000ms")
    else:
        print("입력: 키보드. 종료는 /quit , 분류는 /classify , 마이크는 --mic")
        print("목표: Enter → 첫 TTS 오디오 바이트 < 1000ms")
    print()

    llm_client = AsyncOpenAI(api_key=openai_api_key())
    mic: LocalMicSource | None = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as tts_client:
            opening_metrics = LatencyMetrics(t0=time.perf_counter())

            def mark_opening_audio() -> None:
                if opening_metrics.tts_first_byte_ms is None:
                    opening_metrics.tts_first_byte_ms = (
                        time.perf_counter() - opening_metrics.t0
                    ) * 1000.0

            if args.mic:
                mic = LocalMicSource(sample_rate=stt_sample_rate())
                mic.start()
                mic.mute()

            print(f"[상대] {scenario.opening_line}")
            await speak_text(
                scenario.opening_line,
                sink,
                scenario=scenario,
                tts_client=tts_client,
                on_first_audio=mark_opening_audio,
            )
            sink.wait_until_idle()
            _print_metrics("opening TTS", opening_metrics)
            print()

            if args.mic:
                assert mic is not None
                await asyncio.sleep(_ECHO_TAIL_SECONDS)
                await _run_mic_loop(
                    scenario=scenario,
                    history=history,
                    sink=sink,
                    mic=mic,
                    llm_client=llm_client,
                    tts_client=tts_client,
                )
            else:
                await _run_text_loop(
                    scenario=scenario,
                    history=history,
                    sink=sink,
                    llm_client=llm_client,
                    tts_client=tts_client,
                )

        if args.classify:
            await _print_classification(history, llm_client)
        return 0
    except KeyboardInterrupt:
        print()
        return 0
    finally:
        if mic is not None:
            mic.close()
        sink.close()
        await llm_client.close()


async def _run_text_loop(
    *,
    scenario,
    history: list[ChatMessage],
    sink: AudioSink,
    llm_client: AsyncOpenAI,
    tts_client: httpx.AsyncClient,
) -> None:
    while True:
        if reached_max_turns(history, scenario):
            print(f"최대 턴({scenario.max_turns})에 도달해 통화를 종료합니다.")
            return
        try:
            user_text = (await _readline("당신> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not user_text:
            continue
        if user_text in {"/quit", "/exit", "/q"}:
            return
        if user_text == "/classify":
            await _print_classification(history, llm_client)
            continue
        result = await run_turn(
            user_text,
            history,
            scenario,
            sink,
            llm_client=llm_client,
            tts_client=tts_client,
        )
        sink.wait_until_idle()
        print(f"[상대] {result.assistant_text}")
        _print_metrics("turn", result.metrics)
        print()


async def _run_mic_loop(
    *,
    scenario,
    history: list[ChatMessage],
    sink: AudioSink,
    mic: LocalMicSource,
    llm_client: AsyncOpenAI,
    tts_client: httpx.AsyncClient,
) -> None:
    utterances: asyncio.Queue[str] = asyncio.Queue()
    listening = True

    def on_partial(text: str) -> None:
        if listening:
            _print_partial(text)

    async def stt_worker() -> None:
        async for text in iter_user_utterances(mic, on_partial=on_partial):
            await utterances.put(text)

    worker = asyncio.create_task(stt_worker())
    mic.unmute()
    try:
        while True:
            if reached_max_turns(history, scenario):
                print(f"최대 턴({scenario.max_turns})에 도달해 통화를 종료합니다.")
                return
            user_text = await _next_utterance(utterances, worker)
            if not listening:
                continue
            text = user_text.strip()
            if not text:
                continue
            listening = False
            mic.mute()
            sys.stdout.write("\r\033[K")
            print(f"당신> {text}")
            result = await run_turn(
                text,
                history,
                scenario,
                sink,
                llm_client=llm_client,
                tts_client=tts_client,
            )
            sink.wait_until_idle()
            print(f"[상대] {result.assistant_text}")
            _print_metrics("turn (after STT final)", result.metrics)
            print()
            await asyncio.sleep(_ECHO_TAIL_SECONDS)
            _drain_queue(utterances)
            if reached_max_turns(history, scenario):
                print(f"최대 턴({scenario.max_turns})에 도달해 통화를 종료합니다.")
                return
            listening = True
            mic.unmute()
    finally:
        mic.mute()
        worker.cancel()
        try:
            await worker
        except (asyncio.CancelledError, Exception):
            pass


async def _next_utterance(
    utterances: asyncio.Queue[str],
    worker: asyncio.Task[None],
) -> str:
    getter = asyncio.create_task(utterances.get())
    done, pending = await asyncio.wait(
        {getter, worker},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if worker in done:
        getter.cancel()
        try:
            await getter
        except asyncio.CancelledError:
            pass
        await worker
        raise RuntimeError("STT stream ended unexpectedly.")
    return getter.result()


def _drain_queue(utterances: asyncio.Queue[str]) -> None:
    while True:
        try:
            utterances.get_nowait()
        except asyncio.QueueEmpty:
            return


async def _print_classification(
    history: list[ChatMessage],
    llm_client: AsyncOpenAI,
) -> None:
    print("\n--- 통화 후 행동 분류 ---")
    payload = await classify_behaviors(history, client=llm_client)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print()


def main() -> None:
    args = _parse_args()
    try:
        raise SystemExit(asyncio.run(_async_main(args)))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
