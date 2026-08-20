"""Punching through a broker that sits behind a proxy.

The real `HolePuncher` on both sides, a real broker, a real STUN server, and a
UDP forwarder in between standing in for frp -- so the broker never observes an
address either peer can be reached at.

This is the end-to-end version of the argument: `test_broker_deployment.py`
shows the broker handing over the forwarder's address, and this shows two peers
connecting anyway because each discovered and reported its own.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from client.net.holepunch import HolePuncher, PunchResult
from common import stun
from tests.test_broker_deployment import BrokerHarness, UdpForwarder


class StunReflector:
    """A real RFC 5389 responder, directly reachable -- as it has to be."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.served = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.sock.getsockname()
        return f"{host}:{port}"

    def _serve(self) -> None:
        cookie = struct.pack("!I", stun.MAGIC_COOKIE)
        while not self._stop.is_set():
            try:
                data, source = self.sock.recvfrom(2048)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            if len(data) < 20 or data[4:8] != cookie:
                continue

            transaction = data[8:20]
            port = source[1] ^ (stun.MAGIC_COOKIE >> 16)
            address = bytes(
                a ^ b for a, b in zip(socket.inet_aton(source[0]), cookie)
            )
            value = struct.pack("!BBH", 0, 0x01, port) + address
            body = struct.pack("!HH", 0x0020, len(value)) + value
            header = struct.pack(
                "!HHI12s", 0x0101, len(body), stun.MAGIC_COOKIE, transaction
            )
            self.served += 1
            self.sock.sendto(header + body, source)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self.sock.close()


@pytest.fixture
def broker():
    harness = BrokerHarness()
    yield harness
    harness.stop()


@pytest.fixture
def reflector():
    server = StunReflector()
    yield server
    server.stop()


def _punch(sock, broker_addr, *, role, peer_role, stun_servers, results, key):
    outcome = HolePuncher(
        sock,
        broker_addr,
        "proxied-room",
        role=role,
        peer_role=peer_role,
        stun_servers=stun_servers,
    ).run()
    results[key] = outcome


def _run_pair(broker_addr, stun_servers):
    """Both peers punch at once, as they really do."""
    results: dict[str, object] = {}
    socks = []
    threads = []
    for key, role, peer_role in (
        ("server", "server", "client"),
        ("client", "client", "server"),
    ):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        socks.append(sock)
        threads.append(
            threading.Thread(
                target=_punch,
                args=(sock, broker_addr, ),
                kwargs=dict(
                    role=role, peer_role=peer_role,
                    stun_servers=stun_servers, results=results, key=key,
                ),
                daemon=True,
            )
        )

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=40)

    for sock in socks:
        sock.close()
    return results


class TestThroughAProxy:
    def test_two_peers_punch_despite_the_broker_seeing_the_proxy(
        self, broker, reflector
    ):
        """The whole point, end to end."""
        forwarder = UdpForwarder(broker.port)
        try:
            results = _run_pair(
                ("127.0.0.1", forwarder.port), [reflector.endpoint]
            )

            assert set(results) == {"server", "client"}, "a peer never finished"
            assert reflector.served >= 2, "STUN was not consulted by both peers"

            for key, outcome in results.items():
                assert outcome.ok, f"{key}: {outcome.describe()}"
                assert outcome.result is not PunchResult.FAILED, key
                assert outcome.result is not PunchResult.RELAY, (
                    f"{key} fell back to relay; the reported candidate was not used"
                )
        finally:
            forwarder.stop()

    def test_without_stun_the_same_setup_cannot_punch(self, broker):
        """Confirms the test above is measuring what it claims to.

        With no STUN server the only candidate is the one the broker observed,
        which behind a proxy is the proxy. This is the original bug.
        """
        forwarder = UdpForwarder(broker.port)
        try:
            results = _run_pair(("127.0.0.1", forwarder.port), [])

            punched = [
                key
                for key, outcome in results.items()
                if outcome.result is PunchResult.PUNCHED
            ]
            assert not punched, (
                "punching succeeded without a reported candidate, so the "
                "forwarder is not actually hiding the peers from each other"
            )
        finally:
            forwarder.stop()


class TestDirectlyReachableIsUnchanged:
    def test_the_common_path_still_punches(self, broker, reflector):
        """No proxy: `public` and `address` agree and nothing behaves differently."""
        results = _run_pair(("127.0.0.1", broker.port), [reflector.endpoint])

        assert set(results) == {"server", "client"}
        for key, outcome in results.items():
            assert outcome.ok, f"{key}: {outcome.describe()}"
            assert outcome.result is not PunchResult.RELAY, key

    def test_it_works_with_stun_disabled_too(self, broker):
        """Empty `stun_servers` is a supported configuration, not a broken one."""
        results = _run_pair(("127.0.0.1", broker.port), [])

        assert set(results) == {"server", "client"}
        for key, outcome in results.items():
            assert outcome.ok, f"{key}: {outcome.describe()}"
