"""Design tokens shared by the two Qt applications and the web GUI.

Lives in ``common/`` for the same reason everything else here does: it is
imported by both sides and depends on nothing but the standard library. No Qt,
no aiohttp, no colour library.
"""

from common.design.tokens import Color, Motion, Radius, Space, Type, palette

__all__ = ["Color", "Motion", "Radius", "Space", "Type", "palette"]
