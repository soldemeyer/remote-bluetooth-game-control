"""Answering the Bluetooth server.

The video server is a passive appliance: it binds its port, announces itself on
the LAN, and waits. The Bluetooth server connects *in*, authenticates with the
operator's video password, and drives it from there.

That inversion is what lets the operator configure everything from the Pi's web
GUI, and it means this side needs no idea where the Bluetooth server is -- which
matters, because the capture PC is the machine most likely to be rebooted or
swapped mid-session.

Over that one session:

  * **down** -- VIDEO_CONFIG: capture and encode settings, the list of viewing
    tickets, the broker details, and the players' password so viewers can be
    admitted at all;
  * **up** -- VIDEO_STATUS once a second, and the preview JPEG.

Threading: one ticker thread for the periodic sends. Inbound messages arrive on
VideoNet's receive thread and are handled there.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from common import player_labels, protocol, video
from common.player_labels import encode_reasoning
from common.protocol import ControlOp
from common.timing import now_ns
from common.video import VideoSettings
from videoserver.preview import PreviewEncoder

log = logging.getLogger(__name__)

_STATUS_INTERVAL_NS = 1_000_000_000

#: The parts of what we report that hardly ever change -- our full settings and
#: the capture device list -- travel on their own message, at their own pace.
#:
#: They used to ride in every status, which put two variable-length structures
#: in a message with a hard 1200-byte ceiling. Going over does not truncate
#: anything: ``encode_control`` refuses the **whole message**, so a source that
#: is streaming perfectly stops reporting at all. Measured on a machine with
#: real capture hardware: 1242 bytes, every status refused, once eight more
#: settings existed.
#:
#: They are still *periodic* rather than sent once on change, because the
#: control channel has no retransmit -- a message sent once and lost is lost
#: for good. Slow-and-absolute is the same discipline the status uses, at a
#: cadence matching how often a capture card is plugged in.
#:
#: Sent immediately to a session we have not sent to before, because the
#: settings are how a Bluetooth server with nothing saved adopts what the
#: source is already doing, and making the operator wait five seconds to see
#: their own camera reads as the link not working.
_SLOW_STATE_INTERVAL_NS = 5_000_000_000

#: How often the identification counters go out, while the developer view is
#: on. Slower than the status: they are for somebody reading them, not for
#: anything that reacts.
_STATS_INTERVAL_NS = 2_000_000_000

#: How often what this session has learned goes back to a Bluetooth server
#: that is managing our tuning. Once a second: it feeds readouts beside the
#: Auto switches, and learning moves on a scale of seconds.
_LEARNED_INTERVAL_NS = 1_000_000_000

_TICK_S = 0.1


class ControlResponder:
    """Serves the Bluetooth server's control session."""

    def __init__(self, app: Any) -> None:
        self._app = app

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        self._preview = PreviewEncoder()
        self._preview_frame_id = 0
        self._last_preview_ns = 0
        self._last_status_ns = 0
        self._last_slow_ns = 0
        self._last_tracks_ns = 0
        self._last_stats_ns = 0
        self._last_learned_ns = 0
        #: The control session that has sent us DETECT_TUNING, if any. Only a
        #: Bluetooth server managing our tuning -- in embedded mode, where it
        #: is our parent -- has anywhere to show what we learned.
        self._tuning_peer: object = None
        #: Which control session the slow state was last sent to, so a
        #: reconnecting Bluetooth server is told everything at once
        #: rather than waiting out the interval.
        self._slow_state_peer: object = None
        self._send_buf = bytearray(protocol.MAX_DATAGRAM)

        self.cfg_seq = 0
        self.configured = False
        self.last_config_ns = 0

        #: Does the Bluetooth server have anyone looking at the preview? Starts
        #: false so a source that has not been told anything costs the server's
        #: datapath nothing; the first config push settles it either way.
        self._preview_wanted = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="vs-control", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def connected(self) -> bool:
        return self._app.net.control_session() is not None

    # -- inbound -----------------------------------------------------------

    def _merge_local_device(self, incoming: VideoSettings) -> VideoSettings:
        """Keep the locally chosen capture device when the push does not name one.

        The device is a property of *this* machine -- it is the one physically
        wired to the console, and it is chosen here, in front of it. A blank
        ``device`` therefore means "carry on with whatever you are using", not
        "fall back to the first device you find".

        Without this, connecting to a Bluetooth server that has never been told
        which camera to use silently switches the capture away from the one the
        operator selected, which reads as the video server losing its settings.
        Naming a device explicitly (from the web GUI's dropdown) still works and
        still wins.
        """
        current = self._app.settings
        if not incoming.device and current.device:
            incoming.device = current.device
        if not incoming.audio_device and current.audio_device:
            incoming.audio_device = current.audio_device
        if incoming.backend == "auto" and current.backend != "auto":
            incoming.backend = current.backend
        return incoming

    def on_control(self, session, body: dict[str, Any]) -> None:
        """Handle one control message. Runs on VideoNet's receive thread.

        The role check happened before this was called -- only the Bluetooth
        server's session reaches here.
        """
        op = body.get("op")
        if op == ControlOp.DETECT_TUNING:
            self._apply_tuning(session, body)
            return
        if op == ControlOp.PLAYER_MAP:
            self._apply_player_map(body)
            return
        if op == ControlOp.VIDEO_PLAYER_INPUT:
            self._apply_player_input(body)
            return
        if op != ControlOp.VIDEO_CONFIG:
            return

        cfg_seq = body.get("cfg_seq")
        log.info("Applying configuration from the Bluetooth server (seq %s)", cfg_seq)

        # The players' password first: without it no viewer can authenticate,
        # and a settings change that restarts the encoder would otherwise leave
        # a gap where the stream exists and nobody may watch it.
        viewer_password = body.get("viewer_password")
        if isinstance(viewer_password, str):
            self._app.set_viewer_password(viewer_password)

        tickets = body.get("tickets")
        if isinstance(tickets, list):
            self._app.net.set_tickets({str(t) for t in tickets if isinstance(t, str)})

        # Absent means an older Bluetooth server that does not gate previews;
        # sending them is the behaviour it expects.
        wanted = body.get("preview_wanted")
        self._preview_wanted = True if wanted is None else bool(wanted)

        broker = body.get("broker")
        room = body.get("room")
        if isinstance(broker, str) and isinstance(room, str):
            self._app.set_broker(broker, room)

        # No "config" key at all means the Bluetooth server has nothing
        # configured and is deferring to ours -- distinct from an empty one,
        # which would parse as a full set of defaults and reset us. Tickets and
        # the password above still apply, so viewers work either way.
        raw_config = body.get("config")
        if isinstance(raw_config, dict):
            settings = self._merge_local_device(VideoSettings.from_dict(raw_config))
            try:
                self._app.apply_config(
                    settings, int(cfg_seq) if isinstance(cfg_seq, int) else None
                )
            except Exception:
                log.exception("Could not apply the configuration")
                return
        else:
            log.info("No settings pushed; keeping the ones configured here")

        if isinstance(cfg_seq, int):
            self.cfg_seq = cfg_seq
        self.configured = True
        self.last_config_ns = now_ns()

        # Acknowledge by reporting straight back, so the server stops re-pushing.
        self._send_status(force=True)

    def _apply_tuning(self, session, body: dict[str, Any]) -> None:
        """How to detect the layout and the players, from the Bluetooth server.

        It only ever sends this to a video server that is its own subprocess:
        in external mode this machine owns these settings, in its own window.
        Applied only when it differs, because it arrives every few seconds and
        re-applying rebuilds the split detector and resets its averaging.

        Never saved: a headless child has no config of its own to write, and
        the Bluetooth server keeps the operator's copy.
        """
        from common.video import DetectionTuning

        tuning = DetectionTuning.from_dict(body.get("tuning")).clamped()
        if tuning != self._app.tuning:
            log.info("Applying detection tuning from the Bluetooth server")
            self._app.apply_tuning(tuning)
        if body.get("reset_learning"):
            self._app.reset_learning()
        self._tuning_peer = getattr(session, "client_id", None)
        # Straight back, so the readouts beside the Auto switches settle at
        # once rather than a second later.
        self._last_learned_ns = 0
        self._send_learned()

    def _apply_player_map(self, body: dict[str, Any]) -> None:
        """Which player owns which viewport.

        The one thing this machine cannot work out for itself: the operator
        assigned those regions on the Bluetooth server, and viewport ownership
        is the strongest identity signal there is. Ids only -- we are never
        told anybody's name, and do not need one.
        """
        from videoserver.playervision.types import PlayerHint

        pairs = player_labels.decode_player_map(body)
        self._app.configure_players(
            hints=tuple(PlayerHint(player_id=pid, regions=regions)
                        for pid, regions in pairs)
        )

    def _apply_player_input(self, body: dict[str, Any]) -> None:
        """A short window of each player's stick motion.

        The only identity signal that survives a shared screen, where there is
        no viewport to attribute anything to -- and the only one that can
        separate two players who picked the same character.
        """
        from videoserver.playervision.types import InputTrace

        decoded = player_labels.decode_traces(body)
        self._app.configure_players(
            traces=tuple(InputTrace(player_id=pid, hz=hz, samples=samples)
                         for pid, hz, samples in decoded)
        )

    # -- outbound ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(_TICK_S)
            if self._stop.is_set():
                return
            try:
                # Sampled before the status is sent, so a layout change reaches
                # the Bluetooth server in the same tick it was confirmed rather
                # than a second later -- a second of every player watching the
                # wrong crop. `sample_vision` is its own rate limiter for both
                # consumers and returns immediately when both are off, which
                # is the default.
                changed = self._app.sample_vision()
                self._send_status(force=changed)
                self._send_slow_state()
                self._send_player_stats()
                self._send_learned()
                self._send_tracks()
                self._send_preview()
            except Exception:
                log.debug("Error sending to the Bluetooth server", exc_info=True)

    def _send_status(self, *, force: bool = False) -> None:
        session = self._app.net.control_session()
        if session is None:
            return

        now = now_ns()
        if not force and now - self._last_status_ns < _STATUS_INTERVAL_NS:
            return
        self._last_status_ns = now

        payload: dict[str, Any] = {
            "cfg_seq": self.cfg_seq,
            "media_port": self._app.net.port,
            "lan_host": _local_ip_toward(*session.address),
            "status": self._app.status(),
        }
        self._app.net.send_control(session, ControlOp.VIDEO_STATUS, payload)

    def _send_slow_state(self) -> None:
        """Our settings and device list, on their own message and cadence.

        A partial VIDEO_STATUS is fine by construction: the registry guards
        every key with its own ``isinstance`` check and updates only what
        arrived, so a message carrying no ``status`` leaves the media port,
        the layout and everything else exactly as they were.

        The settings are what a Bluetooth server with nothing saved adopts as
        its own, and ``status`` cannot stand in for them -- it reports what the
        encoder produced, not the device or the backend behind it.
        """
        session = self._app.net.control_session()
        if session is None:
            # Forget the peer as well, so the next session is told at once
            # rather than inheriting a timer from the last one.
            self._slow_state_peer = None
            return

        peer = getattr(session, "client_id", None)
        now = now_ns()
        if peer == self._slow_state_peer and now - self._last_slow_ns < _SLOW_STATE_INTERVAL_NS:
            return
        self._slow_state_peer = peer
        self._last_slow_ns = now

        payload: dict[str, Any] = {"settings": self._app.settings.to_dict()}
        devices = _devices_that_fit(payload, self._app.devices)
        if devices:
            payload["devices"] = devices

        self._app.net.send_control(session, ControlOp.VIDEO_STATUS, payload)

    def _send_tracks(self) -> None:
        """Where each identified player is. Its own message and cadence.

        **Not folded into the status**, which is already ~650 bytes against a
        hard 1200-byte ceiling and whose headroom is guarded by a test for
        exactly this reason: `encode_control` refuses an oversized message
        *whole*, so a source that grew one field too many stops reporting at
        all rather than reporting less. Tracks also want several sends a
        second against the status's one.

        Paced to the sample rate, because sending the same rows twice tells
        the Bluetooth server nothing it did not already act on.
        """
        settings = self._app.settings
        if not settings.player_id_enabled:
            return
        session = self._app.net.control_session()
        if session is None:
            return

        now = now_ns()
        interval = int(1_000_000_000 / max(float(settings.player_id_hz or 6.0), 0.5))
        if self._last_tracks_ns and now - self._last_tracks_ns < interval:
            return
        self._last_tracks_ns = now

        rows = self._app.player_rows()
        layout = self._app.layout_snapshot()["mode"]
        # Sent even when empty: the Bluetooth server has clients that may be
        # drawing a label for somebody who has just left the picture, and
        # silence cannot tell them to stop.
        payload = player_labels.encode_tracks(rows, str(layout), now)
        self._app.net.send_control(session, ControlOp.VIDEO_TRACKS, payload)

    def _send_player_stats(self) -> None:
        """The detailed identification counters, on a message of their own.

        **Not folded into the slow state**, and that is the same lesson a
        third time. Settings and the device list are already two
        variable-length structures sharing one message with a hard 1200-byte
        ceiling; measured, adding these to them came to 1369 bytes and
        `encode_control` refuses whole. The registry guards every top-level
        key with its own `isinstance`, so a message carrying nothing else
        disturbs nothing else -- which is exactly why `_send_slow_state` is
        separate from `_send_status` in the first place.

        Only while the operator has the developer view on. It is a few hundred
        bytes several times a minute that nobody else reads.
        """
        if not self._app.settings.player_id_debug:
            return
        session = self._app.net.control_session()
        if session is None:
            return

        now = now_ns()
        if self._last_stats_ns and now - self._last_stats_ns < _STATS_INTERVAL_NS:
            return

        stats = self._app.player_id_stats()
        if not stats:
            return
        self._last_stats_ns = now
        self._app.net.send_control(
            session, ControlOp.VIDEO_STATUS, {"player_id_stats": stats}
        )

        # The per-track breakdown, on a message of its own rather than beside
        # the counters. Same reason the counters are not beside the settings:
        # two variable-length structures in one message is how this channel
        # has gone silent twice, and `encode_reasoning` trims against a real
        # encoded size that a shared budget would make it guess at.
        #
        # It is the *only* way this reaches an embedded source's operator:
        # there the video server is a headless subprocess with no window of
        # its own, so the web GUI is the only place it can be read.
        judgements = self._app.player_judgements()
        if judgements:
            self._app.net.send_control(
                session,
                ControlOp.VIDEO_STATUS,
                {"player_id_why": encode_reasoning(judgements)},
            )

    def _send_learned(self) -> None:
        """What this session has learned, to a server that manages our tuning.

        Its own message: the readouts are variable-length -- a learned anchor
        per viewport -- and two variable-length structures sharing one
        message is how this channel has gone silent twice before.
        """
        session = self._app.net.control_session()
        if session is None or self._tuning_peer is None:
            return
        if getattr(session, "client_id", None) != self._tuning_peer:
            # A different Bluetooth server has connected; it has not asked.
            self._tuning_peer = None
            return
        now = now_ns()
        if self._last_learned_ns and now - self._last_learned_ns < _LEARNED_INTERVAL_NS:
            return
        self._last_learned_ns = now
        self._app.net.send_control(
            session, ControlOp.DETECT_LEARNED, encode_learned(self._app.learned())
        )

    def _send_preview(self) -> None:
        session = self._app.net.control_session()
        if session is None:
            return

        # Driven by the server's live "somebody has the panel open" flag, not
        # by our own `preview_enabled`. That setting travels in the pushed
        # config, so consulting it here would mean a value we were handed could
        # switch the preview off permanently -- and there is no control on
        # either GUI to switch it back.
        if not self._preview_wanted:
            return

        settings: VideoSettings = self._app.settings

        interval_ns = 1_000_000_000 // max(settings.preview_fps, 1)
        now = now_ns()
        if now - self._last_preview_ns < interval_ns:
            return
        self._last_preview_ns = now

        # Follow the operator's chosen size. Rebuilding the encoder rather than
        # resizing one: it holds an MJPEG context bound to a frame size, and it
        # is cheap to make.
        if self._preview.width != settings.preview_width:
            self._preview = PreviewEncoder(width=settings.preview_width)

        jpeg, captured = self._app.encode_preview(self._preview)
        if not jpeg or captured is None:
            return

        frame_id = self._preview_frame_id
        self._preview_frame_id = (frame_id + 1) & 0xFFFFFFFF

        payload = memoryview(jpeg)
        count = video.slice_count_for(len(jpeg))
        for index in range(count):
            chunk = payload[
                index * video.VIDEO_SLICE_PAYLOAD : (index + 1) * video.VIDEO_SLICE_PAYLOAD
            ]
            size = video.encode_video_slice_into(
                self._send_buf,
                0,
                frame_id,
                index,
                count,
                video.SliceFlags.KEYFRAME,     # every JPEG stands alone
                video.MediaCodec.MJPEG,
                captured.capture_ts,
                chunk,
            )
            self._app.net.send_to(session, bytes(self._send_buf[:size]))

    def snapshot(self) -> dict[str, object]:
        return {
            "connected": self.connected,
            "configured": self.configured,
            "cfg_seq": self.cfg_seq,
            "preview_frames": self._preview.frames_encoded,
        }


def _local_ip_toward(host: str, port: int) -> str:
    """Our LAN address on the route to ``host``. Sends nothing.

    Reported so a viewer on our own subnet can reach us directly rather than
    via whatever address the Bluetooth server happens to know us by.
    """
    import socket

    if not host:
        return ""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((host, port or 1))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def _devices_that_fit(payload: dict[str, Any], devices: list[dict[str, str]]) -> list:
    """As many devices as the message has room for, longest-first dropped.

    The device list is the one genuinely unbounded thing we report: a machine
    with a capture card, a webcam and several virtual cameras can name enough
    of them to exceed the datagram on its own. Trimming here means the
    operator sees most of their devices; not trimming means they see the
    message refused and no devices at all.
    """
    if not devices:
        return []

    room = protocol.MAX_DATAGRAM - 64  # header, op, and the JSON around it
    used = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    kept: list[dict[str, str]] = []
    for device in devices:
        cost = len(json.dumps(device, separators=(",", ":")).encode("utf-8")) + 1
        if used + cost > room:
            log.debug("Dropping %d capture device(s) that do not fit the message",
                      len(devices) - len(kept))
            break
        used += cost
        kept.append(device)
    return kept


def encode_learned(learned: dict[str, Any]) -> dict[str, Any]:
    """The DETECT_LEARNED body: floats to two places, nothing unbounded.

    Anchors are keyed by region, and there are at most eight region names
    across every layout, so the message is bounded by the vocabulary rather
    than by how long a session runs.
    """

    def tidy(value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 2)
        if isinstance(value, dict):
            return {str(k): tidy(v) for k, v in list(value.items())[:16]}
        if isinstance(value, (list, tuple)):
            return [tidy(v) for v in list(value)[:4]]
        if isinstance(value, (int, str, bool)) or value is None:
            return value
        return None

    split = learned.get("split") if isinstance(learned.get("split"), dict) else {}
    identity = learned.get("identity") if isinstance(learned.get("identity"), dict) else {}
    return {"split": tidy(split), "identity": tidy(identity)}
