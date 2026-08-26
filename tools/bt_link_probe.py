"""Read what the Bluetooth radios are *actually* doing.

Run it on the server, with adapters up and ideally a console connected::

    sudo python -m tools.bt_link_probe

This exists because this project has repeatedly been misled by management-layer
state. ``hciconfig`` writes raw HCI below MGMT, ``btmgmt`` is a second MGMT
client, and ``bluetoothd`` owns the state both are writing -- so the three
disagree, and every one of them will happily report a value the radio is not
using. The class of device reverting, an adapter answering inquiries while
every layer said it was hidden, an expired pairing window still advertising:
all of them were invisible until somebody read the controller directly.

So everything here is read over HCI, from the controller, and nothing is
inferred from what we asked for earlier.

What it cannot tell you
-----------------------
**Whether a link is in sniff mode right now.** There is no HCI command to read
current mode; the controller reports it in a Mode Change event when it happens.
What is shown instead is whether sniff is *permitted* by the link policy, which
is the thing we control. To see the mode change itself, watch ``btmon``:

    sudo btmon -T | grep -A2 'Mode Change'

Numbers here are a snapshot. For latency work, take one before a change and one
after, and keep both.
"""

from __future__ import annotations

import argparse
import struct
import sys

try:
    import fcntl
except ImportError:                     # not Linux
    fcntl = None

from server.bt import hci
from server.bt.link import LinkPolicy

#: ``_IOR('H', 212, int)`` -- enumerate the connections on one adapter.
HCIGETCONNLIST = 0x800448D4

#: ``struct hci_conn_info``: handle, bdaddr[6], type, out, state, link_mode.
_CONN_INFO = struct.Struct("<H6sBBHI")

#: Connection types, from ``bluetooth/hci.h``.
_LINK_TYPES = {0x00: "SCO", 0x01: "ACL", 0x02: "eSCO", 0x80: "LE"}


def _format_addr(raw: bytes) -> str:
    """A BD_ADDR arrives from the kernel little-endian."""
    return ":".join(f"{byte:02X}" for byte in reversed(raw))


def list_connections(index: int, max_conns: int = 16) -> list[dict[str, object]]:
    """Live connections on one adapter, via the kernel's connection list ioctl.

    Preferred over guessing handles or matching by address: it is the same view
    the kernel hands ``hcitool con``, and it includes the handle every command
    below is addressed to.
    """
    import socket

    if fcntl is None:
        return []

    try:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, hci.BTPROTO_HCI)
    except OSError as exc:
        print(f"  could not open an HCI socket: {exc}")
        return []

    try:
        buf = bytearray(4 + _CONN_INFO.size * max_conns)
        struct.pack_into("<HH", buf, 0, index, max_conns)
        try:
            fcntl.ioctl(sock.fileno(), HCIGETCONNLIST, buf)
        except OSError as exc:
            print(f"  could not list connections: {exc}")
            return []

        count = struct.unpack_from("<H", buf, 2)[0]
        connections = []
        for i in range(min(count, max_conns)):
            handle, addr, link_type, out, state, mode = _CONN_INFO.unpack_from(
                buf, 4 + i * _CONN_INFO.size
            )
            connections.append(
                {
                    "handle": handle,
                    "peer": _format_addr(addr),
                    "type": _LINK_TYPES.get(link_type, f"0x{link_type:02X}"),
                    "outgoing": bool(out),
                    "state": state,
                    "link_mode": mode,
                }
            )
        return connections
    finally:
        sock.close()


def _u16_reply(sock: hci.HCISocket, opcode: int, handle: int) -> int | None:
    """Read a per-connection u16. These replies are all handle then value."""
    try:
        reply = sock.command(opcode, struct.pack("<H", handle))
    except hci.HCIError:
        return None
    return struct.unpack_from("<H", reply, 2)[0] if len(reply) >= 4 else None


def _adapter_state(sock: hci.HCISocket) -> dict[str, object]:
    """Controller-level settings, read from the radio rather than from MGMT.

    Every one of these has been observed disagreeing with what the management
    layers reported, which is the whole reason this function exists.
    """
    state: dict[str, object] = {}

    try:
        state["scan_enable"] = sock.command(hci.OCF_READ_SCAN_ENABLE)[0]
    except (hci.HCIError, IndexError):
        state["scan_enable"] = None

    try:
        raw = sock.command(hci.OCF_READ_CLASS_OF_DEVICE)
        state["class_of_device"] = int.from_bytes(raw[:3], "little")
    except (hci.HCIError, IndexError):
        state["class_of_device"] = None

    try:
        state["ssp_mode"] = sock.command(hci.OCF_READ_SIMPLE_PAIRING_MODE)[0]
    except (hci.HCIError, IndexError):
        state["ssp_mode"] = None

    try:
        state["auth_enable"] = sock.command(hci.OCF_READ_AUTHENTICATION_ENABLE)[0]
    except (hci.HCIError, IndexError):
        state["auth_enable"] = None

    try:
        raw = sock.command(hci.OCF_READ_DEFAULT_LINK_POLICY)
        state["default_link_policy"] = struct.unpack_from("<H", raw, 0)[0]
    except (hci.HCIError, struct.error):
        state["default_link_policy"] = None

    return state


#: Scan enable is a two-bit field, and the two bits are routinely confused. A
#: host that cannot *find* us and a host that cannot *connect* to us are
#: different faults with the same error message.
_SCAN_ENABLE = {
    0x00: "off (no inquiry scan, no page scan)",
    0x01: "inquiry scan only -- discoverable but cannot be connected to",
    0x02: "page scan only -- connectable, not discoverable",
    0x03: "inquiry + page scan -- discoverable and connectable",
}


def _describe_policy(bits: int | None) -> str:
    if bits is None:
        return "unreadable"
    names = [
        name
        for bit, name in (
            (hci.LP_ROLE_SWITCH, "role-switch"),
            (hci.LP_HOLD, "hold"),
            (hci.LP_SNIFF, "SNIFF"),
            (hci.LP_PARK, "park"),
        )
        if bits & bit
    ]
    return f"0x{bits:04X} ({', '.join(names) if names else 'none'})"


def probe_adapter(index: int, policy: LinkPolicy) -> None:
    """Print everything readable about one adapter and its live links."""
    print(f"\nhci{index}")
    print("-" * 60)

    sock = hci.HCISocket(index)
    try:
        sock.open()
    except hci.HCIError as exc:
        print(f"  {exc}")
        return

    try:
        state = _adapter_state(sock)

        scan = state["scan_enable"]
        print(f"  scan enable          {_SCAN_ENABLE.get(scan, scan)}")

        cod = state["class_of_device"]
        if cod is None:
            print("  class of device      unreadable")
        else:
            # Compare the **major and minor device class only**, not the whole
            # word. The upper bits are service-class flags that bluetoothd
            # toggles by itself -- Limited Discoverable in particular -- so
            # 0x000508 and 0x002508 are the same controller. Measured on the
            # reference Pi: four adapters, two reading each value at the same
            # instant, all four peripheral/gamepad and all four working.
            # Flagging that difference sends someone chasing a class-of-device
            # problem that is not there.
            major, minor = (cod >> 8) & 0x1F, (cod >> 2) & 0x3F
            ok = (major, minor) == (0x05, 0x02)
            note = "" if ok else "   <- NOT peripheral/gamepad (major 5, minor 2)"
            print(f"  class of device      0x{cod:06X}  major={major} minor={minor}{note}")
            if not ok:
                print(
                    "                       A console that filters its pairing list on "
                    "the class\n"
                    "                       will never offer us as a controller. Set it "
                    "in bluetoothd's\n"
                    "                       own config: /etc/bluetooth/main.conf -> "
                    "[General] Class"
                )

        ssp = state["ssp_mode"]
        print(f"  simple pairing       {'on' if ssp == 1 else 'OFF' if ssp == 0 else '?'}")
        if ssp == 0:
            print(
                "                       With SSP off the spec *requires* legacy PIN "
                "pairing."
            )

        auth = state["auth_enable"]
        print(f"  authentication       {'ON' if auth else 'off'}")
        if auth:
            print(
                "                       Authentication enabled forces the legacy flow "
                "and skips\n"
                "                       the SSP IO-capability exchange. It should be off."
            )

        print(f"  default link policy  {_describe_policy(state['default_link_policy'])}")

        connections = [c for c in list_connections(index) if c["type"] == "ACL"]
        if not connections:
            print("\n  no ACL connections")
            return

        for conn in connections:
            _probe_connection(sock, conn, policy)
    finally:
        sock.close()


def _probe_connection(sock: hci.HCISocket, conn: dict, policy: LinkPolicy) -> None:
    """Print one link's tuning, and flag anything that will cost latency."""
    handle = conn["handle"]
    direction = "outgoing" if conn["outgoing"] else "incoming"
    print(f"\n  link to {conn['peer']}  (handle 0x{handle:04X}, {direction})")

    # A connection handle is a 12-bit field, so 0x0EFF is the largest valid one.
    # A stale or wedged kernel entry appears here as an out-of-range value, and
    # every command against it then returns "unknown connection identifier" --
    # which reads as a tuning failure rather than as a connection that is not
    # really there. Observed on a Realtek USB dongle after a failed outgoing
    # connect; "sudo hciconfig hciX reset" clears it.
    if handle > 0x0EFF:
        print("    handle is outside the valid range (0x0000-0x0EFF): a stale")
        print("    kernel connection entry, not a live link.")
        print("    Clear it with: sudo hciconfig hciX reset")
        return

    link_policy = _u16_reply(sock, hci.OCF_READ_LINK_POLICY, handle)
    flush = _u16_reply(sock, hci.OCF_READ_AUTOMATIC_FLUSH_TIMEOUT, handle)
    supervision = _u16_reply(sock, hci.OCF_READ_LINK_SUPERVISION_TIMEOUT, handle)

    print(f"    link policy        {_describe_policy(link_policy)}")
    if link_policy is not None and link_policy & hci.LP_SNIFF:
        print(
            "                       Sniff is PERMITTED. Either end may park the link, "
            "and the\n"
            "                       first report after an idle gap then pays a full "
            "sniff exit."
        )

    if flush is None:
        print("    flush timeout      unreadable")
    elif flush == 0:
        print("    flush timeout      INFINITE (0)")
        print(
            "                       This is the controller default and the single "
            "largest source\n"
            "                       of tail latency: a packet caught in an "
            "interference burst is\n"
            "                       retransmitted until it succeeds, blocking every "
            "fresh report\n"
            "                       behind it."
        )
    else:
        want = hci.ms_to_slots(policy.flush_timeout_ms)
        mark = "" if flush == want else f"   (policy asks for {want})"
        print(f"    flush timeout      {hci.slots_to_ms(flush):.1f} ms{mark}")

    if supervision is None:
        print("    supervision        unreadable")
    else:
        print(f"    supervision        {hci.slots_to_ms(supervision):.0f} ms")

    print(
        "\n    Current sniff state is not readable over HCI. To see it, watch for "
        "Mode Change:\n"
        "      sudo btmon -T | grep -A2 'Mode Change'"
    )


def discover_indices() -> list[int]:
    """Which ``hciX`` devices exist, from sysfs.

    sysfs rather than ``hciconfig``: the binary is not always on a non-root
    PATH, which is precisely the situation this tool runs in.
    """
    from pathlib import Path

    root = Path("/sys/class/bluetooth")
    if not root.is_dir():
        return []

    indices = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if name.startswith("hci") and name[3:].isdigit():
            indices.append(int(name[3:]))
    return indices


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read live Bluetooth link parameters from the controller.",
        epilog="Needs CAP_NET_RAW; run with sudo.",
    )
    parser.add_argument(
        "-i", "--index", type=int, action="append",
        help="Adapter index to probe (repeatable). Default: every adapter present.",
    )
    args = parser.parse_args(argv)

    if not hci.is_supported() or fcntl is None:
        print(
            "This tool reads Bluetooth controllers over raw HCI, which is Linux "
            "only. Run it on the server."
        )
        return 1

    indices = args.index or discover_indices()
    if not indices:
        print("No Bluetooth adapters found under /sys/class/bluetooth.")
        return 1

    policy = LinkPolicy()
    print("Reading from the controller over HCI, not from MGMT or D-Bus.")
    print(
        f"Policy for comparison: sniff "
        f"{'allowed' if policy.allow_sniff else 'refused'}, "
        f"flush {policy.flush_timeout_ms:.0f} ms, "
        f"supervision {policy.supervision_timeout_ms:.0f} ms"
    )

    for index in indices:
        probe_adapter(index, policy)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
