"""
Progress display for batch runs.

On a terminal: one status line on stderr, redrawn in place —

    docgen ▕██████████░░░░░░░░░░▏ 6/12  ✓5 ✗1  ↺2  running DEMO-7, DEMO-8  0:41 ETA 0:39

Log lines still print: the logging handler clears the bar, writes the
record, and redraws it underneath (see logging_setup). Off a terminal (CI,
server, pipes) nothing is drawn and each completion is logged instead.
"""
from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
from typing import TextIO

log = logging.getLogger(__name__)

_active: Progress | None = None
_active_lock = threading.Lock()


def active() -> Progress | None:
    return _active


class Progress:
    def __init__(self, total: int, *, label: str = "docgen", stream: TextIO | None = None,
                 enabled: bool | None = None):
        self.total = total
        self.label = label
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else enabled
        self.done = 0
        self.failed = 0
        self.unchanged = 0
        self.running: list[str] = []
        self.started = time.monotonic()
        self._lock = threading.RLock()
        self._drawn = False

    def __enter__(self) -> Progress:
        global _active
        with _active_lock:
            _active = self
        self.redraw()
        return self

    def __exit__(self, *exc) -> None:
        global _active
        self.clear()
        with _active_lock:
            _active = None

    # ── events ───────────────────────────────────────────────

    def start(self, key: str) -> None:
        with self._lock:
            self.running.append(key)
            self.redraw()

    def finish(self, key: str, *, ok: bool, action: str = "") -> None:
        with self._lock:
            if key in self.running:
                self.running.remove(key)
            self.done += 1
            self.failed += 0 if ok else 1
            self.unchanged += 1 if action == "unchanged" else 0
            done = self.done
            self.redraw()
        # Logged outside our lock: the log handler takes its own lock and then ours.
        if not self.enabled:
            log.info("progress", extra={"done": done, "total": self.total, "ticket": key,
                                        "status": action or ("ok" if ok else "failed")})

    # ── drawing ──────────────────────────────────────────────

    def line(self) -> str:
        elapsed = time.monotonic() - self.started
        width = 20
        filled = int(width * self.done / self.total) if self.total else width
        bar = "█" * filled + "░" * (width - filled)
        parts = [f"{self.label} ▕{bar}▏ {self.done}/{self.total}",
                 f"✓{self.done - self.failed} ✗{self.failed}"]
        if self.unchanged:
            parts.append(f"={self.unchanged} unchanged")
        if self.running:
            more = len(self.running) - 3
            shown = ", ".join(self.running[:3]) + (f" +{more}" if more > 0 else "")
            parts.append(f"running {shown}")
        eta = ""
        if 0 < self.done < self.total:
            eta = f" ETA {_clock(elapsed / self.done * (self.total - self.done))}"
        parts.append(f"{_clock(elapsed)}{eta}")
        text = "  ".join(parts)
        columns = shutil.get_terminal_size((100, 20)).columns
        return text[:columns - 1]

    def clear(self) -> None:
        with self._lock:
            if self.enabled and self._drawn:
                self.stream.write("\r\033[2K")
                self.stream.flush()
                self._drawn = False

    def redraw(self) -> None:
        with self._lock:
            if self.enabled:
                self.stream.write("\r\033[2K" + self.line())
                self.stream.flush()
                self._drawn = True


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"
