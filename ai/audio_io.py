"""Audio I/O adapters.

LocalSpeakerSink / LocalMicSource are the local test devices. Replace them
later with Twilio Media Streams websocket send/recv using the same PCM frames.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Protocol

import numpy as np

try:
    import sounddevice as sd
except OSError as exc:  # PortAudio missing
    sd = None
    _SD_IMPORT_ERROR = exc
else:
    _SD_IMPORT_ERROR = None


class AudioSink(Protocol):
    """Outbound audio. LocalSpeakerSink now; Twilio websocket later."""

    sample_rate: int

    def write(self, pcm_bytes: bytes) -> None:
        """Enqueue PCM bytes. Must not block the TTS stream for long."""

    def wait_until_idle(self, timeout: float = 30.0) -> None:
        """Block until queued audio has been consumed."""

    def close(self) -> None:
        """Release the device / connection."""


class AudioSource(Protocol):
    """Inbound PCM frames. LocalMicSource now; Twilio websocket later."""

    sample_rate: int

    def mute(self) -> None:
        """Stop forwarding capture (TTS playback / echo avoidance)."""

    def unmute(self) -> None:
        """Resume forwarding capture."""

    def close(self) -> None:
        """Release the device / connection."""

    def frames(self) -> Iterator[bytes] | AsyncIterator[bytes]:
        ...


class NullAudioSink:
    """Keep TTS running without a speaker — useful for latency measurement."""

    def __init__(self, sample_rate: int = 24000) -> None:
        self.sample_rate = sample_rate
        self.bytes_received = 0

    def write(self, pcm_bytes: bytes) -> None:
        self.bytes_received += len(pcm_bytes)

    def wait_until_idle(self, timeout: float = 30.0) -> None:
        return None

    def close(self) -> None:
        return None


class LocalSpeakerSink:
    """Stream int16 mono PCM to the default output device."""

    def __init__(self, sample_rate: int = 24000) -> None:
        if sd is None:
            raise RuntimeError(
                "sounddevice/PortAudio is unavailable. Install PortAudio "
                "or run with --no-play."
            ) from _SD_IMPORT_ERROR

        self.sample_rate = sample_rate
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self._buffer = bytearray()
        self._rest = b""
        self._queued_bytes = 0
        self._played_bytes = 0
        self._lock = threading.Lock()
        self._closed = False
        self._stream = sd.OutputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            callback=self._callback,
            blocksize=int(sample_rate * 0.04),  # 40 ms — low latency, stable
        )
        self._stream.start()

    def write(self, pcm_bytes: bytes) -> None:
        if self._closed or not pcm_bytes:
            return
        data = self._rest + pcm_bytes
        if len(data) % 2:
            self._rest = data[-1:]
            data = data[:-1]
        else:
            self._rest = b""
        if not data:
            return
        with self._lock:
            self._queued_bytes += len(data)
        self._queue.put(data)

    def wait_until_idle(self, timeout: float = 30.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                queued = self._queued_bytes
                played = self._played_bytes
            if queued <= played and self._queue.empty() and not self._buffer:
                return
            time.sleep(0.02)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:
            pass

    def _callback(self, outdata, frames, _time_info, _status) -> None:  # type: ignore[no-untyped-def]
        need = frames * 2  # int16 mono
        while len(self._buffer) < need:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item:
                self._buffer.extend(item)

        if len(self._buffer) >= need:
            chunk = bytes(self._buffer[:need])
            del self._buffer[:need]
            outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)
            with self._lock:
                self._played_bytes += need
            return

        if self._buffer:
            available = (len(self._buffer) // 2) * 2
            padded = bytes(self._buffer[:available]).ljust(need, b"\x00")
            del self._buffer[:available]
            outdata[:] = np.frombuffer(padded, dtype=np.int16).reshape(-1, 1)
            with self._lock:
                self._played_bytes += available
            return

        outdata.fill(0)


class LocalMicSource:
    """Capture int16 mono PCM from the default input device.

    `mute()` drops frames so TTS playback is not fed back into STT.
    Swap this class for a Twilio Media Streams reader later; STT only
    consumes `frames()`.
    """

    def __init__(self, sample_rate: int = 16000, block_ms: int = 20) -> None:
        if sd is None:
            raise RuntimeError(
                "sounddevice/PortAudio is unavailable. Cannot open the microphone."
            ) from _SD_IMPORT_ERROR

        self.sample_rate = sample_rate
        self._block_size = max(1, int(sample_rate * block_ms / 1000))
        self._queue: asyncio.Queue[bytes | None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._muted = False
        self._closed = False
        self._stream: sd.InputStream | None = None

    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if self._stream is not None:
            return
        self._loop = loop or asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=32)
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._block_size,
            callback=self._callback,
        )
        self._stream.start()

    def mute(self) -> None:
        self._muted = True

    def unmute(self) -> None:
        self._muted = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self._put(None)

    async def frames(self) -> AsyncIterator[bytes]:
        if self._queue is None:
            self.start()
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                break
            yield item

    def _callback(self, indata, _frames, _time_info, _status) -> None:  # type: ignore[no-untyped-def]
        if self._closed or self._muted:
            return
        self._put(indata.copy().tobytes())

    def _put(self, item: bytes | None) -> None:
        loop = self._loop
        queue_ = self._queue
        if loop is None or queue_ is None:
            return
        try:
            loop.call_soon_threadsafe(self._enqueue, queue_, item)
        except RuntimeError:
            pass

    @staticmethod
    def _enqueue(queue_: asyncio.Queue[bytes | None], item: bytes | None) -> None:
        if queue_.full():
            try:
                queue_.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue_.put_nowait(item)
        except asyncio.QueueFull:
            pass

