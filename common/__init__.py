"""Shared protocol, crypto, state and timing primitives.

Imported by both the client and the server, so this package must stay
platform-neutral and dependency-light: no SDL2, no BlueZ, no Qt.
"""

from __future__ import annotations

__version__ = "0.1.0"
