"""Streaming LLM with sentence-level yields.

The full reply is never buffered on purpose: as soon as a spoken sentence
is complete, it is yielded so TTS can start immediately.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence

from openai import AsyncOpenAI

from ai.config import (
    GEMINI_OPENAI_BASE_URL,
    call_llm_model,
    call_llm_provider,
    gemini_api_key,
    openai_api_key,
)
from ai.sentences import feed_sentence_buffer, flush_sentence_buffer, split_complete_text

ChatMessage = dict[str, str]

__all__ = [
    "ChatMessage",
    "build_call_llm_client",
    "feed_sentence_buffer",
    "flush_sentence_buffer",
    "generate_llm_sentences",
    "split_complete_text",
]


def build_call_llm_client() -> AsyncOpenAI:
    """Client for the in-call LLM, honouring CALL_LLM_PROVIDER.

    Gemini is reached through its OpenAI-compatible endpoint, so the same
    AsyncOpenAI client works for both providers.
    """
    if call_llm_provider() == "gemini":
        return AsyncOpenAI(api_key=gemini_api_key(), base_url=GEMINI_OPENAI_BASE_URL)
    return AsyncOpenAI(api_key=openai_api_key())


async def generate_llm_sentences(
    messages: Sequence[ChatMessage],
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
    # Raised from 0.65 -- at the old value the model tended to reuse the same
    # phrasing turn after turn, which reads as scripted/robotic. This still
    # keeps coherent short phone-register replies while adding variety.
    temperature: float = 0.75,
    max_tokens: int = 180,
    on_first_token: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Yield spoken sentences as soon as the streamed reply can be cut.

    `on_first_token` is an optional zero-arg callable fired on the first delta.
    """
    openai_client = client or build_call_llm_client()
    stream = await openai_client.chat.completions.create(
        model=model or call_llm_model(),
        messages=list(messages),
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
    )

    buffer = ""
    first_token_seen = False
    async for chunk in stream:
        delta = chunk.choices[0].delta.content or ""
        if not delta:
            continue
        if not first_token_seen:
            first_token_seen = True
            if on_first_token is not None:
                on_first_token()
        sentences, buffer = feed_sentence_buffer(buffer, delta)
        for sentence in sentences:
            yield sentence

    for sentence in flush_sentence_buffer(buffer):
        yield sentence
