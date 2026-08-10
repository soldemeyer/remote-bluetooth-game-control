"""The org.bluez.Profile1 D-Bus object, isolated in its own module.

DO NOT add ``from __future__ import annotations`` to this file.

``dbus_next`` reads method annotations at decoration time and requires them to
be **string constants holding D-Bus type signatures** (``"o"``, ``"h"``,
``"a{sv}"``). PEP 563 defers annotation evaluation and stores the *source text*
instead, which turns ``"o"`` into ``'"o"'`` and makes every signature invalid.

Likewise, a void D-Bus method must have **no return annotation at all**.
Writing ``-> None`` fails with::

    ValueError: service annotations must be a string constant (got None)

Both mistakes produce errors far from their cause, which is why this lives in a
dedicated module with this note rather than inside sdp.py.
"""

import logging

from dbus_next.service import ServiceInterface, method

log = logging.getLogger(__name__)


class HIDProfile(ServiceInterface):
    """Minimal org.bluez.Profile1 implementation.

    BlueZ requires an exported object implementing this interface before it
    will accept a profile registration. We serve the actual HID connection
    ourselves over raw L2CAP sockets (see server/bt/hid.py), so these callbacks
    only need to exist and not raise -- BlueZ calls them, we acknowledge, and
    the real work happens on our own listeners.
    """

    def __init__(self):
        super().__init__("org.bluez.Profile1")

    @method()
    def Release(self):  # noqa: N802 - D-Bus method name
        log.info("BlueZ released the HID profile")

    @method()
    def NewConnection(self, path: "o", fd: "h", properties: "a{sv}"):  # noqa
        # We accept connections on our own L2CAP listeners rather than taking
        # the fd BlueZ offers here, so there is nothing to do but acknowledge.
        log.debug("BlueZ NewConnection for %s (served via our L2CAP listeners)", path)

    @method()
    def RequestDisconnection(self, path: "o"):  # noqa: N802
        log.debug("BlueZ RequestDisconnection for %s", path)
