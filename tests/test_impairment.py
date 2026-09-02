"""The impairment relay, tested as an instrument.

Phases 8, 9 and 23 are all measured *through* this thing, so a fault in it
would not look like a fault in it -- it would look like a result. These tests
exist to make it trustworthy before anything is concluded with it: asking for
2% loss must produce about 2% loss, asking for 20 ms of delay must produce
about 20 ms, and asking for nothing must be transparent.
"""

from __future__ import annotations

import socket
import time

import pytest

from tools.impair import ImpairedRelay, Impairment, _TokenBucket


class Echo:
    """A server that sends every datagram straight back."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self._stop = False
        import threading

        self.received = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def address(self):
        return self._sock.getsockname()

    def _run(self) -> None:
        while not self._stop:
            try:
                data, source = self._sock.recvfrom(65535)
            except (TimeoutError, socket.timeout, OSError):
                continue
            self.received += 1
            try:
                self._sock.sendto(data, source)
            except OSError:
                pass

    def stop(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def echo():
    server = Echo()
    yield server
    server.stop()


def _client():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(1.0)
    return sock


def _blast(sock, address, count: int, size: int = 64, gap_s: float = 0.001) -> int:
    """Send `count` datagrams, then count how many came back."""
    payload = bytes(size)
    for index in range(count):
        sock.sendto(index.to_bytes(4, "little") + payload, address)
        time.sleep(gap_s)

    back = 0
    deadline = time.monotonic() + 2.0
    sock.settimeout(0.25)
    while time.monotonic() < deadline:
        try:
            sock.recvfrom(65535)
        except (TimeoutError, socket.timeout):
            break
        except OSError:
            break
        back += 1
    return back


class TestACleanPathIsTransparent:
    def test_everything_gets_through(self, echo):
        relay = ImpairedRelay(echo.address, seed=1)
        relay.start()
        try:
            sock = _client()
            back = _blast(sock, relay.address, 200)
            sock.close()
        finally:
            relay.stop()

        assert back == 200, "a clean relay must not lose anything"
        counts = relay.snapshot()["to_server"]
        assert counts["dropped_loss"] == 0
        assert counts["dropped_queue"] == 0

    def test_it_adds_no_delay_it_was_not_asked_for(self, echo):
        relay = ImpairedRelay(echo.address, seed=1)
        relay.start()
        try:
            sock = _client()
            sock.sendto(b"x" * 64, relay.address)
            started = time.perf_counter()
            sock.recvfrom(65535)
            elapsed_ms = (time.perf_counter() - started) * 1000
            sock.close()
        finally:
            relay.stop()
        assert elapsed_ms < 50, f"round trip took {elapsed_ms:.1f} ms on loopback"


class TestLossIsWhatWasAskedFor:
    def test_the_measured_rate_matches_the_request(self, echo):
        # One direction only, so the arithmetic is unambiguous.
        relay = ImpairedRelay(
            echo.address, to_server=Impairment(loss=0.20), seed=7
        )
        relay.start()
        try:
            sock = _client()
            back = _blast(sock, relay.address, 500)
            sock.close()
        finally:
            relay.stop()

        counts = relay.snapshot()["to_server"]
        assert counts["offered"] == 500
        # 20% of 500 with a fixed seed: comfortably inside 5 points.
        assert 15.0 <= counts["loss_pct"] <= 25.0
        assert abs(back - counts["forwarded"]) <= 5

    def test_total_loss_delivers_nothing(self, echo):
        relay = ImpairedRelay(echo.address, to_server=Impairment(loss=1.0), seed=3)
        relay.start()
        try:
            sock = _client()
            back = _blast(sock, relay.address, 50)
            sock.close()
        finally:
            relay.stop()
        assert back == 0
        assert echo.received == 0


class TestDelayAndJitter:
    def test_a_fixed_delay_is_applied_each_way(self, echo):
        relay = ImpairedRelay(
            echo.address,
            to_server=Impairment(delay_ms=25.0),
            to_client=Impairment(delay_ms=25.0),
            seed=1,
        )
        relay.start()
        try:
            sock = _client()
            sock.sendto(b"x" * 64, relay.address)
            started = time.perf_counter()
            sock.recvfrom(65535)
            elapsed_ms = (time.perf_counter() - started) * 1000
            sock.close()
        finally:
            relay.stop()
        # 25 ms each way; generous upper bound for scheduler noise.
        assert 40 <= elapsed_ms <= 120, f"round trip {elapsed_ms:.1f} ms"

    def test_jitter_produces_a_spread_not_a_constant(self, echo):
        relay = ImpairedRelay(
            echo.address, to_server=Impairment(jitter_ms=30.0), seed=11
        )
        relay.start()
        try:
            sock = _client()
            _blast(sock, relay.address, 200)
            sock.close()
        finally:
            relay.stop()

        counts = relay.snapshot()["to_server"]
        mean = counts["mean_delay_ms"]
        # Uniform on [0, 30) has a mean near 15.
        assert 8.0 <= mean <= 22.0, f"mean applied delay {mean:.1f} ms"

    def test_nothing_is_lost_merely_by_being_delayed(self, echo):
        relay = ImpairedRelay(
            echo.address, to_server=Impairment(delay_ms=5.0, jitter_ms=10.0), seed=5
        )
        relay.start()
        try:
            sock = _client()
            back = _blast(sock, relay.address, 200)
            sock.close()
        finally:
            relay.stop()
        assert back == 200


class TestTheBandwidthCapQueuesRatherThanShreds:
    """A rate limiter that only drops is a loss generator wearing a hat.

    The queueing is the whole point: congestion control has to be able to see
    delay grow before loss starts, which is exactly what Phase 8 is about.
    """

    def test_a_small_burst_is_delayed_not_dropped(self):
        bucket = _TokenBucket(rate_kbps=1000, burst_bytes=8192)
        # Drain the bucket, then ask for more.
        assert bucket.take(8192) == 0.0
        wait = bucket.take(4096)
        assert wait is not None and wait > 0, "should have been queued, not dropped"

    def test_something_far_beyond_the_queue_is_dropped(self):
        bucket = _TokenBucket(rate_kbps=1000, burst_bytes=4096)
        bucket.take(4096)
        assert bucket.take(100_000) is None, "an overfull queue must drop"

    def test_the_cap_holds_roughly_to_the_rate(self, echo):
        relay = ImpairedRelay(
            echo.address, to_server=Impairment(rate_kbps=800, burst_bytes=4096), seed=2
        )
        relay.start()
        try:
            sock = _client()
            started = time.perf_counter()
            _blast(sock, relay.address, 300, size=1000, gap_s=0.0005)
            elapsed = time.perf_counter() - started
            sock.close()
        finally:
            relay.stop()

        counts = relay.snapshot()["to_server"]
        kbps = counts["bytes_forwarded"] * 8 / 1000 / max(elapsed, 0.001)
        # Generous: the point is that it is bounded near the request, not that
        # it is exact -- the burst allowance and the run length both smear it.
        assert kbps < 800 * 3, f"forwarded {kbps:.0f} kbps against an 800 cap"


class TestItReportsWhatItDid:
    """The counters are what a test asserts on, so they must be complete."""

    def test_offered_accounts_for_every_datagram(self, echo):
        relay = ImpairedRelay(echo.address, to_server=Impairment(loss=0.3), seed=9)
        relay.start()
        try:
            sock = _client()
            _blast(sock, relay.address, 300)
            sock.close()
        finally:
            relay.stop()

        counts = relay.snapshot()["to_server"]
        assert (
            counts["forwarded"] + counts["dropped_loss"] + counts["dropped_queue"]
            == counts["offered"] == 300
        )

    def test_both_directions_are_counted_separately(self, echo):
        relay = ImpairedRelay(
            echo.address,
            to_server=Impairment(),
            to_client=Impairment(loss=1.0),      # nothing comes back
            seed=4,
        )
        relay.start()
        try:
            sock = _client()
            back = _blast(sock, relay.address, 100)
            sock.close()
        finally:
            relay.stop()

        snap = relay.snapshot()
        assert back == 0, "the return path was fully lossy"
        assert snap["to_server"]["forwarded"] == 100, "the forward path was clean"
        assert snap["to_client"]["dropped_loss"] > 0

    def test_a_seed_makes_a_run_repeatable(self, echo):
        results = []
        for _ in range(2):
            relay = ImpairedRelay(echo.address, to_server=Impairment(loss=0.25), seed=42)
            relay.start()
            try:
                sock = _client()
                _blast(sock, relay.address, 200)
                sock.close()
            finally:
                relay.stop()
            results.append(relay.snapshot()["to_server"]["dropped_loss"])
        assert results[0] == results[1], "a seeded run must reproduce"


class TestEverySettingIsLive:
    """Impairments must be changeable mid-run, or a scenario measures nothing.

    `loss` and `jitter` are read per datagram, so they always worked. The
    bandwidth cap was baked into a token bucket built once at construction, so
    turning congestion on halfway through a scenario silently did nothing --
    and the scenario measured a clean path and reported it as a pass.
    """

    def test_the_rate_cap_can_be_turned_on_after_construction(self, echo):
        impairment = Impairment()                      # starts uncapped
        relay = ImpairedRelay(echo.address, to_server=impairment, seed=6)
        relay.start()
        try:
            sock = _client()
            _blast(sock, relay.address, 60, size=1000, gap_s=0.0005)
            uncapped = relay.snapshot()["to_server"]["dropped_queue"]

            impairment.rate_kbps = 100                 # brutally low, mid-run
            impairment.burst_bytes = 2048
            _blast(sock, relay.address, 200, size=1000, gap_s=0.0005)
            capped = relay.snapshot()["to_server"]["dropped_queue"]
            sock.close()
        finally:
            relay.stop()

        assert uncapped == 0, "no cap was set for the first blast"
        assert capped > 0, "the cap set mid-run was ignored"

    def test_the_rate_cap_can_be_turned_off_again(self, echo):
        impairment = Impairment(rate_kbps=100, burst_bytes=2048)
        relay = ImpairedRelay(echo.address, to_server=impairment, seed=6)
        relay.start()
        try:
            sock = _client()
            _blast(sock, relay.address, 150, size=1000, gap_s=0.0005)
            assert relay.snapshot()["to_server"]["dropped_queue"] > 0

            impairment.rate_kbps = 0.0
            before = relay.snapshot()["to_server"]["dropped_queue"]
            _blast(sock, relay.address, 150, size=1000, gap_s=0.0005)
            after = relay.snapshot()["to_server"]["dropped_queue"]
            sock.close()
        finally:
            relay.stop()
        assert after == before, "the cap was not lifted"
