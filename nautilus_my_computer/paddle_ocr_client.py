"""Client for the optional, crash-isolated PaddleOCR runtime."""

from __future__ import annotations

import atexit
import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_REQUEST_TIMEOUT_SECONDS = 60.0
_START_TIMEOUT_SECONDS = 45.0
_IDLE_TIMEOUT_SECONDS = 120.0


def _data_home() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    return Path(configured) if configured else Path.home() / ".local" / "share"


def _runtime_base() -> Path:
    override = os.environ.get("MC_PADDLEOCR_HOME")
    return Path(override) if override else _data_home() / "nautilus-my-computer" / "paddleocr"


def active_runtime() -> tuple[Path, Path] | None:
    """Return (runtime root, its Python) only for a complete active runtime."""
    base = _runtime_base()
    try:
        with (base / "active-runtime.json").open(encoding="utf-8") as handle:
            active = json.load(handle)
        runtime_id = active["runtime_id"]
        if not isinstance(runtime_id, str) or runtime_id in {"", ".", ".."}:
            return None
        runtime = (base / "runtimes" / runtime_id).resolve()
        if runtime.parent != (base / "runtimes").resolve():
            return None
        with (runtime / "runtime.json").open(encoding="utf-8") as handle:
            metadata = json.load(handle)
        if metadata.get("schema_version") != 2:
            return None
        python = runtime / str(metadata["python"])
        models = metadata["models"]
        required = (
            python,
            runtime / str(models["detection_dir"]),
            runtime / str(models["recognition_dir"]),
            runtime / str(models["layout_dir"]),
        )
        if not python.is_file() or not all(path.exists() for path in required[1:]):
            return None
        return runtime, python
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def available() -> bool:
    return active_runtime() is not None


class PaddleOcrClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._idle_timer: threading.Timer | None = None

    @staticmethod
    def _cancelled(cancellable: Any) -> bool:
        return bool(cancellable is not None and cancellable.is_cancelled())

    def _read_message(self, timeout: float, cancellable: Any = None) -> dict[str, Any] | None:
        process = self._process
        if process is None or process.stdout is None:
            return None
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if self._cancelled(cancellable):
                    self._stop_locked()
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_locked()
                    return None
                if not selector.select(min(0.1, remaining)):
                    continue
                line = process.stdout.readline()
                if not line:
                    return None
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
        finally:
            selector.close()
        return None

    def _start_locked(self) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        self._stop_locked()
        resolved = active_runtime()
        if resolved is None:
            return False
        runtime, python = resolved
        helper = Path(__file__).with_name("paddle_ocr_helper.py")
        if not helper.is_file():
            return False
        try:
            self._process = subprocess.Popen(
                [str(python), str(helper), "--serve", "--runtime-root", str(runtime)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                start_new_session=True,
            )
        except OSError:
            self._process = None
            return False
        ready = self._read_message(_START_TIMEOUT_SECONDS)
        if ready is None or ready.get("event") != "ready":
            self._stop_locked()
            return False
        return True

    def _stop_locked(self) -> None:
        timer, self._idle_timer = self._idle_timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        process, self._process = self._process, None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _idle_stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def close(self) -> None:
        """Stop the private helper promptly when Nautilus/the client exits."""
        with self._lock:
            self._stop_locked()

    def _arm_idle_stop_locked(self) -> None:
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timer = threading.Timer(_IDLE_TIMEOUT_SECONDS, self._idle_stop)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def recognize(
        self, path: str, cancellable: Any = None, *, layout: bool | str = "auto"
    ) -> dict[str, Any] | None:
        with self._lock:
            if self._cancelled(cancellable) or not self._start_locked():
                return None
            process = self._process
            if process is None or process.stdin is None:
                return None
            request_id = self._next_id
            self._next_id += 1
            try:
                process.stdin.write(
                    json.dumps(
                        {"id": request_id, "path": path, "layout": layout},
                        separators=(",", ":"),
                    )
                    + "\n"
                )
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._stop_locked()
                return None
            message = self._read_message(_REQUEST_TIMEOUT_SECONDS, cancellable)
            if message is None or message.get("id") != request_id or "error" in message:
                self._stop_locked()
                return None
            result = message.get("result")
            if not isinstance(result, dict):
                self._stop_locked()
                return None
            self._arm_idle_stop_locked()
            return result


_CLIENT = PaddleOcrClient()
atexit.register(_CLIENT.close)


def recognize(
    path: str, cancellable: Any = None, *, layout: bool | str = "auto"
) -> dict[str, Any] | None:
    return _CLIENT.recognize(path, cancellable, layout=layout)
