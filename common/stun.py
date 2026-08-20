"""Minimal STUN client (RFC 5389), for learning our own public address.

A peer behind NAT cannot know the address the world sees it at. Its socket
knows only the LAN address; the public ``IP:port`` is invented by the NAT and is
visible only from outside. STUN is the standard way to ask.

That matters here because the rendezvous broker's original design *observed*
each peer's source address and handed it to the other peer. Anything that
re-originates the datagram on the way -- an L4 proxy, an frp tunnel, Docker's
userland proxy -- destroys the observation. Learning the address ourselves and
reporting it turns the broker into pure signalling, which can live anywhere.

**The binding request must go out on the socket that will carry gameplay.** A
NAT mapping belongs to one specific local port, so discovering on a scratch
socket learns a mapping nobody will ever use. This is the same trap
``client/net/holepunch.py`` documents for the punch itself, for the same reason.

Stdlib only, and deliberately small: this parses one response type and two
attributes. It is not a general STUN implementation and should not grow into
one -- anything more belongs in a library, and we do not need one.
"""

from __future__ import annotations

import logging
import os
import socket
import struct

log = logging.getLogger(__name__)

#: Every STUN message carries this at bytes 4:8. It is what lets a receiver
#: tell a STUN response from anything else sharing the socket.
MAGIC_COOKIE = 0x2112A442
_COOKIE_BYTES = struct.pack("!I", MAGIC_COOKIE)

_BINDING_REQUEST = 0x0001
_BINDING_SUCCESS = 0x0101

_ATTR_MAPPED_ADDRESS = 0x0001
_ATTR_XOR_MAPPED_ADDRESS = 0x0020

_FAMILY_IPV4 = 0x01

_HEADER = struct.Struct("!HHI12s")
assert _HEADER.size == 20

TRANSACTION_ID_BYTES = 12

#: One request, one short wait. A STUN server that has not answered in this long
#: is not worth holding a connection attempt open for -- discovery fails soft and
#: the caller carries on with the candidates it already has.
DEFAULT_TIMEOUT_S = 1.0


def binding_request() -> tuple[bytes, bytes]:
    """Return ``(datagram, transaction_id)`` for a new Binding Request."""
    transaction_id = os.urandom(TRANSACTION_ID_BYTES)
    return (
        _HEADER.pack(_BINDING_REQUEST, 0, MAGIC_COOKIE, transaction_id),
        transaction_id,
    )


def is_stun_response(data: bytes) -> bool:
    """Cheap test for "is this datagram a STUN response?".

    Used where the socket carries other traffic: the server shares one socket
    between gameplay, broker signalling and this. Two conditions, both from the
    RFC -- the top two bits of a STUN message are zero (which no packet type we
    use starts with), and the magic cookie sits at a fixed offset.
    """
    return (
        len(data) >= _HEADER.size
        and (data[0] & 0xC0) == 0
        and data[4:8] == _COOKIE_BYTES
    )


def parse_response(data: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """Extract the reflexive address from a Binding Success Response.

    Returns None for anything that is not a success response to *this*
    transaction. Checking the transaction ID is what stops a stray or forged
    datagram being mistaken for our answer -- the socket is shared, and an
    attacker who could guess the reply would otherwise choose the address we
    advertise to our peer.
    """
    if not is_stun_response(data):
        return None

    message_type, length, _cookie, echoed = _HEADER.unpack_from(data, 0)
    if message_type != _BINDING_SUCCESS or echoed != transaction_id:
        return None
    if length + _HEADER.size > len(data):
        return None

    # XOR-MAPPED-ADDRESS is preferred and taken whichever order they arrive in:
    # some NATs rewrite anything that looks like an address in the payload, and
    # obfuscating it is the entire reason the XOR variant exists. The plain form
    # is kept only for servers old enough not to send the XOR one.
    plain: tuple[str, int] | None = None

    offset = _HEADER.size
    end = _HEADER.size + length
    while offset + 4 <= end:
        attr_type, attr_length = struct.unpack_from("!HH", data, offset)
        value_at = offset + 4
        if value_at + attr_length > len(data):
            return None
        value = data[value_at : value_at + attr_length]

        if attr_type == _ATTR_XOR_MAPPED_ADDRESS:
            found = _parse_address(value, xor=True)
            if found is not None:
                return found
        elif attr_type == _ATTR_MAPPED_ADDRESS and plain is None:
            plain = _parse_address(value, xor=False)

        # Attributes are padded to a four-byte boundary; the padding is not
        # counted in the length, so walking by the raw length desynchronises
        # every attribute after the first odd-sized one.
        offset = value_at + attr_length + (-attr_length % 4)

    return plain


def _parse_address(value: bytes, *, xor: bool) -> tuple[str, int] | None:
    """One MAPPED-ADDRESS / XOR-MAPPED-ADDRESS attribute value."""
    if len(value) < 8 or value[1] != _FAMILY_IPV4:
        # IPv6 is deliberately not handled: a peer with a routable IPv6 address
        # does not need hole-punching, and mixing families here would mean
        # advertising a candidate the other side may have no route to.
        return None

    port = struct.unpack_from("!H", value, 2)[0]
    raw = value[4:8]
    if xor:
        port ^= MAGIC_COOKIE >> 16
        raw = bytes(a ^ b for a, b in zip(raw, _COOKIE_BYTES))
    return (socket.inet_ntoa(raw), port)


def discover(
    sock: socket.socket,
    servers: list[str] | tuple[str, ...],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, int] | None:
    """Ask each server in turn until one answers. Returns None if none do.

    ``sock`` **must be the socket that will carry traffic afterwards** -- see
    the module docstring. It is used as-is and left as it was found: the caller
    owns its timeout and its bound address.

    Datagrams that are not our response are ignored rather than treated as
    failure. On a shared socket something else may well arrive first, and
    discarding it is safe only because callers run discovery before anything
    else is expected -- see the call sites.
    """
    if not servers:
        return None

    previous_timeout = sock.gettimeout()
    try:
        for entry in servers:
            address = _resolve(entry)
            if address is None:
                continue
            found = _query(sock, address, timeout_s)
            if found is not None:
                log.debug("STUN: %s reports us at %s:%d", entry, *found)
                return found
    finally:
        try:
            sock.settimeout(previous_timeout)
        except OSError:
            pass

    log.debug("STUN: no server answered; continuing without a public candidate")
    return None


def _query(
    sock: socket.socket, address: tuple[str, int], timeout_s: float
) -> tuple[str, int] | None:
    request, transaction_id = binding_request()
    try:
        sock.settimeout(timeout_s)
        sock.sendto(request, address)
    except OSError as exc:
        log.debug("STUN: could not send to %s:%d: %s", *address, exc)
        return None

    # Keep reading until the timeout: on a shared socket the first datagram back
    # may be somebody else's.
    while True:
        try:
            data, _source = sock.recvfrom(2048)
        except (socket.timeout, TimeoutError):
            return None
        except OSError as exc:
            log.debug("STUN: receive failed: %s", exc)
            return None

        found = parse_response(data, transaction_id)
        if found is not None:
            return found


def _resolve(entry: str) -> tuple[str, int] | None:
    """"host:port" (or bare "host", defaulting to 3478) to an address."""
    entry = entry.strip()
    if not entry:
        return None

    host, _, port_text = entry.rpartition(":")
    if not host:
        host, port = entry, 3478
    else:
        try:
            port = int(port_text)
        except ValueError:
            return None

    try:
        return (socket.gethostbyname(host), port)
    except OSError as exc:
        log.debug("STUN: cannot resolve %s: %s", host, exc)
        return None
