"""The Bluetooth server's link to a video server.

The Bluetooth server is the client here. It holds the operator's *video
password*, connects out to whichever video server the operator pointed it at,
and from then on drives it: settings down, status and preview up.

That direction is deliberate. The operator configures everything from one place
-- the Pi's web GUI -- and a video server is a passive appliance that runs and
waits. It also means the video server needs no idea where the Bluetooth server
is, which matters because the capture PC is the machine most likely to be
rebooted, moved, or replaced mid-session.

Two credentials meet here, and keeping them apart is the point:

  * the **video password** authenticates *us* to the video server, and is the
    operator's alone;
  * the **player password** is handed over this encrypted link so the video
    server can admit viewers, who already have it.

A viewer therefore never learns the video password, and so can never come back
claiming to be the control peer -- the one role tickets do not gate.

Threading: one thread, owning one ClientTransport, exactly like the video
server's old outbound link. The registry it updates is thread-safe.
"""

from __future__ import annotations

import logging
import selectors
import threading
from typing import Any

from common import protocol, video
from common.protocol import ControlOp
from common.timing import now_ns
from client.net.transport import ClientTransport, ConnectionState, TransportError

log = logging.getLogger(__name__)

#: Reconnect backoff. The capture PC may be off, asleep, or rebooting; none of
#: those deserve a tight loop, and Argon2id costs ~0.1 s per attempt.
_RECONNECT_DELAYS = (2.0, 5.0, 10.0, 20.0)

_SERVICE_TIMEOUT_S = 0.02


class VideoLink:
    """Keeps a control session to the configured video server."""

    def __init__(self, registry, datapath, config) -> None:
        self._registry = registry
        self._datapath = datapath
        self._config = config

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._transport: ClientTransport | None = None
        self._last_config_ns = 0
        self._preview = video.FrameAssembler(max_frame_size=256 * 1024)

        self.connected = False
        self.last_error = ""
        self.attempts = 0

    # -- lifecycle ---------------------------------------------------------

    def target(self) -> tuple[str, int]:
        """Where to dial, which is not always what the operator typed.

        In embedded mode the source is our own subprocess on this machine, so
        the address is loopback and there is nothing for the operator to point
        at. ``video_host`` therefore means **the external video server** and
        nothing else.

        It used to mean both. Selecting embedded mode overwrote it with
        ``127.0.0.1``, and switching back to external left that behind -- so
        the server dialled *itself*, forever, for an address nobody had typed.
        The web GUI showed ``127.0.0.1`` in a field the operator had filled in
        with something else, and the only symptom was "Server did not respond.
        Check the address, the port, and that the server is running", which
        sends you to look at the video server, the port and the firewall. All
        three were fine.

        Resolved per attempt rather than captured at construction, because the
        mode can change under a running link.
        """
        if self._embedded():
            return "127.0.0.1", self._config.video_port
        return self._config.video_host, self._config.video_port

    def _embedded(self) -> bool:
        return getattr(self._config, "video_mode", "") == "embedded"

    def credential(self) -> str:
        """The password for whichever server `target()` names.

        Two servers, two credentials, and they must not be confused: the
        external one's is the operator's, the embedded one's is invented for a
        subprocess. Sharing a field meant trying embedded mode once replaced
        the operator's and there was no way back but re-typing it.
        """
        if self._embedded():
            return (
                self._config.video_embedded_password
                or self._config.video_password
            )
        return self._config.video_password

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="video-link", daemon=True)
        self._thread.start()
        host, port = self.target()
        log.info("Connecting to the video server at %s:%d", host, port)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        transport, self._transport = self._transport, None
        if transport is not None:
            try:
                transport.close()
            except Exception:
                log.debug("Error closing the video link", exc_info=True)
        self.connected = False
        self._registry.detach_source("video-link")

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            host, port = self.target()
            password = self.credential()

            if not host or not password:
                self.last_error = "No video server address or password configured"
                if self._stop.wait(2.0):
                    return
                continue

            transport = ClientTransport(
                password,
                client_name=f"{self._config.server_name} (control)",
                auth_extra={"role": "bt-server"},
                on_control=self._on_control,
                on_media=self._on_media,
                rumble_enabled=False,
            )

            self.attempts += 1
            try:
                transport.connect(host, port, timeout_ns=8_000_000_000)
            except TransportError as exc:
                self.connected = False
                self.last_error = str(exc)
                transport.close()
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                attempt += 1
                log.info("Video server unreachable (%s); retrying in %.0fs", exc, delay)
                if self._stop.wait(delay):
                    return
                continue

            attempt = 0
            self.connected = True
            self.last_error = ""
            self._transport = transport
            self._registry.attach_source_endpoint(host, port)
            log.info("Video link up to %s:%d", host, port)

            # Configure it immediately: it has been sitting idle waiting to be
            # told what to capture.
            self._last_config_ns = 0
            self._push_config(transport, force=True)

            try:
                self._service(transport)
            except Exception:
                log.debug("Video link error", exc_info=True)
            finally:
                self.connected = False
                self._transport = None
                self._registry.detach_source("video-link")
                self._datapath.broadcast_video_source()
                try:
                    transport.close()
                except Exception:
                    pass

            if self._stop.is_set():
                return
            log.info("Video link dropped; reconnecting")
            if self._stop.wait(_RECONNECT_DELAYS[0]):
                return

    def _service(self, transport: ClientTransport) -> None:
        selector = selectors.DefaultSelector()
        try:
            selector.register(transport.fileno(), selectors.EVENT_READ)
        except (TransportError, OSError, ValueError):
            return

        try:
            while not self._stop.is_set():
                selector.select(timeout=_SERVICE_TIMEOUT_S)
                transport.service()

                if transport.state in (
                    ConnectionState.DISCONNECTED,
                    ConnectionState.FAILED,
                ):
                    self.last_error = transport.state_detail
                    return

                self._push_config(transport)
        finally:
            selector.close()

    # -- outbound ----------------------------------------------------------

    def _push_config(self, transport: ClientTransport, *, force: bool = False) -> None:
        """Send settings, tickets and the players' password.

        Re-sent until the video server echoes the sequence back in its status,
        which is the same self-healing discipline used everywhere else here
        rather than a second reliability mechanism.
        """
        # One gate, not two. `needs_config_push()` both answers the question and
        # records that a push happened, so checking a second timer afterwards
        # could consume that state and then send nothing -- the configuration
        # silently skipped, and not offered again for another interval. A
        # newly minted ticket landed in exactly that hole, leaving a player
        # waiting on an advert that never turned available.
        if not force and not self._registry.needs_config_push():
            return
        self._last_config_ns = now_ns()

        message = self._registry.config_message()
        # The players' password, so the video server can admit viewers. It
        # travels only over this authenticated, encrypted link -- never to a
        # client, and never on the discovery beacon.
        message["viewer_password"] = self._config.password
        log.debug(
            "Pushing configuration seq %s with %d ticket(s)",
            message.get("cfg_seq"),
            len(message.get("tickets", [])),
        )
        transport.queue_control(ControlOp.VIDEO_CONFIG, message)

    def request_config_push(self) -> None:
        """Push settings now rather than at the next tick."""
        transport = self._transport
        if transport is not None:
            self._push_config(transport, force=True)

    def reconnect(self) -> None:
        """Drop the current session so the loop picks up new details at once.

        Called when the operator changes the address or password: leaving it to
        retry the old ones would look like the new settings were ignored.
        """
        transport = self._transport
        if transport is not None:
            try:
                transport.close()
            except Exception:
                log.debug("Error closing the video link for reconnect", exc_info=True)

    # -- inbound -----------------------------------------------------------

    def _on_control(self, body: dict[str, Any]) -> None:
        if body.get("op") != ControlOp.VIDEO_STATUS:
            return

        changed = self._registry.update_status_from_link(body)
        if changed:
            self._datapath.broadcast_video_source()
            # The layout is part of what "changed materially" covers, and it is
            # the half clients act on immediately. Pushed unconditionally on
            # any material change rather than only on a layout change: the
            # message carries each client's whole region state, so an extra one
            # costs a datagram and a missed one costs a player the wrong crop.
            self._datapath.broadcast_regions()

    def _on_media(self, plaintext: bytes) -> None:
        """Preview frames, arriving the same way video does elsewhere."""
        if plaintext[0] != protocol.PacketType.VIDEO_FRAME:
            return
        try:
            parsed = video.decode_video_slice(plaintext, 0)
        except ValueError:
            return
        self._registry.feed_preview_slice(parsed)

    # -- introspection -----------------------------------------------------

    def snapshot(self) -> dict:
        # The address actually being dialled, not the stored one -- in embedded
        # mode those differ, and reporting the stored one would describe a
        # connection to a machine we are not talking to.
        host, port = self.target()
        return {
            "connected": self.connected,
            "host": host,
            "port": port,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }
