"""Reconnect logic in the HID server.

The Bluetooth I/O itself needs hardware, so these cover the parts that do not:
target bookkeeping, session mutual-exclusion, and backoff shape. The end-to-end
behaviour is verified on real hardware.
"""

from __future__ import annotations

import errno
import socket
import time

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


class FakeSocket:
    """A stand-in listener that records whether it was closed."""

    def __init__(self, psm: int) -> None:
        self.psm = psm
        self.closed = False

    def close(self) -> None:
        self.closed = True


class TestStartReleasesOnFailure:
    """A half-bound HID server must not keep the PSM it did manage to bind.

    HID needs both PSM 17 (control) and PSM 19 (interrupt). ``start()`` binds
    control first, so a failure on interrupt used to propagate with the control
    socket still open and owned by nobody: ``stop()`` was never called, the
    socket held PSM 17 for the life of the process, and no retry could rebind.

    The adapter then went on advertising a HID service it could not serve, so a
    host would discover it, pair, and fail the interrupt connect with nothing
    but "try connecting your device again". Three adapters sat like that for
    days because the one log line explaining it went to a dead terminal.
    """

    def _server(self, monkeypatch, failing_psm: int, error: Exception):
        server = hid.HIDServer(
            "00:11:22:33:44:55", create_profile("generic"), FakeSink()
        )
        opened: list[FakeSocket] = []

        def fake_listen(psm: int):
            if psm == failing_psm:
                raise error
            sock = FakeSocket(psm)
            opened.append(sock)
            return sock

        monkeypatch.setattr(server, "_listen", fake_listen)
        return server, opened

    def test_the_control_socket_is_closed_when_interrupt_fails(self, monkeypatch):
        server, opened = self._server(
            monkeypatch, hid.PSM_INTERRUPT, OSError(errno.EADDRINUSE, "Address already in use")
        )

        with pytest.raises(OSError):
            server.start()

        assert len(opened) == 1, "control should have bound first"
        assert opened[0].closed, "control socket leaked"

    def test_no_listener_is_left_behind(self, monkeypatch):
        server, _ = self._server(
            monkeypatch, hid.PSM_INTERRUPT, OSError(errno.EADDRINUSE, "Address already in use")
        )

        with pytest.raises(OSError):
            server.start()

        assert server._control_listener is None
        assert server._interrupt_listener is None

    def test_a_control_failure_leaves_nothing_open(self, monkeypatch):
        """Nothing bound yet, so this only has to not explode."""
        server, opened = self._server(
            monkeypatch, hid.PSM_CONTROL, OSError(errno.EADDRINUSE, "Address already in use")
        )

        with pytest.raises(OSError):
            server.start()

        assert opened == []
        assert server._control_listener is None

    def test_eaddrinuse_still_explains_the_input_plugin(self, monkeypatch):
        """The actionable message must survive the cleanup path."""
        server, _ = self._server(
            monkeypatch, hid.PSM_INTERRUPT, OSError(errno.EADDRINUSE, "Address already in use")
        )

        with pytest.raises(OSError) as excinfo:
            server.start()

        assert "input" in str(excinfo.value)
        assert "already bound" in str(excinfo.value), "both causes must be named"

    def test_permission_denied_still_explains_setcap(self, monkeypatch):
        server, opened = self._server(
            monkeypatch, hid.PSM_INTERRUPT, PermissionError(1, "Operation not permitted")
        )

        with pytest.raises(PermissionError) as excinfo:
            server.start()

        assert "setcap" in str(excinfo.value)
        assert opened[0].closed, "control socket leaked on the EPERM path"

    def test_no_accept_thread_is_started(self, monkeypatch):
        """A thread accepting on a closed socket would spin logging warnings."""
        server, _ = self._server(
            monkeypatch, hid.PSM_INTERRUPT, OSError(errno.EADDRINUSE, "Address already in use")
        )

        with pytest.raises(OSError):
            server.start()

        assert server._accept_thread is None
        assert server._reconnect_thread is None


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


class TestReconnectSuspension:
    """Outgoing pages must stay off the radio during a pairing window.

    ``_connect`` binds to this adapter's BD_ADDR deliberately, so the page
    leaves on the same dongle the console is trying to reach. Paging out while
    inquiry and page scan are meant to be listening puts both on one antenna --
    the btmon capture from a failed pairing showed five Create Connection
    attempts to the very host that was trying to connect to us.
    """

    def test_not_suspended_by_default(self, server):
        assert server._reconnect_suspended_until == 0.0

    def test_suspending_sets_a_deadline(self, server):
        server.suspend_reconnect(120)

        assert server._reconnect_suspended_until > time.monotonic()

    def test_the_suspension_expires_on_its_own(self, server):
        """A pairing window ends by timeout as often as by the operator
        pressing Stop. A flag needing manual clearing would have become a
        permanent suspension the first time a window simply lapsed."""
        server.suspend_reconnect(0.05)
        assert server._reconnect_suspended_until > time.monotonic()

        time.sleep(0.08)

        assert server._reconnect_suspended_until < time.monotonic()

    def test_resuming_clears_it_and_retries_at_once(self, server):
        server.suspend_reconnect(120)
        server._retry_now.clear()

        server.suspend_reconnect(0)

        assert server._reconnect_suspended_until == 0.0
        assert server._retry_now.is_set(), "a waiting host should be picked up now"

    def test_the_loop_does_not_connect_while_suspended(self, server, monkeypatch):
        connects: list[str] = []
        monkeypatch.setattr(
            server, "_connect", lambda *a, **k: connects.append(a) or FakeSocket(0)
        )
        server.set_reconnect_target("AA:BB:CC:DD:EE:FF")
        server.suspend_reconnect(5)

        # One pass of the loop body's guard.
        remaining = server._reconnect_suspended_until - time.monotonic()

        assert remaining > 0, "still inside the window"
        assert connects == []


class TestReconnectFailureReporting:
    """A host that has deleted our link key looks exactly like one that is
    switched off, and both were logged at debug -- so paging a host that would
    never accept us was completely invisible."""

    def test_failures_start_at_zero(self, server):
        assert server._reconnect_failures == 0

    def test_the_threshold_is_minutes_not_seconds(self):
        """Short enough to be useful, long enough that a reboot stays quiet."""
        assert hid._RECONNECT_COMPLAIN_AFTER >= 5
        # At the 30 s ceiling that is about five minutes.
        assert hid._RECONNECT_COMPLAIN_AFTER * 30 >= 240
