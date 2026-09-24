"""Synthesize a scenario's fixed lines before the call, and keep them on disk.

Every line the caller can say verbatim -- the opening, the hang-up line, the
quick replies, and the script mode's progression and intent answers -- is
known before the phone rings. Synthesizing it then instead of mid-call buys
two things at once:

- latency: the line plays the instant it is chosen, with no TTS round trip.
- quality: nothing is waiting on it, so it can use a slower, more expressive
  ElevenLabs model and the style/speed settings the live streaming client
  cannot pass (see call_service.build_pipeline_session).

Audio is requested as `ulaw_8000`, which is exactly what the phone leg carries
(G.711 mu-law, 8 kHz, mono), so playback is a byte copy -- no resampling.

Files are keyed by a hash of (voice, model, settings, text), so changing any
of them simply misses the cache and synthesizes again. Warm the cache ahead of
a deployment with

    python -m ai.prerender                 # every scenario, script lines included
    python -m ai.prerender --scenario investigation_unit --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TTSCache",
    "VoiceSpec",
    "default_cache",
    "ensure_lines",
    "scenario_lines",
    "synthesize_ulaw",
    "voice_spec_for",
]

log = logging.getLogger("ai.prerender")

ULAW_BYTES_PER_SECOND = 8000
_API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
# language_code is only accepted by the v2.5 models; others reject the field.
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
            # v3 only accepts its three presets (creative / natural / robust).
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
    """Memory in front of a directory of `<key>.ulaw` files."""

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
            # A read-only or full disk only costs the cross-process cache;
            # this process still has the line in memory.
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


def prerender_model() -> str:
    """Model for pre-synthesized lines.

    Defaults to the live model so fixed and live lines sound like the same
    person. Set ELEVENLABS_PRERENDER_MODEL_ID (eleven_multilingual_v2,
    eleven_v3) for more expressive fixed lines -- and listen to a call first:
    the more the two models differ, the more the switch between a fixed line
    and a live one is audible.
    """
    live = os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip() or "eleven_flash_v2_5"
    return os.getenv("ELEVENLABS_PRERENDER_MODEL_ID", "").strip() or live


def voice_spec_for(scenario, voice_id: str, *, model: str | None = None) -> VoiceSpec:
    return VoiceSpec(
        voice_id=voice_id,
        model=model or prerender_model(),
        stability=float(os.getenv("ELEVENLABS_STABILITY", "") or getattr(scenario, "tts_stability", 0.3)),
        similarity_boost=float(getattr(scenario, "tts_similarity_boost", 0.7)),
        style=float(os.getenv("ELEVENLABS_STYLE", "") or getattr(scenario, "tts_style", 0.15)),
        speed=float(os.getenv("ELEVENLABS_SPEED", "") or getattr(scenario, "tts_speed", 0.94)),
    )


def scenario_lines(scenario, *, include_script: bool = True, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Every line this scenario may speak verbatim, deduplicated, in order."""
    lines: list[str] = [scenario.opening_line, getattr(scenario, "hangup_line", "")]
    lines += [reply for _trigger, reply in getattr(scenario, "quick_replies", ()) or ()]
    if include_script:
        lines += list(getattr(scenario, "progression", ()) or ())
        for reply in getattr(scenario, "script", ()) or ():
            lines += list(reply.lines)
    lines += list(extra)
    seen: list[str] = []
    for line in lines:
        cleaned = (line or "").strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


async def synthesize_ulaw(spec: VoiceSpec, text: str, *, api_key: str, client) -> bytes:
    """One REST call → raw mu-law 8 kHz bytes."""
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
    """Make sure every line is cached. Returns {line: cached_now_or_before}.

    Never raises for one bad line: a line that fails to synthesize is simply
    spoken through the live TTS path when its turn comes.
    """
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
            except Exception as exc:  # noqa: BLE001 -- one line must not sink the rest
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


# ── CLI ────────────────────────────────────────────────────────────────────


def _main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    import ai.config  # noqa: F401 -- importing it loads backend/.env
    from ai.scenarios import SCENARIOS

    parser = argparse.ArgumentParser(description="Pre-synthesize scenario lines into the TTS cache.")
    parser.add_argument("--scenario", action="append", help="scenario id (repeatable; default: all)")
    parser.add_argument("--voice", help="override voice id (default: ELEVENLABS_VOICE_ID or the scenario's)")
    parser.add_argument("--model", help="override model (default: ELEVENLABS_PRERENDER_MODEL_ID)")
    parser.add_argument("--no-script", action="store_true", help="only opening/hang-up/quick replies")
    parser.add_argument("--dry-run", action="store_true", help="list what would be synthesized")
    args = parser.parse_args(argv)

    ids = args.scenario or list(SCENARIOS)
    exit_code = 0
    for scenario_id in ids:
        scenario = SCENARIOS.get(scenario_id)
        if scenario is None:
            print(f"unknown scenario: {scenario_id}", file=sys.stderr)
            exit_code = 2
            continue
        voice = args.voice or os.getenv("ELEVENLABS_VOICE_ID", "").strip() or scenario.tts_voice_id
        spec = voice_spec_for(scenario, voice, model=args.model)
        lines = scenario_lines(scenario, include_script=not args.no_script)
        cache = default_cache()
        missing = [line for line in lines if not cache.has(spec, line)]
        print(f"[{scenario_id}] voice={voice} model={spec.model} lines={len(lines)} missing={len(missing)}")
        if args.dry_run:
            for line in missing:
                print(f"   - {line}")
            continue
        result = asyncio.run(ensure_lines(spec, lines, cache=cache))
        failed = [line for line, ok in result.items() if not ok]
        print(f"   cached {len(lines) - len(failed)}/{len(lines)} → {cache.root}")
        if failed:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(_main())
