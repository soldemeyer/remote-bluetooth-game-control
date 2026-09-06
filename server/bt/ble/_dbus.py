"""One import site for the D-Bus binding, so the choice is made in one place.

``dbus-fast`` is an API-compatible fork of ``dbus-next`` whose marshaller is
Cython rather than pure Python. That matters here more than anywhere else in
the codebase, because a BLE input report *is* a D-Bus signal: every report a
player generates gets marshalled, and on the reference Pi that cost was
measured at

    Message._marshall            146.8 us      <- pure-Python dbus-next
    Message.new_signal            19.9 us
    property reflection            5.0 us
    _path_exports identity scan    9.7 us
    ------------------------------------
    total                        188.3 us  per report, per player

against a documented **1 ms whole-system** software budget. The Classic path's
entire server-side packet handling is 0.03-0.09 ms by comparison.

``dbus-next`` stays as the fallback and must keep working: it is what a machine
without the optional dependency gets, and it is still what ``agent.py``,
``sdp.py`` and ``adapter_dbus.py`` use. Mixing the two in one process is safe --
they are separate packages holding separate connections, and no object crosses
between them.

Importing this module **never fails**, even where neither binding is installed.
That is deliberate and is what lets ``peripheral.py`` keep being importable on a
machine with no D-Bus at all, the same way ``hogp.py`` is: the test suite builds
a ``BLESink`` on Windows. Only actually *using* a binding name raises, and it
raises saying which package to install rather than ``No module named``.

.. warning::
   Do **not** add ``from __future__ import annotations`` to any module that
   defines a ``ServiceInterface``. Both bindings read method annotations at
   decoration time and require them to be string constants holding D-Bus type
   signatures; PEP 563 stores the source text instead and turns every signature
   into nonsense. This applies to dbus-fast exactly as it does to dbus-next --
   the fork did not change that behaviour. See ``server/bt/_dbus_profile.py``.
"""

import logging

log = logging.getLogger(__name__)

#: Which binding won, or None where neither is installed.
BINDING = None

_NAMES = {}

try:
    from dbus_fast import BusType, Message, Variant
    from dbus_fast.aio import MessageBus
    from dbus_fast.constants import PropertyAccess
    from dbus_fast.service import ServiceInterface, dbus_property, method

    BINDING = "dbus-fast"
except ImportError:
    try:
        from dbus_next import BusType, Message, Variant
        from dbus_next.aio import MessageBus
        from dbus_next.constants import PropertyAccess
        from dbus_next.service import ServiceInterface, dbus_property, method

        BINDING = "dbus-next"
    except ImportError:
        # Neither is present. Leave the names undefined and let the module
        # __getattr__ below explain, at the point of use, rather than making
        # this module unimportable for the code that needs none of them.
        pass

if BINDING is not None:
    _NAMES = {
        "BusType": BusType,
        "Message": Message,
        "MessageBus": MessageBus,
        "PropertyAccess": PropertyAccess,
        "ServiceInterface": ServiceInterface,
        "Variant": Variant,
        "dbus_property": dbus_property,
        "method": method,
    }


_BINDING_NAMES = frozenset(
    [
        "BusType",
        "Message",
        "MessageBus",
        "PropertyAccess",
        "ServiceInterface",
        "Variant",
        "dbus_property",
        "method",
    ]
)


def __getattr__(name):
    """Explain a missing binding at the point of use, not at import.

    ``from server.bt.ble._dbus import ServiceInterface`` goes through here, so
    a module that genuinely needs a binding still fails immediately -- it just
    fails with something actionable instead of ``No module named 'dbus_next'``
    from four frames down.
    """
    if name in _BINDING_NAMES:
        raise ImportError(
            f"{name} needs a D-Bus binding. Install dbus-fast (recommended, "
            f"the marshaller is compiled) or dbus-next:\n"
            f"    pip install dbus-fast"
        )
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def writer_backlog(bus):
    """How many messages are queued but not yet written to the bus socket.

    The single most useful number for diagnosing this subsystem, and nothing
    reported it. Both bindings buffer outbound messages in an **unbounded**
    queue and hand back success the moment a message is queued -- so a backlog
    is invisible from every counter we own: reports climb, no failure is
    recorded, and latency simply grows. It is the same failure the Classic path
    documents at the L2CAP boundary, one layer further out.

    Reaching into a private attribute is deliberate, and isolating it here is
    why: it is a read-only diagnostic, it differs between the two bindings, and
    a binding that stops exposing it must degrade to "unknown" rather than take
    the datapath down. Returns -1 when it cannot be read.
    """
    try:
        messages = bus._writer.messages  # noqa: SLF001
    except Exception:
        return -1

    # dbus-next holds a queue.Queue, dbus-fast a collections.deque. Try both
    # rather than picking one: which binding is in use is decided at import,
    # and a diagnostic that silently reads -1 on the fast path is worse than
    # no diagnostic, because it looks like a measurement.
    try:
        return messages.qsize()
    except AttributeError:
        pass
    except Exception:
        return -1

    try:
        return len(messages)
    except Exception:
        return -1
