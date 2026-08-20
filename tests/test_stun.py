"""Learning our own public address, so the broker need not observe it.

A peer cannot know its own NAT mapping from the inside, which is the whole
reason the broker observed it -- and the whole reason the broker could not sit
behind a proxy. STUN moves that observation to a service that *is* directly
reachable, so the address can be reported rather than inferred.

Everything here runs against locally built messages and an in-process responder.
No test touches the Internet: a test that needs a public STUN server is a test
that does not run in CI, and would fail for reasons that have nothing to do with
this code.
"""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from common import stun


def _success_response(
    transaction_id: bytes,
    ip: str = "203.0.113.7",
    port: int = 54321,
    *,
    xor: bool = True,
    extra_attribute: bytes = b"",
) -> bytes:
    """Build a Binding Success Response the way a real server would."""
    raw = socket.inet_aton(ip)
    if xor:
        encoded_port = port ^ (stun.MAGIC_COOKIE >> 16)
        cookie = struct.pack("!I", stun.MAGIC_COOKIE)
        encoded_ip = bytes(a ^ b for a, b in zip(raw, cookie))
        attr_type = 0x0020
    else:
        encoded_port = port
        encoded_ip = raw
        attr_type = 0x0001

    value = struct.pack("!BBH", 0, 0x01, encoded_port) + encoded_ip
    body = extra_attribute + struct.pack("!HH", attr_type, len(value)) + value
    header = struct.pack(
        "!HHI12s", 0x0101, len(body), stun.MAGIC_COOKIE, transaction_id
    )
    return header + body


class TestParsingAResponse:
    def test_the_xor_form_is_decoded(self):
        _request, transaction_id = stun.binding_request()

        found = stun.parse_response(
            _success_response(transaction_id, "198.51.100.9", 40000), transaction_id
        )

        assert found == ("198.51.100.9", 40000)

    def test_the_plain_form_still_works(self):
        """Kept for servers old enough not to send the XOR variant."""
        _request, transaction_id = stun.binding_request()

        found = stun.parse_response(
            _success_response(transaction_id, "203.0.113.7", 1234, xor=False),
            transaction_id,
        )

        assert found == ("203.0.113.7", 1234)

    def test_the_xor_form_wins_when_both_are_present(self):
        """A NAT that rewrites addresses in payloads mangles the plain one."""
        _request, transaction_id = stun.binding_request()

        plain_value = struct.pack("!BBH", 0, 0x01, 9999) + socket.inet_aton("10.0.0.1")
        plain = struct.pack("!HH", 0x0001, len(plain_value)) + plain_value

        message = _success_response(
            transaction_id, "203.0.113.7", 4444, extra_attribute=plain
        )

        assert stun.parse_response(message, transaction_id) == ("203.0.113.7", 4444)

    def test_a_reply_to_a_different_transaction_is_refused(self):
        """The socket is shared; a stray or forged reply must not be believed."""
        _request, mine = stun.binding_request()
        _other_request, theirs = stun.binding_request()

        assert stun.parse_response(_success_response(theirs), mine) is None

    def test_attribute_padding_is_walked_correctly(self):
        """An odd-sized attribute desynchronises everything after it."""
        _request, transaction_id = stun.binding_request()

        # SOFTWARE, 5 bytes, padded to 8. Real servers send exactly this.
        odd = struct.pack("!HH", 0x8022, 5) + b"abcde" + b"\x00" * 3
        message = _success_response(
            transaction_id, "203.0.113.7", 4444, extra_attribute=odd
        )

        assert stun.parse_response(message, transaction_id) == ("203.0.113.7", 4444)

    def test_a_truncated_attribute_is_refused_rather_than_guessed(self):
        _request, transaction_id = stun.binding_request()
        message = _success_response(transaction_id)

        assert stun.parse_response(message[:-4], transaction_id) is None

    def test_junk_is_not_mistaken_for_a_response(self):
        _request, transaction_id = stun.binding_request()
        for datagram in (b"", b"{}", b"\x00" * 40, b"not a stun message at all"):
            assert stun.parse_response(datagram, transaction_id) is None

    def test_ipv6_is_ignored_rather_than_half_parsed(self):
        """A peer with routable IPv6 does not need punching, and a candidate
        the other side cannot route to is worse than none."""
        _request, transaction_id = stun.binding_request()
        value = struct.pack("!BBH", 0, 0x02, 1234) + b"\x00" * 16
        body = struct.pack("!HH", 0x0020, len(value)) + value
        header = struct.pack(
            "!HHI12s", 0x0101, len(body), stun.MAGIC_COOKIE, transaction_id
        )

        assert stun.parse_response(header + body, transaction_id) is None


class TestRecognisingOneOnASharedSocket:
    """The server has one socket for gameplay, signalling and this."""

    def test_a_response_is_recognised(self):
        _request, transaction_id = stun.binding_request()
        assert stun.is_stun_response(_success_response(transaction_id)) is True

    def test_our_own_traffic_is_not(self):
        from common import protocol

        for datagram in (
            protocol.PUNCH_PROBE,
            protocol.PUNCH_ACK_PROBE,
            b'{"op": "registered"}',
            bytes([protocol.PacketType.SESSION]) + b"\x00" * 40,
        ):
            assert stun.is_stun_response(datagram) is False, datagram[:8]

    def test_something_cookie_shaped_but_short_is_not(self):
        assert stun.is_stun_response(b"\x00\x01\x00\x00" + b"\x21\x12\xa4\x42") is False


class FakeStunServer:
    """An in-process responder, so no test reaches the Internet."""

    def __init__(self, *, silent: bool = False) -> None:
        self.silent = silent
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.requests = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.sock.getsockname()
        return f"{host}:{port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, source = self.sock.recvfrom(2048)
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            self.requests += 1
            if self.silent:
                continue
            transaction_id = data[8:20]
            # Report the sender's real address, exactly as a STUN server does.
            self.sock.sendto(
                _success_response(transaction_id, source[0], source[1]), source
            )

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        self.sock.close()


@pytest.fixture
def stun_server():
    server = FakeStunServer()
    yield server
    server.stop()


class TestDiscovery:
    def test_it_reports_the_socket_the_request_went_out_on(self, stun_server):
        """The mapping belongs to *this* socket, which is the whole point."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        try:
            found = stun.discover(sock, [stun_server.endpoint])
            assert found == sock.getsockname()
        finally:
            sock.close()

    def test_a_silent_server_fails_soft(self):
        """Discovery is best-effort: no candidate, not an exception."""
        silent = FakeStunServer(silent=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        try:
            assert stun.discover(sock, [silent.endpoint], timeout_s=0.3) is None
            assert silent.requests > 0, "it never even asked"
        finally:
            sock.close()
            silent.stop()

    def test_it_moves_on_to_the_next_server(self, stun_server):
        silent = FakeStunServer(silent=True)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        try:
            found = stun.discover(
                sock, [silent.endpoint, stun_server.endpoint], timeout_s=0.3
            )
            assert found == sock.getsockname()
        finally:
            sock.close()
            silent.stop()

    def test_no_servers_configured_means_no_discovery(self):
        """The setting for anyone unwilling to involve a third party."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            assert stun.discover(sock, []) is None
        finally:
            sock.close()

    def test_an_unresolvable_name_is_skipped_not_fatal(self, stun_server):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        try:
            found = stun.discover(
                sock,
                ["no-such-host.invalid:3478", stun_server.endpoint],
                timeout_s=0.3,
            )
            assert found == sock.getsockname()
        finally:
            sock.close()

    def test_the_sockets_timeout_is_left_as_it_was_found(self, stun_server):
        """The caller owns the socket; discovery borrows it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(4.25)
        try:
            stun.discover(sock, [stun_server.endpoint])
            assert sock.gettimeout() == pytest.approx(4.25)
        finally:
            sock.close()

    def test_other_traffic_on_the_socket_does_not_break_it(self, stun_server):
        """Something else may well arrive first on a shared socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            noise.sendto(b"unrelated datagram", sock.getsockname())
            found = stun.discover(sock, [stun_server.endpoint], timeout_s=1.0)
            assert found == sock.getsockname()
        finally:
            sock.close()
            noise.close()
