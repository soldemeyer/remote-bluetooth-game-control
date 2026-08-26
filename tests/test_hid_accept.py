"""Pairing up the two HID channels as they arrive.

A Bluetooth HID link is two L2CAP connections -- control on PSM 17, interrupt on
PSM 19 -- and it is only up once both exist. The old implementation accepted
control and then blocked on the interrupt listener, which produced two failures,
both silent:

* A host that opened control and went away **wedged the adapter forever**.
  Nothing else could connect on it for the life of the process and no log line
  said so.
* The interrupt peer address was discarded, so a *second* host opening interrupt
  while the first was mid-connect got spliced onto the first host's control
  channel -- one host's buttons arriving on another host's link.

Both are answered by never blocking on an accept: sockets are filed under the
peer that opened them, and a session starts only when one peer has supplied
both.
"""

from __future__ import annotations

import pytest

hid = pytest.importorskip(
    "server.bt.hid",
    reason="AF_BLUETOOTH sockets are Linux-only",
)

from server.bt.profiles import create_profile  # noqa: E402
from server.bt.sdp import PSM_CONTROL, PSM_INTERRUPT  # noqa: E402

HOST_A = "11:22:33:44:55:66"
HOST_B = "AA:AA:AA:AA:AA:AA"


class FakeSink:
    def __init__(self) -> None:
        self.connected = False

    @property
    def is_connected(self) -> bool:
        return self.connected

    def attach(self, control, interrupt, peer) -> None:
        self.connected = True

    def detach(self) -> None:
        self.connected = False


class FakeChannel:
    """A socket handed back by ``accept()``."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def __repr__(self) -> str:
        return f"<FakeChannel {self.name}>"


class FakeListener:
    """A listener with a queue of connections waiting to be accepted."""

    def __init__(self, psm: int) -> None:
        self.psm = psm
        self.pending: list[tuple[FakeChannel, tuple[str, int]]] = []

    def queue(self, peer: str, name: str) -> FakeChannel:
        channel = FakeChannel(name)
        self.pending.append((channel, (peer, self.psm)))
        return channel

    def accept(self):
        if not self.pending:
            raise BlockingIOError("nothing queued")
        return self.pending.pop(0)


@pytest.fixture
def server():
    return hid.HIDServer("00:11:22:33:44:55", create_profile("generic"), FakeSink())


@pytest.fixture
def listeners():
    return FakeListener(PSM_CONTROL), FakeListener(PSM_INTERRUPT)


class TestPairingByPeer:
    def test_one_host_supplying_both_channels_completes(self, server, listeners):
        control_listener, interrupt_listener = listeners
        control = control_listener.queue(HOST_A, "a-control")
        interrupt = interrupt_listener.queue(HOST_A, "a-interrupt")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        assert server._take_complete(half) is None, "control alone must not complete"

        server._accept_one(interrupt_listener, PSM_INTERRUPT, half)
        ready = server._take_complete(half)

        assert ready is not None
        peer, pair = ready
        assert peer == HOST_A.upper()
        assert pair.control is control
        assert pair.interrupt is interrupt
        assert half == {}, "a completed pair must leave the pending set"

    def test_a_second_host_cannot_complete_the_first_host_s_link(self, server, listeners):
        """The bug that let one host's buttons arrive on another host's link."""
        control_listener, interrupt_listener = listeners
        control_listener.queue(HOST_A, "a-control")
        interrupt_listener.queue(HOST_B, "b-interrupt")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        server._accept_one(interrupt_listener, PSM_INTERRUPT, half)

        assert server._take_complete(half) is None
        assert set(half) == {HOST_A.upper(), HOST_B.upper()}

    def test_two_hosts_each_completing_are_served_one_at_a_time(self, server, listeners):
        control_listener, interrupt_listener = listeners
        for host in (HOST_A, HOST_B):
            control_listener.queue(host, f"{host}-control")
            interrupt_listener.queue(host, f"{host}-interrupt")

        half: dict = {}
        for _ in range(2):
            server._accept_one(control_listener, PSM_CONTROL, half)
            server._accept_one(interrupt_listener, PSM_INTERRUPT, half)

        first = server._take_complete(half)
        second = server._take_complete(half)
        assert first is not None and second is not None
        assert {first[0], second[0]} == {HOST_A.upper(), HOST_B.upper()}
        assert server._take_complete(half) is None

    def test_peer_addresses_are_normalised(self, server, listeners):
        """A host that appears in lower case is the same host.

        Everything else in this codebase keys on an upper-case BD_ADDR, so a
        stray case difference would file the two channels under different peers
        and the link would never complete.
        """
        control_listener, interrupt_listener = listeners
        control_listener.queue(HOST_A.lower(), "a-control")
        interrupt_listener.queue(HOST_A.upper(), "a-interrupt")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        server._accept_one(interrupt_listener, PSM_INTERRUPT, half)

        assert server._take_complete(half) is not None

    def test_a_spurious_readiness_is_harmless(self, server, listeners):
        """``select`` can report a listener readable with nothing queued."""
        control_listener, _ = listeners
        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        assert half == {}

    def test_reopening_a_channel_replaces_and_closes_the_old_one(self, server, listeners):
        control_listener, _ = listeners
        first = control_listener.queue(HOST_A, "first")
        second = control_listener.queue(HOST_A, "second")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        server._accept_one(control_listener, PSM_CONTROL, half)

        assert half[HOST_A.upper()].control is second
        assert first.closed, "the abandoned socket must not be leaked"


class TestHalfOpenExpiry:
    """The fix for the wedge: an orphan is closed, not waited on forever."""

    def test_an_orphaned_channel_is_closed_once_its_deadline_passes(self, server, listeners):
        control_listener, _ = listeners
        control = control_listener.queue(HOST_A, "a-control")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)

        server._expire_half_open(half)
        assert half, "must not expire before the deadline"
        assert not control.closed

        half[HOST_A.upper()].deadline = 0.0
        server._expire_half_open(half)

        assert half == {}
        assert control.closed

    def test_expiry_does_not_touch_a_pair_that_completed(self, server, listeners):
        control_listener, interrupt_listener = listeners
        control = control_listener.queue(HOST_A, "a-control")
        interrupt = interrupt_listener.queue(HOST_A, "a-interrupt")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        server._accept_one(interrupt_listener, PSM_INTERRUPT, half)
        server._take_complete(half)

        server._expire_half_open(half)
        assert not control.closed and not interrupt.closed

    def test_one_peer_expiring_leaves_another_alone(self, server, listeners):
        control_listener, _ = listeners
        stale = control_listener.queue(HOST_A, "stale")
        fresh = control_listener.queue(HOST_B, "fresh")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)
        server._accept_one(control_listener, PSM_CONTROL, half)

        half[HOST_A.upper()].deadline = 0.0
        server._expire_half_open(half)

        assert set(half) == {HOST_B.upper()}
        assert stale.closed and not fresh.closed

    def test_the_deadline_is_bounded(self, server, listeners):
        """However generous, it must exist. Unbounded is the original bug."""
        control_listener, _ = listeners
        control_listener.queue(HOST_A, "a-control")

        half: dict = {}
        server._accept_one(control_listener, PSM_CONTROL, half)

        assert half[HOST_A.upper()].deadline < float("inf")
        assert hid._HALF_OPEN_TIMEOUT_S > 0
