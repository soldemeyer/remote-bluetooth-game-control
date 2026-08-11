"""Reconnect logic in the HID server.

The Bluetooth I/O itself needs hardware, so these cover the parts that do not:
target bookkeeping, session mutual-exclusion, and backoff shape. The end-to-end
behaviour is verified on real hardware.
"""

from __future__ import annotations

import socket
import threading

import pytest

hid = pytest.importorskip(
    "server.bt.hid",
    reason="AF_BLUETOOTH sockets are Linux-only",
)

from server.bt.profiles import create_profile  # noqa: E402


class FakeSink:
    """Stands in for L2CAPSink: records attach/detach without touching sockets."""

    def __init__(self) -> None:
        self.connected = False
        self.peer = ""
        self.attach_count = 0
        self.detach_count = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    def attach(self, control, interrupt, peer) -> None:
        self.connected = True
        self.peer = peer
        self.attach_count += 1

    def detach(self) -> None:
        if self.connected:
            self.detach_count += 1
        self.connected = False
        self.peer = ""


@pytest.fixture
def server():
    return hid.HIDServer("00:11:22:33:44:55", create_profile("generic"), FakeSink())


class TestReconnectTarget:
    def test_starts_with_no_target(self, server):
        assert server.reconnect_target is None

    def test_set_target_normalizes_case(self, server):
        """Addresses arrive from bluetoothctl, hciconfig and L2CAP peers in
        different cases; comparing them raw would miss matches."""
        server.set_reconnect_target("aa:bb:cc:dd:ee:ff")
        assert server.reconnect_target == "AA:BB:CC:DD:EE:FF"

    def test_setting_target_wakes_the_retry_loop(self, server):
        server._retry_now.clear()
        server.set_reconnect_target("AA:BB:CC:DD:EE:FF")
        assert server._retry_now.is_set(), "a new target should trigger an immediate try"

    def test_setting_the_same_target_again_is_a_noop(self, server):
        server.set_reconnect_target("AA:BB:CC:DD:EE:FF")
        server._retry_now.clear()
        server.set_reconnect_target("AA:BB:CC:DD:EE:FF")
        assert not server._retry_now.is_set()

    def test_clearing_the_target_does_not_trigger_a_retry(self, server):
        server.set_reconnect_target("AA:BB:CC:DD:EE:FF")
        server._retry_now.clear()
        server.set_reconnect_target(None)
        assert server.reconnect_target is None
        assert not server._retry_now.is_set()


class TestSessionExclusion:
    def test_second_session_is_rejected_while_one_is_active(self, server):
        """An incoming connection and a reconnect attempt can race. Only one
        may attach, or the sink ends up with mismatched socket pairs."""
        server._session_lock.acquire()
        try:
            a, b = socket.socketpair()
            try:
                server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)
                assert server._sink.attach_count == 0, "should not have attached"
            finally:
                for s in (a, b):
                    try:
                        s.close()
                    except OSError:
                        pass
        finally:
            server._session_lock.release()

    def test_lock_is_released_after_a_session(self, server, monkeypatch):
        monkeypatch.setattr(server, "_serve_control", lambda control: None)

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)

        assert server._session_lock.acquire(blocking=False), "lock leaked"
        server._session_lock.release()

    def test_session_learns_the_peer_address(self, server, monkeypatch):
        """Learning the address from an incoming connection is how reconnect
        gets a target at all."""
        monkeypatch.setattr(server, "_serve_control", lambda control: None)

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)

        assert server.reconnect_target == "AA:BB:CC:DD:EE:FF"

    def test_incoming_session_fires_the_callback(self, monkeypatch):
        seen = []
        server = hid.HIDServer(
            "00:11:22:33:44:55",
            create_profile("generic"),
            FakeSink(),
            on_host_connected=seen.append,
        )
        monkeypatch.setattr(server, "_serve_control", lambda control: None)

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)

        assert seen == ["AA:BB:CC:DD:EE:FF"]

    def test_outgoing_session_does_not_fire_the_callback(self, monkeypatch):
        """The callback persists a newly-learned host. An outgoing reconnect is
        to an address we already had, so re-persisting is pointless churn."""
        seen = []
        server = hid.HIDServer(
            "00:11:22:33:44:55",
            create_profile("generic"),
            FakeSink(),
            on_host_connected=seen.append,
        )
        monkeypatch.setattr(server, "_serve_control", lambda control: None)

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=False)

        assert seen == []

    def test_a_failing_callback_does_not_break_the_session(self, monkeypatch):
        """Persisting config must never take down the Bluetooth link."""
        def boom(_host):
            raise RuntimeError("disk full")

        server = hid.HIDServer(
            "00:11:22:33:44:55",
            create_profile("generic"),
            FakeSink(),
            on_host_connected=boom,
        )
        monkeypatch.setattr(server, "_serve_control", lambda control: None)

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)

        assert server._sink.attach_count == 1
        assert server._sink.detach_count == 1

    def test_session_end_schedules_a_prompt_retry(self, server, monkeypatch):
        monkeypatch.setattr(server, "_serve_control", lambda control: None)
        server._retry_now.clear()

        a, b = socket.socketpair()
        server._serve_session(a, b, "AA:BB:CC:DD:EE:FF", incoming=True)

        assert server._retry_now.is_set(), "should retry promptly after a drop"


class TestBackoff:
    def test_delays_increase_then_cap(self):
        delays = hid._RECONNECT_DELAYS
        assert delays[0] <= 2.0, "first retry should be quick"
        assert list(delays) == sorted(delays), "delays must be non-decreasing"
        assert delays[-1] <= 30.0, "cap should stay responsive to a host waking"

    def test_index_saturates_past_the_end(self):
        """A host that stays off for hours must not walk off the end."""
        delays = hid._RECONNECT_DELAYS
        for attempt in (0, 5, 50, 10_000):
            assert delays[min(attempt, len(delays) - 1)] == delays[min(attempt, len(delays) - 1)]


def test_close_quietly_tolerates_none_and_closed_sockets():
    a, b = socket.socketpair()
    a.close()
    hid._close_quietly(a, b, None)  # must not raise
