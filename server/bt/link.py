"""Per-connection link tuning: the part of the latency budget we actually own.

We are the **peripheral**. In Bluetooth Classic the central schedules the ACL
slots, so we do not choose the poll rate and no amount of application tuning
changes it. What we *do* choose is everything that decides how badly the link
degrades away from that rate, and none of it has a BlueZ interface:

============================  =====================================================
Automatic flush timeout       BlueZ default is **infinite**. A packet caught in a
                              2.4 GHz interference burst is retransmitted by the
                              baseband until it succeeds, blocking the channel
                              head-of-line the whole time. Every fresh report
                              queues behind a stale one nobody wants any more.
                              This is the mechanism behind the tail latency.
Link policy (sniff)           With sniff permitted, either end may park the link
                              when it looks idle. Waking it costs a full sniff-exit
                              negotiation -- the ~70 ms first-report-after-idle
                              cost this project had written off as unavoidable.
Supervision timeout           How long a dead link looks alive. The 20 s default
                              means a console that was switched off holds the
                              channel for twenty seconds before reconnect starts.
============================  =====================================================

Flushing only helps if the packets are flushable
------------------------------------------------
A flush timeout applies to **flushable** ACL packets only; non-flushable ones
are retransmitted regardless. Linux sends L2CAP data non-flushable unless the
socket asks otherwise, so :data:`BT_FLUSHABLE` on the interrupt socket and the
flush timeout on the connection are a **pair** -- either alone does nothing, and
the one that does nothing silently is the socket option. Both are applied
together in ``server/bt/hid.py``; neither should be removed without the other.

Flushing is only safe because the reports are self-healing
----------------------------------------------------------
Discarding a report means the console never learns about that state change.
With send-on-change alone that is a stuck button, which is far worse than the
jitter we set out to fix. It is safe here because the sink also re-sends the
current state at :data:`LinkPolicy.keepalive_hz` -- so a flushed report costs
one keepalive period, not the rest of the session. **A tight flush timeout and
the keepalive are one design, not two features.** Raising the flush timeout or
removing the keepalive independently reintroduces the bug.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from server.bt import hci

log = logging.getLogger(__name__)

#: ``getsockopt`` coordinates for the ACL handle behind an L2CAP socket.
#: ``struct l2cap_conninfo`` is ``__u16 hci_handle; __u8 dev_class[3]``.
SOL_L2CAP = 6
L2CAP_CONNINFO = 0x02
_CONNINFO_SIZE = 8

#: ``SOL_BLUETOOTH`` and the socket option that makes our ACL packets eligible
#: for the flush timeout. Python's socket module names neither.
SOL_BLUETOOTH = 274
BT_FLUSHABLE = 8
BT_FLUSHABLE_OFF = 0
BT_FLUSHABLE_ON = 1


@dataclass(frozen=True, slots=True)
class LinkPolicy:
    """The tuning applied to every HID connection.

    Defaults are chosen for a mains-powered server driving a gamepad across one
    room. They are deliberately *not* general-purpose Bluetooth defaults: they
    trade power and range headroom for latency, which is the correct trade here
    and the wrong one almost everywhere else.
    """

    #: Let the peer take the central role. Kept **on**: when we page the console
    #: to reconnect we start as central, and a console schedules its own
    #: peripherals better than it schedules a stranger. The switch happens at
    #: connection setup, not mid-session.
    allow_role_switch: bool = True

    #: Permit sniff mode. **Off** is the whole point of this module -- see the
    #: module docstring. Costs power, which is irrelevant on a Pi.
    allow_sniff: bool = False

    #: Discard a queued report after this long. Roughly three poll intervals at
    #: a typical HID rate: long enough that an ordinary retransmission still
    #: succeeds, short enough that a stale report never blocks a fresh one.
    #: Safe only in combination with ``keepalive_hz`` (see the module docstring).
    flush_timeout_ms: float = 30.0

    #: Declare the link dead after this long with no reply. The 20 s controller
    #: default is far too slow to start a reconnect against; much below a
    #: second and an ordinary interference burst starts killing healthy links.
    supervision_timeout_ms: float = 5000.0

    #: How long to page a host before giving up. The 5.12 s default makes each
    #: failed reconnect attempt expensive on a radio four adapters are sharing.
    page_timeout_ms: float = 2560.0

    #: Re-send the current state at this rate even when nothing has changed.
    #: This is what makes a flushed report self-healing, and it keeps the link
    #: warm so the peer has no idle period to park. Real controllers stream
    #: continuously at a fixed rate for the same reasons.
    keepalive_hz: float = 50.0

    def link_policy_bits(self) -> int:
        """The value for Write Link Policy Settings."""
        bits = 0
        if self.allow_role_switch:
            bits |= hci.LP_ROLE_SWITCH
        if self.allow_sniff:
            bits |= hci.LP_SNIFF
        # Hold and park are never wanted: both suspend the link for longer than
        # sniff does, and nothing in this design has any use for either.
        return bits

    @property
    def keepalive_interval_s(self) -> float:
        return 1.0 / self.keepalive_hz if self.keepalive_hz > 0 else 0.0


#: Status codes that are a *normal outcome* for a command rather than a fault.
#: Measured on a Pi 5 (BlueZ 5.82, kernel 6.18) against a live ACL link.
#:
#: Without this every healthy link logs a warning naming two commands that
#: could never have succeeded, which is worse than saying nothing: it trains
#: whoever reads the log to ignore a line that is also used for real faults.
_BENIGN_STATUS = {
    "exit-sniff": {
        # There is no sniff to exit. Sending it unconditionally is still right
        # -- the link may be parked when we arrive, and there is no way to ask
        # which mode it is in -- so "disallowed" is the ordinary answer on a
        # link that was already active, not a problem.
        0x0C: "link is already in active mode",
    },
    "supervision-timeout": {
        # Write Link Supervision Timeout is the **central's** to set. When a
        # console connects to us we are the peripheral, so this is refused by
        # specification and the timeout stays at whatever the console chose --
        # 20 s by default, which is how long a vanished console holds the
        # channel before our reconnect can start. We do get to set it on links
        # we initiate, where we are central.
        0x0C: "peripheral role: the central owns the supervision timeout",
    },
}


#: What a link looked like after tuning. Reported to the operator and to the
#: probe tool, because "we sent the command" is not the same as "the controller
#: took it" -- several of this project's worst bugs were exactly that gap.
@dataclass(slots=True)
class LinkReport:
    handle: int = 0
    link_policy: int | None = None
    flush_timeout_slots: int | None = None
    supervision_timeout_slots: int | None = None
    applied: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    #: ``(command, why)`` for commands the controller declined for a reason
    #: that is expected in this role. Deliberately not merged with ``failed``:
    #: one of them means something is wrong and the other does not.
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def sniff_allowed(self) -> bool | None:
        if self.link_policy is None:
            return None
        return bool(self.link_policy & hci.LP_SNIFF)

    def snapshot(self) -> dict[str, object]:
        return {
            "handle": self.handle,
            "link_policy": self.link_policy,
            "sniff_allowed": self.sniff_allowed,
            "flush_timeout_ms": (
                round(hci.slots_to_ms(self.flush_timeout_slots), 2)
                if self.flush_timeout_slots
                else None
            ),
            "supervision_timeout_ms": (
                round(hci.slots_to_ms(self.supervision_timeout_slots), 1)
                if self.supervision_timeout_slots
                else None
            ),
            "applied": list(self.applied),
            "failed": list(self.failed),
            "skipped": {name: why for name, why in self.skipped},
        }


def acl_handle_for(sock) -> int | None:
    """Read the ACL connection handle behind a connected L2CAP socket.

    Every HCI command here is addressed to a *connection handle*, and the
    obvious ways to find one -- enumerating connections over an ioctl, or
    matching by peer address -- are both racy when four adapters are connecting
    and dropping independently. ``L2CAP_CONNINFO`` answers for exactly the
    socket in hand, which is the connection we are about to tune.

    Returns None rather than raising: a link that dropped between accept and
    tuning is ordinary, and it must not take down the session.
    """
    try:
        raw = sock.getsockopt(SOL_L2CAP, L2CAP_CONNINFO, _CONNINFO_SIZE)
    except (OSError, AttributeError):
        return None
    if len(raw) < 2:
        return None
    handle = struct.unpack_from("<H", raw, 0)[0]
    # 0 is not a valid ACL handle; the kernel returns it for a socket that is
    # not actually connected.
    return handle or None


def set_flushable(sock, flushable: bool = True) -> bool:
    """Mark this socket's ACL packets flushable. Half of the flush-timeout pair.

    Without this the flush timeout is inert -- non-flushable packets are
    retransmitted until they succeed no matter what timeout the connection
    carries. That failure is completely silent: the command succeeds, the
    read-back looks right, and the tail latency does not move.
    """
    try:
        sock.setsockopt(
            SOL_BLUETOOTH,
            BT_FLUSHABLE,
            BT_FLUSHABLE_ON if flushable else BT_FLUSHABLE_OFF,
        )
        return True
    except OSError as exc:
        log.warning(
            "Could not mark the interrupt channel flushable (%s). The automatic "
            "flush timeout will have no effect and stale reports will block "
            "fresh ones under interference.",
            exc,
        )
        return False


class LinkTuner:
    """Applies a :class:`LinkPolicy` to one adapter and its live connections.

    Holds an :class:`~server.bt.hci.HCISocket` for the adapter. Every method is
    best-effort and non-fatal: a controller that refuses one of these commands
    still carries a perfectly usable HID link, just with worse tail latency, and
    trading a working controller for a tuning nicety would be the wrong call.
    Failures are reported rather than raised so the operator can see which
    adapter is untuned.
    """

    def __init__(self, index: int, policy: LinkPolicy | None = None) -> None:
        self.index = index
        self.policy = policy or LinkPolicy()
        self._hci = hci.HCISocket(index)

    def open(self) -> bool:
        try:
            self._hci.open()
            return True
        except hci.HCIError as exc:
            log.warning(
                "No HCI channel on hci%d (%s). Links will use BlueZ defaults: "
                "an infinite flush timeout and sniff mode permitted, so expect "
                "latency spikes under interference and a slow first report "
                "after an idle gap.",
                self.index,
                exc,
            )
            return False

    def close(self) -> None:
        self._hci.close()

    @property
    def is_open(self) -> bool:
        return self._hci.is_open

    # -- adapter-wide ------------------------------------------------------

    def apply_adapter_defaults(self) -> None:
        """Set the policy new connections inherit, before any of them exist.

        Write Default Link Policy Settings is what the controller stamps onto
        each new ACL link. Setting it means an incoming connection is already
        correct at the instant it comes up, rather than spending its first
        moments sniff-capable until we get round to tuning it -- and a host that
        requests sniff immediately on connect would otherwise win that race.

        The per-connection write in :meth:`tune` is still required: this default
        is not retroactive, and a controller may reset it when the adapter is
        cycled.
        """
        if not self._hci.is_open:
            return

        bits = self.policy.link_policy_bits()
        try:
            self._hci.command(
                hci.OCF_WRITE_DEFAULT_LINK_POLICY, struct.pack("<H", bits)
            )
            log.debug("hci%d default link policy set to 0x%04X", self.index, bits)
        except hci.HCIError as exc:
            log.debug("hci%d default link policy not set: %s", self.index, exc)

        try:
            slots = hci.ms_to_slots(self.policy.page_timeout_ms)
            self._hci.command(hci.OCF_WRITE_PAGE_TIMEOUT, struct.pack("<H", slots))
        except hci.HCIError as exc:
            log.debug("hci%d page timeout not set: %s", self.index, exc)

    def ensure_adapter_defaults(self) -> bool:
        """Re-assert the controller default if something has reset it.

        **A controller reset silently discards it.** ``hciconfig hciX reset``,
        a USB re-enumeration, or a firmware reload puts the default link policy
        back to whatever the dongle ships with -- ``0x000F`` on the Realtek
        USB-BT500, meaning hold, sniff and park all permitted -- and nothing
        tells anyone. Observed live: an adapter reset to clear an unrelated
        stale connection came back sniff-capable while its three siblings
        stayed correct, and only a read showed it.

        The per-connection tuning in :meth:`tune` still fixes each link as it
        comes up, so the damage is bounded, but the default exists to close the
        window *before* that: a host that requests sniff immediately on connect
        would otherwise win the race.

        Read-then-write, so the ordinary case costs one command and writes
        nothing. Returns True if a correction was needed.
        """
        if not self._hci.is_open:
            return False

        want = self.policy.link_policy_bits()
        try:
            reply = self._hci.command(hci.OCF_READ_DEFAULT_LINK_POLICY)
            current = struct.unpack_from("<H", reply, 0)[0]
        except (hci.HCIError, struct.error):
            return False

        if current == want:
            return False

        log.warning(
            "hci%d default link policy was 0x%04X, not 0x%04X -- something reset "
            "the controller. Re-applying; new links would otherwise permit %s.",
            self.index, current, want,
            "sniff" if current & hci.LP_SNIFF else "power-save modes",
        )
        self.apply_adapter_defaults()
        return True

    # -- per connection ----------------------------------------------------

    def tune(self, handle: int) -> LinkReport:
        """Apply the whole policy set to one live connection.

        Called on **every** connection, incoming and outgoing alike, because
        these are per-connection settings: they are re-derived from the
        controller defaults each time a link comes up, so a reconnect that
        skipped this would silently run untuned.
        """
        report = LinkReport(handle=handle)
        if not self._hci.is_open:
            report.failed = ("hci-channel",)
            return report

        applied: list[str] = []
        failed: list[str] = []
        skipped: list[tuple[str, str]] = []

        for name, opcode, params in self._commands_for(handle):
            try:
                self._hci.command(opcode, params)
                applied.append(name)
            except hci.HCIStatusError as exc:
                reason = _BENIGN_STATUS.get(name, {}).get(exc.status)
                if reason is not None:
                    skipped.append((name, reason))
                    log.debug("hci%d %s on 0x%04X: %s", self.index, name, handle, reason)
                else:
                    failed.append(name)
                    log.debug(
                        "hci%d could not set %s on handle 0x%04X: %s",
                        self.index, name, handle, exc,
                    )
            except hci.HCIError as exc:
                failed.append(name)
                log.debug(
                    "hci%d could not set %s on handle 0x%04X: %s",
                    self.index, name, handle, exc,
                )

        report.applied = tuple(applied)
        report.failed = tuple(failed)
        report.skipped = tuple(skipped)

        # Read back rather than trusting the writes. Several of this project's
        # longest debugging sessions came from a write that reported success and
        # did nothing, so the honest answer is the one the controller gives.
        self._read_into(report, handle)

        if failed:
            log.warning(
                "hci%d handle 0x%04X: could not apply %s. The link works but its "
                "tail latency will be worse than the tuned adapters.",
                self.index, handle, ", ".join(failed),
            )

        log.info(
            "hci%d handle 0x%04X tuned: sniff %s, flush %.1f ms, supervision %.0f ms%s",
            self.index,
            handle,
            "allowed" if report.sniff_allowed else "refused",
            hci.slots_to_ms(report.flush_timeout_slots or 0),
            hci.slots_to_ms(report.supervision_timeout_slots or 0),
            f" ({report.skipped[0][1]})" if report.skipped else "",
        )
        return report

    def _commands_for(self, handle: int) -> list[tuple[str, int, bytes]]:
        """The command list for one connection, in the order it must be issued.

        Sniff is exited **before** the policy is written. Clearing the sniff bit
        stops the link entering sniff again, but it does not pull it out of a
        sniff it is already in -- a link tuned while parked would stay parked
        until something else woke it, which is precisely the case that hurts.
        """
        policy = self.policy
        flush = min(
            hci.ms_to_slots(policy.flush_timeout_ms), hci.MAX_FLUSH_TIMEOUT_SLOTS
        )
        supervision = max(
            hci.MIN_SUPERVISION_SLOTS,
            min(
                hci.ms_to_slots(policy.supervision_timeout_ms),
                hci.MAX_SUPERVISION_SLOTS,
            ),
        )

        commands: list[tuple[str, int, bytes]] = []
        if not policy.allow_sniff:
            commands.append(
                ("exit-sniff", hci.OCF_EXIT_SNIFF_MODE, struct.pack("<H", handle))
            )
        commands += [
            (
                "link-policy",
                hci.OCF_WRITE_LINK_POLICY,
                struct.pack("<HH", handle, policy.link_policy_bits()),
            ),
            (
                "flush-timeout",
                hci.OCF_WRITE_AUTOMATIC_FLUSH_TIMEOUT,
                struct.pack("<HH", handle, flush),
            ),
            (
                "supervision-timeout",
                hci.OCF_WRITE_LINK_SUPERVISION_TIMEOUT,
                struct.pack("<HH", handle, supervision),
            ),
        ]
        return commands

    def _read_into(self, report: LinkReport, handle: int) -> None:
        """Fill a report from what the controller says the link actually is."""
        report.link_policy = self._read_u16(hci.OCF_READ_LINK_POLICY, handle)
        report.flush_timeout_slots = self._read_u16(
            hci.OCF_READ_AUTOMATIC_FLUSH_TIMEOUT, handle
        )
        report.supervision_timeout_slots = self._read_u16(
            hci.OCF_READ_LINK_SUPERVISION_TIMEOUT, handle
        )

    def _read_u16(self, opcode: int, handle: int) -> int | None:
        """Read a per-connection u16. Every one of these replies is handle+value."""
        try:
            reply = self._hci.command(opcode, struct.pack("<H", handle))
        except hci.HCIError:
            return None
        if len(reply) < 4:
            return None
        return struct.unpack_from("<H", reply, 2)[0]


class LEPingTuner:
    """Stops the kernel hanging up on an idle-but-healthy LE link.

    Bluetooth Classic and BLE share almost none of the tuning in
    :class:`LinkTuner` -- there is no sniff mode, no flush timeout and no link
    policy on an LE connection -- so this is deliberately a separate, much
    smaller thing rather than a branch inside it.

    The one lever that matters on LE is the **Authenticated Payload Timeout**.
    Its default of 30 s assumes both ends talk. A gamepad does not: it notifies
    the console and the console answers nothing, so no authenticated packet ever
    arrives from the peer, the controller raises `Authenticated Payload Timeout
    Expired`, and the kernel disconnects a link on which reports were flowing
    perfectly.

    Measured against an Analogue 3D: connect, encrypt, subscribe, receive
    reports, hang up 30 s later, repeat -- six times in three minutes, with the
    console showing a controller that did nothing. Every counter on our side
    reported a healthy connection the whole time, because from our side it *was*
    one.

    Like every other tuning step in this module, failure is logged and never
    fatal: an untuned link still carries HID reports, it just gets torn down
    periodically, which is strictly better than refusing to serve at all.
    """

    def __init__(self, index: int, *, timeout_ms: int | None = None) -> None:
        self.index = index
        self._timeout = (
            hci.MAX_AUTH_PAYLOAD_TIMEOUT
            if timeout_ms is None
            else max(1, min(hci.MAX_AUTH_PAYLOAD_TIMEOUT, int(timeout_ms / 10)))
        )

    def ensure_peer(self, bd_addr: str) -> bool:
        """Re-assert the timeout only if the kernel has put its own back.

        Read-then-write, so the steady state costs one command and changes
        nothing. Called from the reconcile pass because the kernel rewrites
        this value on every encryption change and there is no event to hang a
        one-shot correction on.

        Returns True if a write was needed and succeeded.
        """
        handle = hci.handle_for_address(self.index, bd_addr)
        if handle is None:
            return False

        try:
            with hci.HCISocket(self.index) as sock:
                reply = sock.command(
                    hci.OCF_READ_AUTHENTICATED_PAYLOAD_TIMEOUT,
                    struct.pack("<H", handle),
                )
                # Reply is connection_handle(2) then timeout(2).
                if len(reply) >= 4:
                    current = struct.unpack_from("<H", reply, 2)[0]
                    if current == self._timeout:
                        return False
                    log.info(
                        "LE ping timeout on hci%d was %.0f s, putting it back "
                        "to %.0f s -- the kernel resets this on every "
                        "encryption change",
                        self.index, current * 0.01, self._timeout * 0.01,
                    )
                sock.command(
                    hci.OCF_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT,
                    struct.pack("<HH", handle, self._timeout),
                )
        except hci.HCIError as exc:
            log.debug(
                "Could not re-assert the LE ping timeout on hci%d: %s",
                self.index, exc,
            )
            return False
        return True

    def tune_peer(self, bd_addr: str) -> bool:
        """Extend the APTO on the live link to ``bd_addr``. True if applied."""
        handle = hci.handle_for_address(self.index, bd_addr)
        if handle is None:
            log.debug(
                "No live connection to %s on hci%d to tune", bd_addr, self.index
            )
            return False
        return self.tune(handle)

    def tune(self, handle: int) -> bool:
        try:
            with hci.HCISocket(self.index) as sock:
                sock.command(
                    hci.OCF_WRITE_AUTHENTICATED_PAYLOAD_TIMEOUT,
                    struct.pack("<HH", handle, self._timeout),
                )
        except hci.HCIError as exc:
            # Worth a warning rather than a debug line: "untuned" and "fine"
            # are indistinguishable until the link starts dropping every 30 s,
            # and by then nobody connects it to a silent failure at startup.
            log.warning(
                "Could not extend the LE ping timeout on hci%d handle 0x%04x "
                "(%s). The link will work, but a console that never transmits "
                "may be disconnected roughly every 30 s.",
                self.index, handle, exc,
            )
            return False

        log.info(
            "LE ping timeout on hci%d handle 0x%04x set to %.0f s",
            self.index, handle, self._timeout * 0.01,
        )
        return True
