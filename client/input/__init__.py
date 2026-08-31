"""Gamepad input backends.

The concrete backend is chosen at runtime so the rest of the client never has
to care which one is in use, and so a missing SDL2 install degrades to a clear
error rather than an import crash at startup.
"""

from __future__ import annotations

import logging

from client.input.base import DeviceInfo, InputBackend, InputBackendError, PolledInput
from client.input.composite import CompositeBackend
from client.input.synthetic import SyntheticBackend

log = logging.getLogger(__name__)

__all__ = [
    "CompositeBackend",
    "DeviceInfo",
    "InputBackend",
    "InputBackendError",
    "PolledInput",
    "SyntheticBackend",
    "create_backend",
]


def create_backend(kind: str = "auto", *, keyboard: bool = False, **kwargs) -> InputBackend:
    """Build an input backend.

    ``kind``:
      * ``auto``      -- SDL2 if available, otherwise raise with install advice
      * ``sdl2``      -- force SDL2
      * ``synthetic`` -- fake controllers for testing

    ``keyboard`` adds the keyboard as an extra virtual gamepad alongside the
    real ones. GUI-only: it needs window focus to see key events, so
    ``--headless`` never asks for it.
    """
    base: InputBackend

    if kind == "synthetic":
        base = SyntheticBackend(**kwargs)
    elif kind in ("auto", "sdl2"):
        from client.input import sdl2_backend

        if sdl2_backend.is_available():
            base = sdl2_backend.SDL2Backend(**kwargs)
        elif keyboard:
            # No gamepad library, but the keyboard alone is still a usable
            # controller -- better than refusing to start.
            base = None
        else:
            # Say *why*, not just that it is missing. A packaged build ships
            # PySDL2, so "not installed" is wrong there and points at a fix
            # that cannot work -- the real cause is a bundle missing the
            # shared library PySDL2 dlopens.
            detail = sdl2_backend.import_error()
            raise InputBackendError(
                "No gamepad backend available: "
                + (detail or "PySDL2 could not be imported")
                + '.\nFrom source, run: pip install -e ".[client]"'
            )
    else:
        raise ValueError(f"Unknown input backend: {kind!r}")

    if not keyboard:
        return base

    from client.input.composite import CompositeBackend
    from client.input.keyboard_backend import KeyboardBackend, default_keyboard_mapping

    keyboard_backend = KeyboardBackend(default_keyboard_mapping())
    return CompositeBackend([b for b in (base, keyboard_backend) if b is not None])
