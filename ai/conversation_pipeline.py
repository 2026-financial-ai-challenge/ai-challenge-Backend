"""Low-latency conversation pipeline.

Flow (each stage is a generator so Twilio can replace local I/O later):

    AudioSource  →  streaming STT  →  streaming LLM  →  streaming TTS  →  AudioSink

Swap later:

    LocalMicSource     →  Twilio inbound media frames
    LocalSpeakerSink   →  Twilio websocket sender
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI

from ai.audio_io import AudioSink, AudioSource
from ai.llm_stream import ChatMessage, generate_llm_sentences
from ai.scenarios.types import Scenario
from ai.sentences import split_complete_text
from ai.stt_stream import iter_final_utterances
from ai.tts_stream import stream_tts

FirstByteCallback = Callable[[], None]


@dataclass
class LatencyMetrics:
    t0: float
    llm_first_token_ms: float | None = None
    first_sentence_ms: float | None = None
    tts_first_byte_ms: float | None = None

    @property
    def e2e_to_audio_ms(self) -> float | None:
        return self.tts_first_byte_ms


@dataclass
class TurnResult:
    assistant_text: str
    sentences: list[str] = field(default_factory=list)
    metrics: LatencyMetrics = field(default_factory=lambda: LatencyMetrics(t0=0.0))


def build_messages(
    scenario: Scenario,
    history: Sequence[ChatMessage],
) -> list[ChatMessage]:
    return [{"role": "system", "content": scenario.system_prompt}, *history]


async def iter_user_utterances(
    source: AudioSource,
    *,
    on_partial: Callable[[str], None] | None = None,
) -> AsyncIterator[str]:
    """Stage 1: streaming STT. `source` is an AudioSource (mic or Twilio)."""
    async for text in iter_final_utterances(source, on_partial=on_partial):
        yield text


async def iter_llm_sentences(
    scenario: Scenario,
    history: Sequence[ChatMessage],
    *,
    client: AsyncOpenAI | None = None,
    on_first_token: FirstByteCallback | None = None,
) -> AsyncIterator[str]:
    """Stage 2: streaming LLM. Replaceable independently of audio I/O."""
    async for sentence in generate_llm_sentences(
        build_messages(scenario, history),
        client=client,
        on_first_token=on_first_token,
    ):
        yield sentence


async def iter_tts_audio(
    sentence: str,
    *,
    scenario: Scenario | None = None,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[bytes]:
    """Stage 3: sentence-level streaming TTS. Bytes go to any AudioSink."""
    async for chunk in stream_tts(sentence, scenario=scenario, client=client):
        yield chunk


async def speak_sentences(
    sentences: Sequence[str],
    sink: AudioSink,
    *,
    scenario: Scenario | None = None,
    tts_client: httpx.AsyncClient | None = None,
    on_first_audio: FirstByteCallback | None = None,
) -> None:
    """TTS each sentence in order and write PCM to the sink as it arrives."""
    first_audio_seen = False
    for sentence in sentences:
        async for chunk in iter_tts_audio(sentence, scenario=scenario, client=tts_client):
            if not first_audio_seen:
                first_audio_seen = True
                if on_first_audio is not None:
                    on_first_audio()
            sink.write(chunk)


async def speak_text(
    text: str,
    sink: AudioSink,
    *,
    scenario: Scenario | None = None,
    tts_client: httpx.AsyncClient | None = None,
    on_first_audio: FirstByteCallback | None = None,
) -> list[str]:
    """Speak a fully known line (opening) using the same sentence-level TTS."""
    sentences = split_complete_text(text)
    await speak_sentences(
        sentences,
        sink,
        scenario=scenario,
        tts_client=tts_client,
        on_first_audio=on_first_audio,
    )
    return sentences


async def run_turn(
    user_text: str,
    history: list[ChatMessage],
    scenario: Scenario,
    sink: AudioSink,
    *,
    llm_client: AsyncOpenAI | None = None,
    tts_client: httpx.AsyncClient | None = None,
) -> TurnResult:
    """One user utterance → streamed LLM → streamed TTS → sink.

    `history` is mutated in place with the new user/assistant turns.
    """
    history.append({"role": "user", "content": user_text.strip()})
    metrics = LatencyMetrics(t0=time.perf_counter())
    sentences: list[str] = []

    def mark_first_token() -> None:
        if metrics.llm_first_token_ms is None:
            metrics.llm_first_token_ms = _elapsed_ms(metrics.t0)

    def mark_first_audio() -> None:
        if metrics.tts_first_byte_ms is None:
            metrics.tts_first_byte_ms = _elapsed_ms(metrics.t0)

    first_audio_seen = False
    async for sentence in iter_llm_sentences(
        scenario,
        history,
        client=llm_client,
        on_first_token=mark_first_token,
    ):
        if metrics.first_sentence_ms is None:
            metrics.first_sentence_ms = _elapsed_ms(metrics.t0)
        sentences.append(sentence)
        async for chunk in iter_tts_audio(sentence, scenario=scenario, client=tts_client):
            if not first_audio_seen:
                first_audio_seen = True
                mark_first_audio()
            sink.write(chunk)

    assistant_text = " ".join(sentences).strip()
    history.append({"role": "assistant", "content": assistant_text})
    return TurnResult(assistant_text=assistant_text, sentences=sentences, metrics=metrics)


def user_turn_count(history: Sequence[ChatMessage]) -> int:
    return sum(1 for message in history if message.get("role") == "user")


def reached_max_turns(history: Sequence[ChatMessage], scenario: Scenario) -> bool:
    return user_turn_count(history) >= scenario.max_turns


def _elapsed_ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0
