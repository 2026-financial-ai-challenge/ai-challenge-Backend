"""GeminiLLM that survives the endpoint's frequent 503s.

clawops' PipelineSession._respond wraps the whole turn in
`except Exception: log.error(...)`, so one transient failure from Gemini is
not an error the trainee hears -- it is dead air, and the caller simply never
answers. Measured against this project's key, gemini-3.5-flash-lite returned
503 UNAVAILABLE or timed out on every attempt, and even a healthy model
fails a noticeable fraction of calls.

Retrying is only safe *before* the first token reaches the caller. Once a
token has been yielded it is already on its way to TTS, so a retry would make
the caller repeat half a sentence; at that point we stop and let the partial
answer stand.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from clawops.agent.pipeline import GeminiLLM


log = logging.getLogger("clawops.agent.pipeline")


class PhoneGeminiLLM(GeminiLLM):
    """Gemini with bounded retries and model fallback for one live turn."""

    def __init__(
        self,
        *,
        fallback_models: tuple[str, ...] = (),
        attempts: int = 2,
        retry_delay: float = 0.4,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        # Only models that differ from the primary are worth falling back to.
        self._fallback_models = tuple(
            m for m in fallback_models if m and m != self._model
        )
        self._attempts = max(1, int(attempts))
        self._retry_delay = max(0.0, float(retry_delay))

    def _base_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[str]:
        """One attempt against `model`, always on the base implementation.

        Going through GeminiLLM.generate explicitly matters: dispatching on
        self.generate would re-enter this subclass and recurse forever, and a
        fallback model is run on a throwaway sibling so self._model is never
        mutated mid-call.
        """
        target = self
        if model != self._model:
            target = GeminiLLM(
                api_key=self._api_key,
                model=model,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            )
        return GeminiLLM.generate(target, messages, tools=tools)

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        last_error: Exception | None = None

        for model in (self._model, *self._fallback_models):
            for attempt in range(1, self._attempts + 1):
                spoke = False
                try:
                    async for token in self._base_stream(model, messages, tools):
                        spoke = True
                        yield token
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                    if spoke:
                        # Already partly said out loud; retrying would repeat it.
                        log.error(
                            "Gemini failed mid-answer on %s (%s); keeping the "
                            "partial reply",
                            model,
                            exc,
                        )
                        return
                    log.warning(
                        "Gemini %s attempt %s/%s failed before any token: %s",
                        model,
                        attempt,
                        self._attempts,
                        exc,
                    )
                    if self._retry_delay:
                        await asyncio.sleep(self._retry_delay)

        log.error("Gemini exhausted every model and retry: %s", last_error)
        if last_error is not None:
            raise last_error
