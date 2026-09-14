"""Usable stdio for a windowed Windows build.

Both apps ship as **windowed** executables (`console=False`), so double-clicking
one does not leave a black console window sitting behind the GUI for the rest
of the session. That is the whole reason this module exists: a windowed process
on Windows starts with `sys.stdout` and `sys.stderr` set to `None`, so anything
that prints -- `--help`, `--headless`, `--list-devices`, a logging handler --
would write into the void or raise `AttributeError: 'NoneType' has no attribute
'write'`.

One copy, because there were two and they had already drifted. The video
server's opened `CONOUT$` directly, which works from a terminal and silently
throws away redirection: `rbgc-video --headless > log.txt` wrote to the console
and left an empty file. Binding the file descriptors first is what makes a pipe
or a file work, and it has to be tried *before* attaching to a console.

Stdlib only, and a no-op everywhere but Windows, so it is fine in `common/`.
"""

from __future__ import annotations

import sys

__all__ = ["attach_console_if_needed"]


class NullWriter:
    """Discards output. Keeps `print` working when there is nowhere to write."""

    def write(self, _data: str) -> int:
        return 0

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("no underlying stream")


def _bind(fileno: int):
    """A text stream over a file descriptor, or None if it is not usable."""
    try:
        return open(fileno, "w", buffering=1, errors="replace", closefd=False)
    except OSError:
        return None


def attach_console_if_needed() -> None:
    """Give a windowed process somewhere to print, if anywhere exists.

    Three cases, in this order, and the order is the point:

    1. **Output is redirected** to a file or a pipe -- fds 1 and 2 are already
       valid and only need Python objects wrapping them. Tried first, because
       attaching to a console would send the output somewhere the person who
       typed `> log.txt` is not looking.
    2. **Launched from a terminal** -- `AttachConsole(ATTACH_PARENT_PROCESS)`
       borrows that console, after which the fds become valid.
    3. **Launched from Explorer with no redirection** -- there is nowhere to
       write at all, so bind a sink that discards rather than raises.

    A normal `python -m ...` run already has both streams and returns at the
    first line, so this costs nothing outside the packaged build.
    """
    if sys.platform != "win32":
        return
    if sys.stdout is not None and sys.stderr is not None:
        return

    import ctypes

    stdout, stderr = _bind(1), _bind(2)

    if stdout is None and stderr is None:
        ATTACH_PARENT_PROCESS = -1
        try:
            attached = ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
        except Exception:  # pragma: no cover - no kernel32 is not a real case
            attached = False
        if attached:
            stdout, stderr = _bind(1), _bind(2)

    sys.stdout = stdout or NullWriter()
    sys.stderr = stderr or NullWriter()
