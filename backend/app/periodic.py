import logging
import os
from collections.abc import Callable
from threading import Event, Lock, Thread


logger = logging.getLogger(__name__)


def env_int(name: str, default: int, *, minimum: int = 0) -> int:
    return max(minimum, int(os.getenv(name, str(default))))


class PeriodicWorker:
    """job을 interval()초마다 백그라운드 스레드에서 실행한다. 예외는 기록하고 계속 돈다."""

    def __init__(self, name: str, job: Callable[[], object], interval: Callable[[], float]):
        self._name = name
        self._job = job
        self._interval = interval
        self._lock = Lock()
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._job()
            except Exception:
                logger.exception("%s failed", self._name)
            self._stop.wait(self._interval())
