"""Bluetooth pairing agent.

DO NOT add ``from __future__ import annotations`` to this file -- see
server/bt/_dbus_profile.py for why dbus-next cannot tolerate PEP 563.

Why this exists
---------------
bluetoothd will not complete pairing without an agent to answer authentication
requests. Without one it falls back to legacy PIN entry, and the host shows a
"Enter the PIN for ..." prompt for a device that has no keypad to type one on.

A real gamepad declares **NoInputNoOutput** capability: it cannot display a
passkey or accept one, so Secure Simple Pairing selects the "Just Works"
association model and both sides pair silently. That is what this agent
reproduces.

The legacy handlers are still implemented, returning the conventional ``0000``
and ``0``, because a host that insists on legacy pairing would otherwise hang
waiting for a reply that never comes. Accepting a fixed PIN is exactly what
mass-market gamepads and headsets do.

Security note: this auto-accepts any pairing request while an adapter is in
pairing mode. That is the correct behaviour for a controller -- the operator
opened a bounded pairing window deliberately -- and matches every consumer
Bluetooth gamepad. Access control lives at the RBGC layer (password plus
operator approval), not at Bluetooth pairing.
"""

import logging

from dbus_next.service import ServiceInterface, method

log = logging.getLogger(__name__)

AGENT_PATH = "/rbgc/agent"

#: "Just Works" pairing: we can neither show a passkey nor accept one, which is
#: literally true of a gamepad and is what avoids the PIN prompt.
AGENT_CAPABILITY = "NoInputNoOutput"

#: Conventional fallback for hosts that insist on legacy pairing.
LEGACY_PIN = "0000"


class PairingAgent(ServiceInterface):
    """org.bluez.Agent1 that accepts pairing without user interaction."""

    def __init__(self, on_paired=None):
        super().__init__("org.bluez.Agent1")
        #: Called with the device object path once pairing is authorized, so
        #: the caller can mark the device trusted.
        self._on_paired = on_paired

    @method()
    def Release(self):  # noqa: N802 - D-Bus method name
        log.debug("Pairing agent released by BlueZ")

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802
        """Legacy pairing. Modern hosts use SSP and never call this.

        Warning, not info: reaching here means Secure Simple Pairing was **not**
        used, so the host is showing a PIN box that a console cannot answer. It
        is the single most useful line in the log when pairing misbehaves, and
        it is worth being loud about -- it sat at info level through eight
        rounds of debugging the wrong machine.
        """
        log.warning(
            "Legacy PIN pairing requested by %s -- Secure Simple Pairing was NOT used. "
            "Supplying %s. Almost always this means the *host* is not advertising SSP "
            "support; see 'When a host demands a PIN' in CLAUDE.md to confirm which "
            "side is at fault in one measurement.",
            device,
            LEGACY_PIN,
        )
        self._notify(device)
        return LEGACY_PIN

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802
        """Also legacy: SSP "Just Works" never asks for a passkey."""
        log.warning(
            "Passkey requested by %s -- Secure Simple Pairing was NOT used. Supplying 0.",
            device,
        )
        self._notify(device)
        return 0

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802
        # A gamepad has no display; nothing to do but acknowledge.
        log.debug("BlueZ asked to display PIN %s for %s", pincode, device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802
        log.debug("BlueZ asked to display passkey %06d for %s", passkey, device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802
        """Numeric comparison. Returning without error confirms the match."""
        log.info("Auto-confirming pairing with %s", device)
        self._notify(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802
        """Just Works authorization -- the usual path for a gamepad."""
        log.info("Auto-authorizing pairing with %s", device)
        self._notify(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802
        """Service-level authorization, asked on each connect.

        Returning without error accepts. Refusing here would block reconnects
        even from an already-paired console.
        """
        log.debug("Authorizing service %s for %s", uuid, device)

    @method()
    def Cancel(self):  # noqa: N802
        log.info("Pairing cancelled by the remote device")

    def _notify(self, device_path):
        if self._on_paired is None:
            return
        try:
            self._on_paired(device_path)
        except Exception:
            log.debug("on_paired callback failed", exc_info=True)


async def register_agent(on_paired=None):
    """Register the pairing agent and make it the default.

    Returns the owning bus, which must stay alive: BlueZ drops the agent when
    its D-Bus connection closes, and pairing would silently start prompting for
    a PIN again.
    """
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    agent = PairingAgent(on_paired=on_paired)
    bus.export(AGENT_PATH, agent)

    introspection = await bus.introspect("org.bluez", "/org/bluez")
    obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
    manager = obj.get_interface("org.bluez.AgentManager1")

    await manager.call_register_agent(AGENT_PATH, AGENT_CAPABILITY)

    # Becoming the *default* agent is what makes BlueZ route requests here
    # rather than to whatever bluetoothctl session happens to be running.
    await manager.call_request_default_agent(AGENT_PATH)

    log.info("Pairing agent registered (%s -- Just Works, no PIN)", AGENT_CAPABILITY)
    return bus


async def unregister_agent(bus):
    """Unregister and drop the connection."""
    try:
        introspection = await bus.introspect("org.bluez", "/org/bluez")
        obj = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
        manager = obj.get_interface("org.bluez.AgentManager1")
        await manager.call_unregister_agent(AGENT_PATH)
    except Exception:
        log.debug("Could not cleanly unregister the pairing agent", exc_info=True)
    finally:
        try:
            bus.disconnect()
        except Exception:
            pass
