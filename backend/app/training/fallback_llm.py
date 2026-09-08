"""Cross-provider fallback for the in-call LLM.

The live pipeline already retries within one provider (see gemini_llm.py's
PhoneGeminiLLM cycling through GEMINI_FALLBACK_MODELS). This adds one more
rung above that: if the primary provider's whole retry chain still returns
nothing -- because its key is out of quota, suspended, or the service is
down -- the OTHER configured provider answers that turn instead of the
trainee hearing PhonePipelineSession's stall line.

Only safe before the first token: once streaming has started, switching
providers mid-turn would make the caller repeat part of the answer, so a
mid-stream failure keeps the partial reply exactly like PhoneGeminiLLM does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol


log = logging.getLogger("clawops.agent.pipeline")


class GenerateLLM(Protocol):
    """The shape both clawops LLM classes and PhoneGeminiLLM already have."""

    model: str

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]: ...


class FallbackLLM:
    """Try `primary`; if it fails before any token, try `secondary`."""

    def __init__(
        self,
        primary: GenerateLLM,
        secondary: GenerateLLM,
        *,
        primary_label: str,
        secondary_label: str,
    ) -> None:
        self._primary = primary
        self._secondary = secondary
        self._primary_label = primary_label
        self._secondary_label = secondary_label

    # PhonePipelineSession and its tests read these straight off self._llm.
    @property
    def model(self) -> str:
        return self._primary.model

    @property
    def provider(self) -> str:
        return getattr(self._primary, "provider", self._primary_label.lower())

    @property
    def _max_tokens(self) -> int:
        return getattr(self._primary, "_max_tokens", 0)

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        spoke = False
        try:
            async for token in self._primary.generate(messages, tools=tools):
                spoke = True
                yield token
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if spoke:
                log.error(
                    "%s failed mid-answer (%s); keeping the partial reply",
                    self._primary_label,
                    exc,
                )
                return
            log.warning(
                "%s failed before any token (%s); falling back to %s",
                self._primary_label,
                exc,
                self._secondary_label,
            )

        async for token in self._secondary.generate(messages, tools=tools):
            yield token
