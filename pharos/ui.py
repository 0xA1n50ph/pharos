"""Interaction with the user in the terminal (questions, confirmations, progress)."""
from __future__ import annotations

import sys
import time


class UserExit(Exception):
    """The user chose to quit (or input ended). The project is saved so it can be resumed."""


def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


class UI:
    def __init__(self, auto: bool = False):
        self.auto = auto
        self.tty = sys.stdout.isatty()

    def info(self, msg: str = "") -> None:
        print(msg, flush=True)

    def heading(self, msg: str) -> None:
        print("\n" + msg + "\n" + "─" * len(msg), flush=True)

    def warn(self, msg: str) -> None:
        print("Warning: " + msg, flush=True)

    def error(self, msg: str) -> None:
        print("Error: " + msg, file=sys.stderr, flush=True)

    def ask(self, text: str, default: str = "") -> str:
        suffix = f" [{default}]" if default else ""
        if self.auto:
            print(f"{text}{suffix}: {default}  (automatic)", flush=True)
            return default
        try:
            answer = input(f"{text}{suffix}: ").strip()
        except EOFError:
            raise UserExit("input ended") from None
        return answer or default

    def confirm(self, text: str, default: bool = True) -> bool:
        r = self.ask(text + (" (Y/n)" if default else " (y/N)"), "").lower()
        if not r:
            return default
        return r in ("y", "yes")

    def multiline(self, text: str) -> str:
        """Reads lines until a blank line."""
        if self.auto:
            return ""
        print(text, flush=True)
        lines = []
        while True:
            try:
                l = input()
            except EOFError:
                break
            if not l.strip():
                break
            lines.append(l)
        return "\n".join(lines).strip()


class Progress:
    """Translation progress callback, with ETA."""

    def __init__(self, ui: UI, label: str = "Translating"):
        self.ui = ui
        self.label = label
        self.start = time.monotonic()
        self.last = self.start
        self.durations: list[float] = []
        self.alerts = 0

    def __call__(self, i: int, total: int, block, result) -> None:
        now = time.monotonic()
        if not result.from_cache:
            self.durations.append(now - self.last)
        self.last = now
        new = [a for a in result.alerts if a not in ("already_in_target", "manually_corrected")]
        if new and not result.from_cache:
            self.alerts += 1
            print(f"\n  ! {block.id}: {', '.join(new)}", flush=True)
        avg = sum(self.durations[-30:]) / max(1, len(self.durations[-30:]))
        eta = avg * (total - i)
        line = f"{self.label}: {i}/{total} ({100 * i // max(1, total)}%)  remaining ~{_hms(eta)}  {block.id}"
        if self.ui.tty:
            sys.stdout.write("\r" + line.ljust(78))
            if i == total:
                sys.stdout.write("\n")
            sys.stdout.flush()
        elif i % 10 == 0 or i == total:
            print(line, flush=True)


class Bar:
    """Simple progress with remaining time, for long stages (OCR, for instance)."""

    def __init__(self, ui: UI, label: str):
        self.ui = ui
        self.label = label
        self.start = time.monotonic()
        self._log_step = 0

    def __call__(self, done: int, total: int, detail: str = "") -> None:
        elapsed = time.monotonic() - self.start
        eta = elapsed / done * (total - done) if done else 0
        line = f"{self.label}: {done}/{total} ({100 * done // max(1, total)}%)  remaining ~{_hms(eta)}"
        if detail:
            line += f"  {detail}"
        if self.ui.tty:
            sys.stdout.write("\r" + line.ljust(78))
            if done == total:
                sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            step = max(1, total // 10)
            if done % step == 0 or done == total:
                print(line, flush=True)
