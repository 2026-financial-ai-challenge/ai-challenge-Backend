"""고정 대사를 ElevenLabs로 미리 합성해 디스크에 캐시한다.

서버가 직접 음성을 처리하던 대본 모드에서 쓰던 코드다. 지금 통화(ClawOps 매니지드 에이전트)에는
쓰이지 않고, backend/tests/test_script_mode.py가 검사하고 있어 남겨 두었다.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TTSCache", "VoiceSpec", "default_cache", "ensure_lines", "synthesize_ulaw"]

log = logging.getLogger("ai.prerender")

_API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
# language_code는 v2.5 모델만 받는다.
_LANGUAGE_CODE_MODELS = ("eleven_turbo_v2_5", "eleven_flash_v2_5")


@dataclass(frozen=True)
class VoiceSpec:
    voice_id: str
    model: str
    stability: float = 0.3
    similarity_boost: float = 0.7
    style: float = 0.15
    speed: float = 0.94

    def key(self, text: str) -> str:
        raw = "|".join(
            (
                self.voice_id,
                self.model,
                f"{self.stability:.3f}",
                f"{self.similarity_boost:.3f}",
                f"{self.style:.3f}",
                f"{self.speed:.3f}",
                text.strip(),
            )
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def payload(self, text: str) -> dict:
        stability = self.stability
        if self.model.startswith("eleven_v3"):
            # v3는 0, 0.5, 1 세 값만 받는다.
            stability = min((0.0, 0.5, 1.0), key=lambda preset: abs(preset - stability))
        body: dict = {
            "text": text.strip(),
            "model_id": self.model,
            "voice_settings": {
                "stability": stability,
                "similarity_boost": self.similarity_boost,
                "style": self.style,
                "use_speaker_boost": True,
                "speed": self.speed,
            },
        }
        if self.model in _LANGUAGE_CODE_MODELS:
            body["language_code"] = "ko"
        return body


class TTSCache:
    """메모리 캐시 + `<key>.ulaw` 파일 디렉터리."""

    def __init__(self, cache_dir: str | os.PathLike | None = None, *, memory_items: int = 256) -> None:
        root = cache_dir or os.getenv("TTS_CACHE_DIR") or os.path.join(
            tempfile.gettempdir(), "safety-call-tts"
        )
        self.root = Path(root)
        self._memory: OrderedDict[str, bytes] = OrderedDict()
        self._memory_items = max(1, memory_items)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.ulaw"

    def get(self, spec: VoiceSpec, text: str) -> bytes | None:
        key = spec.key(text)
        audio = self._memory.get(key)
        if audio is not None:
            self._memory.move_to_end(key)
            return audio
        path = self._path(key)
        try:
            audio = path.read_bytes()
        except OSError:
            return None
        if not audio:
            return None
        self._remember(key, audio)
        return audio

    def has(self, spec: VoiceSpec, text: str) -> bool:
        return self.get(spec, text) is not None

    def put(self, spec: VoiceSpec, text: str, audio: bytes) -> None:
        if not audio:
            return
        key = spec.key(text)
        self._remember(key, audio)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_bytes(audio)
            tmp.replace(self._path(key))
        except OSError as exc:
            # 디스크에 못 써도 메모리 캐시는 남는다.
            log.warning("TTS cache write failed (%s): %s", self.root, exc)

    def _remember(self, key: str, audio: bytes) -> None:
        self._memory[key] = audio
        self._memory.move_to_end(key)
        while len(self._memory) > self._memory_items:
            self._memory.popitem(last=False)


_DEFAULT_CACHE: TTSCache | None = None


def default_cache() -> TTSCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = TTSCache()
    return _DEFAULT_CACHE


async def synthesize_ulaw(spec: VoiceSpec, text: str, *, api_key: str, client) -> bytes:
    """한 줄을 mu-law 8kHz로 합성한다."""
    response = await client.post(
        _API.format(voice_id=spec.voice_id),
        params={"output_format": "ulaw_8000"},
        headers={"xi-api-key": api_key, "accept": "audio/basic"},
        json=spec.payload(text),
    )
    if response.status_code != 200:
        detail = response.text[:200] if hasattr(response, "text") else ""
        raise RuntimeError(f"ElevenLabs {response.status_code}: {detail}")
    return response.content


async def ensure_lines(
    spec: VoiceSpec,
    lines: tuple[str, ...],
    *,
    cache: TTSCache | None = None,
    api_key: str | None = None,
    concurrency: int = 4,
    client=None,
) -> dict[str, bool]:
    """빠진 줄만 합성해 캐시한다. {줄: 캐시 여부}를 돌려준다. 한 줄이 실패해도 나머지는 계속한다."""
    cache = cache or default_cache()
    key = api_key if api_key is not None else os.getenv("ELEVENLABS_API_KEY", "").strip()
    result = {line: cache.has(spec, line) for line in lines}
    missing = [line for line, ok in result.items() if not ok]
    if not missing:
        return result
    if not key:
        log.warning("ELEVENLABS_API_KEY is not set; %d lines stay on live TTS", len(missing))
        return result

    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(line: str) -> None:
        async with gate:
            try:
                audio = await synthesize_ulaw(spec, line, api_key=key, client=client)
            except Exception as exc:  # noqa: BLE001
                log.warning("Prerender failed for %r: %s", line[:30], exc)
                return
            cache.put(spec, line, audio)
            result[line] = True

    try:
        await asyncio.gather(*(one(line) for line in missing))
    finally:
        if owns_client:
            await client.aclose()
    return result
