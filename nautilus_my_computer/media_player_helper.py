#!/usr/bin/python3
"""Crash-isolated mpv audio player for the Nautilus preview column.

The extension must not decode untrusted media in the Nautilus process: a
native decoder assertion aborts its host before Python can handle it.  This
small process owns an mpv child and exchanges line-delimited JSON with the
extension over stdin/stdout.  mpv is controlled through its supported JSON
IPC protocol, never by parsing terminal output.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys

_OBSERVED_PROPERTIES = (
    "pause",
    "mute",
    "volume",
    "time-pos",
    "duration",
    "seekable",
)


class AudioPlayer:
    def __init__(self, uri: str) -> None:
        executable = shutil.which("mpv")
        if executable is None:
            raise RuntimeError("mpv is required for crash-safe audio previews")

        self._ipc, child_ipc = socket.socketpair()
        child_fd = child_ipc.fileno()
        command = [
            executable,
            "--no-config",
            "--load-scripts=no",
            "--terminal=no",
            "--input-terminal=no",
            "--input-default-bindings=no",
            "--video=no",
            "--audio-display=no",
            "--pause=yes",
            "--keep-open=yes",
            "--volume=100",
            "--mute=no",
            f"--input-ipc-client=fd://{child_fd}",
            "--",
            uri,
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(child_fd,),
            )
        except Exception:
            self._ipc.close()
            child_ipc.close()
            raise
        child_ipc.close()

        self._stdin_buffer = b""
        self._ipc_buffer = b""
        self._stopping = False
        self._failed = False
        self._playing = False
        self._muted = False
        self._volume = 1.0
        self._position = 0.0
        self._duration = 0.0
        self._last_state: tuple[bool, bool, float] | None = None
        self._last_position: tuple[float, float] | None = None

    @staticmethod
    def _emit(event: str, **values) -> None:
        try:
            print(
                json.dumps({"event": event, **values}, separators=(",", ":")),
                flush=True,
            )
        except BrokenPipeError:
            pass

    def run(self) -> int:
        for identifier, name in enumerate(_OBSERVED_PROPERTIES, 1):
            self._send_mpv(["observe_property", identifier, name])

        stdin_fd = sys.stdin.fileno()
        while not self._stopping:
            return_code = self._process.poll()
            if return_code is not None:
                if not self._failed:
                    self._emit(
                        "error",
                        message=f"mpv exited unexpectedly with status {return_code}",
                    )
                self._failed = True
                break

            try:
                readable, _writable, _exceptional = select.select(
                    [stdin_fd, self._ipc], [], [], 0.25
                )
            except InterruptedError:
                continue

            if stdin_fd in readable and not self._read_parent_commands(stdin_fd):
                self._stopping = True
                break
            if self._ipc in readable and not self._read_mpv_messages():
                if not self._stopping and not self._failed:
                    self._emit("error", message="mpv closed its control connection")
                    self._failed = True
                break

        self.close()
        return 1 if self._failed else 0

    def stop(self) -> None:
        self._stopping = True

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                self._send_mpv(["quit"])
                process.wait(timeout=0.75)
            except (BrokenPipeError, ConnectionError, OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                else:
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
        self._ipc.close()

    def _read_parent_commands(self, stdin_fd: int) -> bool:
        try:
            chunk = os.read(stdin_fd, 65536)
        except OSError as error:
            self._emit("error", message=f"Could not read player command: {error}")
            self._failed = True
            return False
        if not chunk:
            return False
        self._stdin_buffer += chunk
        while b"\n" in self._stdin_buffer:
            raw_line, self._stdin_buffer = self._stdin_buffer.split(b"\n", 1)
            if raw_line:
                self._apply_parent_command(raw_line)
            if self._stopping:
                break
        return True

    def _apply_parent_command(self, raw_line: bytes) -> None:
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise TypeError("command must be a JSON object")
            command = message.get("command")
            if command == "play":
                if self._duration > 0 and self._position >= self._duration - 0.05:
                    self._send_mpv(["seek", 0.0, "absolute+exact"])
                self._send_mpv(["set_property", "pause", False])
            elif command == "pause":
                self._send_mpv(["set_property", "pause", True])
            elif command == "mute":
                self._send_mpv(["set_property", "mute", bool(message.get("value"))])
            elif command == "volume":
                volume = max(0.0, min(1.0, float(message.get("value", 1.0))))
                self._send_mpv(["set_property", "volume", volume * 100])
                if volume > 0:
                    self._send_mpv(["set_property", "mute", False])
            elif command == "seek":
                seconds = max(0.0, float(message.get("value", 0.0)))
                self._send_mpv(["seek", seconds, "absolute+exact"])
            elif command == "quit":
                self._stopping = True
            else:
                raise ValueError(f"unknown command {command!r}")
        except (TypeError, ValueError, UnicodeDecodeError) as error:
            self._emit("error", message=f"Invalid player command: {error}")
            self._failed = True
            self._stopping = True
        except (BrokenPipeError, ConnectionError, OSError) as error:
            self._emit("error", message=f"Could not control mpv: {error}")
            self._failed = True
            self._stopping = True

    def _send_mpv(self, command: list) -> None:
        payload = json.dumps({"command": command}, separators=(",", ":"))
        self._ipc.sendall(payload.encode("utf-8") + b"\n")

    def _read_mpv_messages(self) -> bool:
        try:
            chunk = self._ipc.recv(65536)
        except OSError as error:
            self._emit("error", message=f"Could not read mpv state: {error}")
            self._failed = True
            return False
        if not chunk:
            return False
        self._ipc_buffer += chunk
        while b"\n" in self._ipc_buffer:
            raw_line, self._ipc_buffer = self._ipc_buffer.split(b"\n", 1)
            if not raw_line:
                continue
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            self._apply_mpv_message(message)
        return True

    def _apply_mpv_message(self, message: dict) -> None:
        event = message.get("event")
        if event == "property-change":
            self._apply_property(message.get("name"), message.get("data"))
        elif event == "file-loaded":
            self._emit("ready")
        elif event == "end-file":
            reason = message.get("reason")
            if reason == "error":
                self._emit(
                    "error",
                    message=message.get("file_error") or "mpv could not decode this file",
                )
                self._failed = True
                self._stopping = True
            elif reason == "eof":
                self._playing = False
                self._position = self._duration
                self._emit_state()
                self._emit_position()
        elif event == "shutdown" and not self._stopping:
            self._emit("error", message="mpv stopped unexpectedly")
            self._failed = True
            self._stopping = True

    def _apply_property(self, name: str | None, value) -> None:
        if name == "pause" and value is not None:
            self._playing = not bool(value)
            self._emit_state()
        elif name == "mute" and value is not None:
            self._muted = bool(value)
            self._emit_state()
        elif name == "volume" and value is not None:
            self._volume = max(0.0, min(1.0, float(value) / 100.0))
            self._emit_state()
        elif name == "time-pos":
            self._position = max(0.0, float(value or 0.0))
            self._emit_position()
        elif name == "duration":
            self._duration = max(0.0, float(value or 0.0))
            self._emit_position()

    def _emit_state(self) -> None:
        state = (self._playing, self._muted, self._volume)
        if state == self._last_state:
            return
        self._last_state = state
        self._emit(
            "state",
            playing=self._playing,
            muted=self._muted,
            volume=self._volume,
        )

    def _emit_position(self) -> None:
        position = (self._position, self._duration)
        if position == self._last_position:
            return
        self._last_position = position
        self._emit("position", position=self._position, duration=self._duration)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1]:
        print("usage: media_player_helper.py URI", file=sys.stderr)
        return 2
    try:
        player = AudioPlayer(argv[1])
    except Exception as error:
        AudioPlayer._emit("error", message=str(error))
        return 1

    def stop_player(_signum, _frame) -> None:
        player.stop()

    signal.signal(signal.SIGTERM, stop_player)
    signal.signal(signal.SIGINT, stop_player)
    return player.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
