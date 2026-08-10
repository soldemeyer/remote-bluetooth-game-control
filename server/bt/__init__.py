"""Bluetooth HID emulation.

Layering:

  * ``profiles/`` -- what we pretend to be (report descriptors, report builders)
  * ``adapter``   -- enumerating and configuring physical dongles
  * ``hid``       -- the L2CAP HID server that talks to a console
  * ``sink``      -- where reports go, so the datapath can target mock or real

Everything Linux-specific is confined to ``adapter`` and ``hid``, so the rest
of the server -- and the whole test suite -- runs on any platform.
"""

from __future__ import annotations

from server.bt.profiles import PROFILES, TargetProfile, create_profile
from server.bt.sink import HIDSink, MockSink, NullSink

__all__ = [
    "PROFILES",
    "HIDSink",
    "MockSink",
    "NullSink",
    "TargetProfile",
    "create_profile",
]
