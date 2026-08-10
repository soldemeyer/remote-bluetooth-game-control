"""Gamepad input backends.

The concrete backend is chosen at runtime so the rest of the client never has
to care which one is in use, and so a missing SDL2 install degrades to a clear
error rather than an import crash at startup.
"""

from __future__ import annotations

import logging

from client.input.base import DeviceInfo, InputBackend, InputBackendError, PolledInput
from client.input.synthetic import SyntheticBackend

log = logging.getLogger(__name__)

__all__ = [
    "DeviceInfo",
    "InputBackend",
    "InputBackendError",
    "PolledInput",
    "SyntheticBackend",
    "create_backend",
]


def create_backend(kind: str = "auto", **kwargs) -> InputBackend:
    """Build an input backend.

    ``kind``:
      * ``auto``      -- SDL2 if available, otherwise raise with install advice
      * ``sdl2``      -- force SDL2
      * ``synthetic`` -- fake controllers for testing
    """
    if kind == "synthetic":
        return SyntheticBackend(**kwargs)

    if kind in ("auto", "sdl2"):
        from client.input import sdl2_backend

        if sdl2_backend.is_available():
            return sdl2_backend.SDL2Backend(**kwargs)

        raise InputBackendError(
            "No gamepad backend available. PySDL2 is not installed -- "
            'run: pip install -e ".[client]"'
        )

    raise ValueError(f"Unknown input backend: {kind!r}")
