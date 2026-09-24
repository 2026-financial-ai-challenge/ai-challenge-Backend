"""OpenAILLM that keeps one connection open for the whole call.

clawops' OpenAILLM builds a new AsyncOpenAI client inside every generate()
and closes it at the end of the turn. Each turn therefore pays a fresh TCP +
TLS handshake to the API before the request is even sent -- typically 100 to
300 ms that the trainee hears as silence, on every single answer.

This subclass holds one client per call (the pipeline session is built per
call through ClawOpsAgent's session_factory), and `warm()` opens the
connection while the phone is still ringing, so even the first answer skips
the handshake.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from clawops.agent.pipeline import OpenAILLM


log = logging.getLogger("clawops.agent.pipeline")


class PhoneOpenAILLM(OpenAILLM):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai

            # No SDK-level retries: a retried request after a slow failure is
            # dead air on the line. FallbackLLM switches provider instead.
            self._client = openai.AsyncOpenAI(api_key=self._api_key, max_retries=0, timeout=15.0)
        return self._client

    async def warm(self) -> None:
        """Open the HTTPS connection before the first real request."""
        try:
            await self._get_client().models.retrieve(self._model)
            log.info("OpenAI connection warmed (%s)", self._model)
        except Exception as exc:  # noqa: BLE001 -- warming is best effort
            log.info("OpenAI warm-up skipped: %s", exc)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools

        stream = await client.chat.completions.create(**kwargs)
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        has_tool_calls = False
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                yield delta.content
            if delta.tool_calls:
                has_tool_calls = True
                for tc in delta.tool_calls:
                    acc = tool_calls_acc.setdefault(
                        tc.index,
                        {"id": getattr(tc, "id", None) or "", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            acc["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            acc["function"]["arguments"] += tc.function.arguments
            if choice.finish_reason == "tool_calls" and has_tool_calls:
                yield json.dumps(
                    {
                        "type": "tool_calls",
                        "tool_calls": [tool_calls_acc[i] for i in sorted(tool_calls_acc)],
                    }
                )
