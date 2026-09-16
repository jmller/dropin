"""Best-effort interactive progress for long-running CLI operations.

Progress is presentation only: events are ephemeral, renderer failures are
ignored, and no caller may use them as evidence for a pipeline transition.
Only a background worker writes, through a temporarily nonblocking terminal fd,
so cosmetic output cannot stall the archive pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading
from typing import Mapping, TextIO
import unicodedata


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    label: str
    item_name: str | None = None
    item_index: int | None = None
    item_total: int | None = None
    completed: int | None = None


def _safe_text(value: str) -> str:
    """Return terminal-safe, single-line text without trusting user names."""
    return "".join(character if character.isprintable() and character != "\x1b"
                   else "?" for character in value)


def _cell_width(value: str) -> int:
    width = 0
    for character in value:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
    return width


def _truncate_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if _cell_width(value) <= width:
        return value
    if width == 1:
        return "…"
    result: list[str] = []
    used = 0
    for character in value:
        cells = (0 if unicodedata.combining(character) else
                 2 if unicodedata.east_asian_width(character) in ("W", "F") else 1)
        if used + cells > width - 1:
            break
        result.append(character)
        used += cells
    return "".join(result).rstrip() + "…"


def format_progress_line(event: ProgressEvent, *, tick: int, width: int) -> str:
    """Format one unstyled line bounded by terminal display cells."""
    width = max(1, width)
    spinner = "|/-\\"[tick % 4]
    pulse_width = 10
    position = tick % (pulse_width * 2 - 2)
    if position >= pulse_width:
        position = pulse_width * 2 - 2 - position
    pulse = "".join("=" if index < position else
                    ">" if index == position else "."
                    for index in range(pulse_width))
    label = _safe_text(event.label)
    compact = f"{spinner} {label}"
    if width >= 36:
        compact = f"{spinner} [{pulse}] {label}"
    details: list[str] = []
    if event.item_name:
        details.append(_safe_text(event.item_name))
    if event.item_index is not None and event.item_total is not None:
        details.append(f"item {event.item_index}/{event.item_total}")
    elif event.item_index is not None:
        details.append(f"item {event.item_index}")
    if event.completed is not None and event.item_total is not None:
        details.append(f"{event.completed}/{event.item_total} done")
    if details:
        compact += " | " + " | ".join(details)
    return _truncate_cells(compact, width)


class TerminalProgress:
    """Continuously redraw the latest progress event on one stderr TTY line."""

    def __init__(self, *, stream: TextIO, interval: float = 0.12,
                 width: int | None = None, color: bool = True) -> None:
        self.stream = stream
        self.interval = interval
        self.width = width
        self.color = color
        self._fd: int | None = None
        self._was_blocking = True
        self._event: ProgressEvent | None = None
        self._event_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_width = 0
        self._tick = 0

    @staticmethod
    def supported(stream: TextIO, *, json_output: bool, dry_run: bool,
                  environ: Mapping[str, str] | None = None) -> bool:
        environ = os.environ if environ is None else environ
        if json_output or dry_run or environ.get("TERM", "") == "dumb":
            return False
        try:
            return bool(stream.isatty()) and stream.fileno() >= 0
        except (AttributeError, OSError, ValueError):
            return False

    @property
    def running(self) -> bool:
        return self._running

    def __enter__(self) -> "TerminalProgress":
        if self._running:
            return self
        try:
            self._fd = self.stream.fileno()
            self._was_blocking = os.get_blocking(self._fd)
            os.set_blocking(self._fd, False)
        except (AttributeError, OSError, ValueError):
            self._fd = None
            return self
        self._stop.clear()
        self._wake.clear()
        self._running = True
        self._thread = None
        try:
            thread = threading.Thread(target=self._animate,
                                      name="dropin-progress", daemon=True)
            thread.start()
            self._thread = thread
        except Exception:
            self._running = False
            self._stop.set()
            self._restore_fd()
        return self

    def update(self, event: ProgressEvent) -> None:
        """Publish only; terminal I/O is exclusively the worker's job."""
        if not self._running:
            return
        with self._event_lock:
            self._event = event
        self._wake.set()

    def close(self) -> None:
        self._running = False
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()  # nonblocking fd writes guarantee bounded shutdown
        self._clear()
        self._restore_fd()
        self._thread = None

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _restore_fd(self) -> None:
        if self._fd is not None:
            try:
                os.set_blocking(self._fd, self._was_blocking)
            except OSError:
                pass
        self._fd = None

    def _animate(self) -> None:
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            if self._stop.is_set():
                return
            self._draw_once()

    def _terminal_width(self) -> int:
        if self.width is not None:
            return max(1, self.width)
        if self._fd is not None:
            try:
                columns = os.get_terminal_size(self._fd).columns
                if columns > 0:
                    return columns
            except OSError:
                pass
        return 80

    def _write(self, value: str) -> bool:
        if self._fd is None:
            return False
        try:
            payload = value.encode(getattr(self.stream, "encoding", None) or "utf-8",
                                   errors="replace")
            os.write(self._fd, payload)
            return True
        except (BlockingIOError, BrokenPipeError, OSError, UnicodeError, ValueError):
            return False

    def _draw_once(self) -> None:
        with self._event_lock:
            event = self._event
        if event is None:
            return
        line = format_progress_line(event, tick=self._tick,
                                    width=self._terminal_width())
        self._tick += 1
        if self.color:
            output = f"\r\x1b[2K\x1b[36m{line}\x1b[0m"
        else:
            padding = " " * max(0, self._last_width - _cell_width(line))
            output = f"\r{line}{padding}"
        if self._write(output):
            self._last_width = _cell_width(line)

    def _clear(self) -> None:
        if self.color:
            self._write("\r\x1b[2K")
        elif self._last_width:
            self._write("\r" + " " * self._last_width + "\r")
        self._last_width = 0
