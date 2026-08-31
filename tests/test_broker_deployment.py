"""What the broker must be able to observe, and what a proxy takes away.

The broker exists to tell two NATed peers each other's **public UDP source
address**, so they can punch a hole and then talk directly. Every part of it
turns on the address it sees on the wire: registration records it, an
introduction hands it over, and the relay table is keyed on it.

That makes the deployment topology a correctness question rather than an ops
preference, and one worth pinning, because the failure is silent. Put an L4
proxy, an frp UDP tunnel, or Docker's userland proxy in front and every peer
appears to arrive from somewhere else: the punch targets the proxy, fails, and
every session quietly falls back to relay. Nothing errors. It just gets slow.

The second class stands a forwarder in front of a real broker and shows exactly
that, so the constraint in packaging/docker/README.md is demonstrated rather
than merely asserted.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from rendezvous.broker import BrokerProtocol


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class BrokerHarness:
    """A real broker on a real socket, on its own event loop thread."""

    host = "127.0.0.1"

    def __init__(self) -> None:
        self.port = _free_port()
        #: The live protocol object, so a test can read its state directly.
        self.protocol: BrokerProtocol | None = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=10), "broker did not start"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self.protocol = BrokerProtocol()
        transport, _ = self._loop.run_until_complete(
            self._loop.create_datagram_endpoint(
                lambda: self.protocol, local_addr=(self.host, self.port)
            )
        )
        self._transport = transport
        self._ready.set()
        self._loop.run_forever()

    def wait_for(self, condition, timeout: float = 5.0) -> None:
        """Poll until the broker's own loop has caught up.

        Signalling that the broker acts on asynchronously -- ``bye`` closing an
        allocation, for instance -- is not finished when ``sendto`` returns.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.02)
        raise AssertionError("the broker never reached the expected state")

    def stop(self) -> None:
        if self.protocol is not None:
            self._loop.call_soon_threadsafe(self.protocol.close)
        self._loop.call_soon_threadsafe(self._transport.close)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class Peer:
    """A peer with its own socket, as a real one has."""

    def __init__(self, broker_port: int, via_port: int | None = None) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.sock.bind(("127.0.0.1", 0))
        self.target = ("127.0.0.1", via_port or broker_port)

    @property
    def address(self) -> tuple[str, int]:
        return self.sock.getsockname()

    def send(self, message: dict) -> None:
        self.sock.sendto(json.dumps(message).encode(), self.target)

    def receive(self, op: str, timeout: float = 5.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.sock.settimeout(max(deadline - time.time(), 0.1))
            try:
                data, _ = self.sock.recvfrom(65535)
            except socket.timeout:
                break
            message = json.loads(data.decode())
            if message.get("op") == op:
                return message
        raise AssertionError("no " + op + " message arrived")

    def drain(self, timeout: float = 0.3) -> None:
        """Discard whatever signalling is queued.

        Introductions are re-sent on every registration, so a test with several
        peers has a backlog that would be read instead of the payload it is
        actually waiting for.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(deadline - time.monotonic(), 0.01))
            try:
                self.sock.recvfrom(65535)
            except (socket.timeout, OSError):
                return
        self.sock.settimeout(5.0)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def broker():
    harness = BrokerHarness()
    yield harness
    harness.stop()


class TestTheBrokerReportsWhatItSees:
    """Delivered directly, the addresses handed over are the peers' own."""

    def test_each_peer_is_told_the_others_real_address(self, broker):
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            server.send({"op": "register", "room": "abc", "role": "server"})
            client.send({"op": "register", "room": "abc", "role": "client"})

            to_server = server.receive("peer")
            to_client = client.receive("peer")

            assert tuple(to_server["address"]) == client.address, (
                "the server was handed something other than the client's socket"
            )
            assert tuple(to_client["address"]) == server.address, (
                "the client was handed something other than the server's socket"
            )
        finally:
            server.close()
            client.close()

    def test_two_clients_are_told_apart(self, broker):
        """They differ only by source port, which is the whole mechanism."""
        server = Peer(broker.port)
        first = Peer(broker.port)
        second = Peer(broker.port)
        try:
            server.send({"op": "register", "room": "abc", "role": "server"})
            first.send({"op": "register", "room": "abc", "role": "client"})
            first.receive("peer")
            second.send({"op": "register", "room": "abc", "role": "client"})

            assert tuple(second.receive("peer")["address"]) == server.address
            assert first.address != second.address
        finally:
            for peer in (server, first, second):
                peer.close()


class UdpForwarder:
    """A minimal L4 UDP proxy -- frp, nginx stream and docker-proxy alike.

    Re-originates each datagram from its own socket, which is the one thing
    they all do and the one thing the broker cannot survive.
    """

    def __init__(self, broker_port: int) -> None:
        self.port = _free_port()
        self._broker = ("127.0.0.1", broker_port)
        self._front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._front.bind(("127.0.0.1", self.port))
        self._front.settimeout(0.2)
        self._back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bound explicitly, or getsockname() reports 0.0.0.0 until the first
        # send and the comparison below tests nothing.
        self._back.bind(("127.0.0.1", 0))
        self._back.settimeout(0.2)
        self._client: tuple[str, int] | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    @property
    def back_address(self) -> tuple[str, int]:
        return self._back.getsockname()

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self._front.recvfrom(65535)
                self._client = source
                self._back.sendto(data, self._broker)
            except (socket.timeout, OSError):
                pass
            try:
                data, _ = self._back.recvfrom(65535)
                if self._client:
                    self._front.sendto(data, self._client)
            except (socket.timeout, OSError):
                pass

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self._front.close()
        self._back.close()


class TestAProxyDestroysTheObservation:
    """Why packaging/docker/README.md says not to put one in front."""

    def test_the_broker_learns_the_proxy_not_the_peer(self, broker):
        forwarder = UdpForwarder(broker.port)
        server = Peer(broker.port)
        client = Peer(broker.port, via_port=forwarder.port)
        try:
            server.send({"op": "register", "room": "xyz", "role": "server"})
            client.send({"op": "register", "room": "xyz", "role": "client"})

            handed_to_server = tuple(server.receive("peer")["address"])

            assert handed_to_server != client.address, (
                "this forwarder preserved the source address, so it is not "
                "modelling a proxy and the rest of this proves nothing"
            )
            assert handed_to_server == forwarder.back_address, (
                "the server was handed the forwarder's address -- punching at "
                "that reaches the forwarder, never the client"
            )
        finally:
            forwarder.stop()
            server.close()
            client.close()


class TestASelfReportedAddressSurvivesTheProxy:
    """The fix for the class above.

    A peer cannot know its own NAT mapping from the inside, so it asks a STUN
    server -- which *is* directly reachable -- and reports the answer. The
    broker passes that candidate through without needing to understand or
    verify it, and stops needing to observe anything at all.
    """

    def test_the_reported_candidate_is_handed_over(self, broker):
        forwarder = UdpForwarder(broker.port)
        server = Peer(broker.port)
        client = Peer(broker.port, via_port=forwarder.port)
        try:
            server.send({"op": "register", "room": "xyz", "role": "server"})
            client.send(
                {
                    "op": "register",
                    "room": "xyz",
                    "role": "client",
                    # What STUN told it. Here it is simply its real address,
                    # which is what a STUN server would have reported.
                    "public": list(client.address),
                }
            )

            introduction = server.receive("peer")

            assert tuple(introduction["address"]) == forwarder.back_address, (
                "the observed address should still be the forwarder's"
            )
            assert tuple(introduction["public"]) == client.address, (
                "the self-reported candidate did not survive; punching behind "
                "a proxy has nothing reachable to aim at"
            )
        finally:
            forwarder.stop()
            server.close()
            client.close()

    def test_it_is_absent_rather_than_wrong_when_stun_fails(self, broker):
        """Discovery is best effort; no candidate must not break anything."""
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            server.send({"op": "register", "room": "abc", "role": "server"})
            client.send({"op": "register", "room": "abc", "role": "client"})

            introduction = server.receive("peer")

            assert introduction["public"] is None
            assert tuple(introduction["address"]) == client.address
        finally:
            server.close()
            client.close()

    def test_a_malformed_candidate_is_ignored(self, broker):
        """Self-reported and unverifiable, so it has to be bounded."""
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            server.send({"op": "register", "room": "abc", "role": "server"})
            client.send(
                {
                    "op": "register",
                    "room": "abc",
                    "role": "client",
                    "public": ["not-an-address", 99999],
                }
            )

            assert server.receive("peer")["public"] is None
        finally:
            server.close()
            client.close()

    def test_the_observed_address_is_still_sent_alongside(self, broker):
        """Kept as a candidate rather than replaced.

        A peer can assert anything, so the address we saw ourselves stays in
        the list -- it is the one the broker can actually vouch for.
        """
        server = Peer(broker.port)
        client = Peer(broker.port)
        try:
            server.send({"op": "register", "room": "abc", "role": "server"})
            client.send(
                {
                    "op": "register",
                    "room": "abc",
                    "role": "client",
                    "public": ["198.51.100.9", 40000],
                }
            )

            introduction = server.receive("peer")
            assert tuple(introduction["address"]) == client.address
            assert tuple(introduction["public"]) == ("198.51.100.9", 40000)
        finally:
            server.close()
            client.close()
