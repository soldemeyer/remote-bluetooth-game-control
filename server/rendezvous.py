"""Server-side rendezvous registration for NAT hole-punching.

Hole punching needs *both* peers registered with the broker; this is the server
half. It deliberately does **not** own a socket: it sends and receives through
the datapath's UDP socket via callbacks.

That constraint is the whole point. A NAT mapping belongs to one specific local
port, so the hole must be punched on the exact socket that will later carry
gameplay traffic. Registering from a helper socket would open a mapping for a
port nobody uses, and clients would still be unable to reach us.

What the broker learns: our external address, our LAN address, and the room
code. It never sees the password and cannot decrypt session traffic -- relayed
packets pass through it already AEAD-sealed.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable

from common.timing import now_ns

log = logging.getLogger(__name__)

#: Re-register well inside the broker's PEER_TTL_S (45 s) so our registration
#: never lapses, and so the NAT mapping toward the broker stays open.
REGISTER_INTERVAL_NS = 20_000_000_000  # 20 s

#: Retry faster until the first successful registration.
INITIAL_RETRY_NS = 2_000_000_000       # 2 s


class RendezvousClient:
    """Keeps the server registered with the broker and answers introductions.

    Threading: :meth:`handle_datagram` is called from the datapath thread.
    :meth:`tick` is also called from there, so no locking is needed for the
    registration state -- it is single-threaded by construction.
    """

    def __init__(
        self,
        broker_host: str,
        broker_port: int,
        room_code: str,
        send: Callable[[bytes, tuple[str, int]], None],
        *,
        local_port: int = 0,
        public_name: str = "",
        describe: Callable[[], tuple[int, int]] | None = None,
        role: str = "server",
        on_relay: Callable[[bool], None] | None = None,
    ) -> None:
        self._room = room_code
        self._send = send
        self._local_port = local_port

        #: Which leg of the room we are. The video server reuses this whole
        #: class with role="video-source"; the broker then introduces us to
        #: viewers rather than to players.
        self._role = role

        #: Called when the broker starts relaying for a peer.
        self._on_relay = on_relay
        self._relaying = False

        #: Name to advertise in the broker's public listing. Empty means hidden:
        #: we still register (so anyone with the room code can reach us) but are
        #: never enumerated to a browsing client.
        self._public_name = public_name

        #: Returns (capacity, in_use) for the listing. Cosmetic only.
        self._describe = describe

        self._broker: tuple[str, int] | None = None
        self._broker_host = broker_host
        self._broker_port = broker_port

        self._next_register_ns = 0
        self._registered = False
        self._external: tuple[str, int] | None = None

        #: Clients the broker has introduced us to. We punch toward each so the
        #: NAT mapping opens from our side too -- both peers must send for the
        #: packets to cross.
        self._pending_peers: dict[tuple[str, int], int] = {}

        #: Every peer the broker has ever introduced us to, for this run.
        #: Kept separately from _pending_peers, which is pruned as soon as a
        #: peer connects -- classifying by that alone would flip an
        #: established Internet client to LAN the instant punching finished.
        self._introduced: set[tuple[str, int]] = set()

        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def resolve(self) -> bool:
        """Resolve the broker address. Returns False if it cannot be reached."""
        try:
            info = socket.getaddrinfo(
                self._broker_host, self._broker_port, socket.AF_INET, socket.SOCK_DGRAM
            )
        except (socket.gaierror, OSError) as exc:
            log.error("Could not resolve broker %s: %s", self._broker_host, exc)
            return False

        if not info:
            return False
        self._broker = info[0][4][:2]
        log.info(
            "Rendezvous broker %s:%d, room '%s'",
            self._broker[0],
            self._broker[1],
            self._room,
        )
        return True

    @property
    def broker_address(self) -> tuple[str, int] | None:
        return self._broker

    @property
    def is_registered(self) -> bool:
        return self._registered

    def set_public_name(self, name: str) -> None:
        """Change what the broker lists us as, or hide us entirely with "".

        Takes effect on the next re-registration; the broker replaces a room's
        advertised name on every register, so no explicit withdrawal is needed.
        """
        self._public_name = name or ""

    @property
    def external_address(self) -> tuple[str, int] | None:
        """What the broker says our public (IP, port) looks like."""
        return self._external

    def owns(self, address: tuple[str, int]) -> bool:
        """True if this datagram is broker signalling rather than game traffic."""
        return self._broker is not None and address[:2] == self._broker

    def was_introduced(self, address: tuple[str, int]) -> bool:
        """True if the broker introduced us to this peer.

        This is what tells the two accept gates apart: a peer we were introduced
        to arrived over the Internet path, anything else reached us directly.

        Introduced peers are remembered past the punch itself -- ``_pending_peers``
        is pruned once a session is up, so membership alone would misclassify an
        established Internet client as LAN the moment punching finished.
        """
        with self._lock:
            return address[:2] in self._introduced

    # -- periodic work -----------------------------------------------------

    def tick(self) -> None:
        """Re-register when due, and keep punching toward pending peers.

        Called from the datapath's maintenance path, so it must be cheap and
        must never block.
        """
        if self._broker is None:
            return

        now = now_ns()
        if now >= self._next_register_ns:
            self._register()
            self._next_register_ns = now + (
                REGISTER_INTERVAL_NS if self._registered else INITIAL_RETRY_NS
            )

        # Keep punching at recently-introduced peers. The client is doing the
        # same toward us; the packets crossing in flight is what opens both
        # NAT mappings.
        with self._lock:
            peers = list(self._pending_peers.items())

        from common.protocol import PUNCH_PROBE

        for peer, expires in peers:
            if now > expires:
                with self._lock:
                    self._pending_peers.pop(peer, None)
                continue
            self._send(PUNCH_PROBE, peer)

    def _register(self) -> None:
        assert self._broker is not None

        message = {
            "op": "register",
            "room": self._room,
            "role": self._role,
        }

        # Only a discoverable server sends its name; the broker lists exactly
        # those that do. Sending nothing is what makes hidden mode hidden.
        if self._public_name:
            message["name"] = self._public_name
            if self._describe is not None:
                try:
                    capacity, in_use = self._describe()
                    message["capacity"] = capacity
                    message["in_use"] = in_use
                except Exception:
                    log.debug("Could not describe capacity for the listing", exc_info=True)

        if self._local_port:
            local_ip = self._local_ip()
            if local_ip:
                # Lets a client behind the same NAT reach us directly rather
                # than hairpinning through the router, which many home routers
                # handle badly or not at all.
                message["local"] = [local_ip, self._local_port]

        try:
            self._send(json.dumps(message).encode("utf-8"), self._broker)
        except OSError as exc:
            log.debug("Broker registration send failed: %s", exc)

    def _local_ip(self) -> str | None:
        """Our LAN address on the route toward the broker. Sends nothing."""
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(self._broker)
                return probe.getsockname()[0]
            finally:
                probe.close()
        except OSError:
            return None

    # -- inbound -----------------------------------------------------------

    def handle_datagram(self, data: bytes, address: tuple[str, int]) -> None:
        """Process one broker message. Never raises."""
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(body, dict):
            return

        op = body.get("op")

        if op == "registered":
            if not self._registered:
                log.info("Registered with broker in room '%s'", self._room)
            self._registered = True
            external = body.get("external")
            if isinstance(external, list) and len(external) == 2:
                try:
                    self._external = (str(external[0]), int(external[1]))
                except (TypeError, ValueError):
                    pass

        elif op == "peer":
            self._on_peer(body)

        elif op == "relaying":
            log.warning(
                "Broker is relaying for a client -- NAT traversal failed for that peer. "
                "Latency will be noticeably higher."
            )
            self._relaying = True
            if self._on_relay is not None:
                # The video server uses this to cap its bitrate: relayed media
                # is somebody else's bandwidth, so the configured rate stops
                # being ours alone to choose.
                try:
                    self._on_relay(True)
                except Exception:
                    log.debug("Relay callback raised", exc_info=True)

        elif op == "error":
            log.error("Broker error: %s", body.get("reason", "unknown"))
            self._registered = False

    def _on_peer(self, body: dict) -> None:
        """A client wants to connect; start punching toward it."""
        peer = _parse_address(body.get("address"))
        if peer is None:
            return

        # Punch toward the peer for a bounded window. Both sides must send for
        # the packets to cross, and a stale entry would keep us sending at an
        # address nobody is listening on.
        deadline = now_ns() + 15_000_000_000  # 15 s

        with self._lock:
            new = peer not in self._pending_peers
            self._pending_peers[peer] = deadline
            self._introduced.add(peer)

            local = _parse_address(body.get("local"))
            if local is not None and local != peer:
                # Same-NAT shortcut: try the LAN address too.
                self._pending_peers[local] = deadline
                self._introduced.add(local)

        if new:
            log.info("Broker introduced client at %s:%d; punching", peer[0], peer[1])

    def peer_connected(self, address: tuple[str, int]) -> None:
        """Stop punching toward a peer that has completed its handshake."""
        with self._lock:
            self._pending_peers.pop(address, None)

    def stop(self) -> None:
        """Tell the broker we are leaving, so the room is freed promptly."""
        if self._broker is None:
            return
        try:
            self._send(json.dumps({"op": "bye"}).encode("utf-8"), self._broker)
        except OSError:
            pass
        self._registered = False

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            pending = len(self._pending_peers)
        return {
            "broker": f"{self._broker[0]}:{self._broker[1]}" if self._broker else None,
            "room": self._room,
            "registered": self._registered,
            "external": f"{self._external[0]}:{self._external[1]}"
            if self._external
            else None,
            "punching_at": pending,
        }


def _parse_address(raw) -> tuple[str, int] | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        return (str(raw[0]), int(raw[1]))
    except (TypeError, ValueError):
        return None
