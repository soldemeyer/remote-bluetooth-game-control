"""Shared Qt look and feel for the two desktop applications.

Its own package rather than a module inside either app, for two reasons:

* ``common/`` must stay dependency-light and platform-neutral -- "no SDL2, no
  BlueZ, no Qt" -- so the toolkit-specific half of the design system cannot
  live there.
* ``videoserver`` cannot import from ``client``: the ``video`` extra does not
  install the client package, so a video-server-only machine has no such
  module. The dependency would only fail once somebody installed it that way.

The design *tokens* stay in ``common.design``; this is the Qt rendering of
them. Nothing here decides what the product looks like -- it only expresses it.
"""

from qtui.theme import apply_theme, icon, stylesheet

__all__ = ["apply_theme", "icon", "stylesheet"]
