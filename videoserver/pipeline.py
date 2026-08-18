"""Composition root: wires capture, encode, network and control together.

Owns the lifecycle and the one piece of policy that spans components -- the
bitrate governor, which needs to see receiver reports (network) to decide what
to tell the encoder.

GC note: this process deliberately does **not** call
``configure_gc_for_realtime()``. The Bluetooth server's datapath does, because
it allocates nothing per packet; PyAV allocates per frame, so disabling the
cyclic collector here would leak steadily. ``gc.freeze()`` alone is safe and
worth it: it moves everything alive at startup out of the collector's reach, so
each later collection has less to walk.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import threading
from typing import Any

from common.timing import now_ns
from common.video import VideoSettings
from videoserver.capture import AudioCapture, VideoCapture, enumerate_devices
from videoserver.config import VideoServerConfig
from videoserver.encode import AudioEncoder, VideoEncoder, available_encoders
from videoserver.net import VideoNet

log = logging.getLogger(__name__)

#: How long a client's loss has to stay bad before the governor reacts, and how
#: long it must stay clean before quality creeps back. Asymmetric on purpose:
#: drop fast so play stays responsive, recover slowly so the stream does not
#: oscillate between two bitrates.
_GOVERNOR_INTERVAL_NS = 5_000_000_000
_RECOVERY_INTERVAL_NS = 30_000_000_000
_LOSS_THRESHOLD = 0.05
_BITRATE_FLOOR_KBPS = 1000

#: Keep the last few errors for the GUI and for VIDEO_STATUS. Bounded so a
#: flapping device cannot grow it without limit.
_MAX_ERRORS = 8


class VideoServerApp:
    """Everything the video server is, minus the user interface."""

    def __init__(self, config: VideoServerConfig) -> None:
        self.config = config
        self.settings = config.settings.clamped()

        self._lock = threading.Lock()

        #: Serialises capture/encode restarts. Three threads can ask for one:
        #: the control link applying a new configuration, the main loop's
        #: bitrate governor, and the receive loop noticing a relay. Two
        #: overlapping restarts orphan a capture object that still holds the
        #: device, and the replacement then fails with the device busy --
        #: reported as a capture-card fault rather than as a race.
        self._media_lock = threading.RLock()

        #: Serialises preview encoding. See :meth:`encode_preview` -- the two
        #: preview consumers share one frame object, and reformatting it from
        #: both threads at once wedges one of them for good.
        self._preview_lock = threading.Lock()

        self._errors: list[str] = []
        self._running = False

        self._capture: VideoCapture | None = None
        self._encoder: VideoEncoder | None = None
        self._audio_capture: AudioCapture | None = None
        self._audio_encoder: AudioEncoder | None = None

        #: Set once a control responder is attached, so inbound control
        #: messages have somewhere to go.
        self.responder: Any = None

        self.net = VideoNet(
            self.settings,
            config.password,
            bind_host=config.media_bind_host,
            bind_port=config.media_port,
            # Viewers must be vouched for unless this is a standalone run.
            # Standalone has no Bluetooth server to issue tickets, which is the
            # whole point of standalone.
            require_tickets=not config.standalone,
            on_idr_request=self._on_idr_request,
            on_control=self._on_control,
        )

        self.cfg_seq = 0
        self.devices: list[dict[str, str]] = []

        self._configured_bitrate = self.settings.bitrate_kbps
        self._active_bitrate = self.settings.bitrate_kbps
        self._relay_active = False
        self._last_governor_ns = 0
        self._last_recovery_ns = 0

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        self.net.start()
        self._start_rendezvous()
        self._start_media()

        # Everything alive now is permanent; take it out of the collector's
        # reach so later collections stay cheap.
        gc.collect()
        gc.freeze()

        log.info("Video server running on port %d", self.net.port)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop_media()

        client = self.net.rendezvous
        if client is not None:
            # Free the broker room now rather than waiting out the TTL.
            try:
                client.stop()
            except Exception:
                log.debug("Rendezvous stop failed", exc_info=True)
            self.net.rendezvous = None

        self.net.stop()

    @property
    def is_running(self) -> bool:
        return self._running

    def _restart_media(self) -> None:
        """Stop and start as one step, so no two callers interleave."""
        with self._media_lock:
            self._stop_media()
            self._start_media()

    def _restart_encoders(self) -> None:
        """Rebuild the encoders, leaving the capture devices open.

        A codec context cannot portably be reconfigured in place across the
        four encoders supported here, so it is rebuilt -- but there is no
        reason to disturb the camera to do it, and every reason not to.
        """
        with self._media_lock:
            capture, audio_capture = self._capture, self._audio_capture
            if capture is None:
                self._start_media_locked()
                return

            for encoder in (self._audio_encoder, self._encoder):
                if encoder is not None:
                    try:
                        encoder.stop()
                    except Exception:
                        log.debug("Error stopping an encoder", exc_info=True)
            self._encoder = None
            self._audio_encoder = None

            self.net.set_settings(self.settings)
            self._encoder = VideoEncoder(
                self.settings, capture,
                on_frame=self._on_encoded, on_error=self._record_error,
            )
            self._encoder.start()

            if self.settings.audio_enabled and audio_capture is not None:
                self._audio_encoder = AudioEncoder(
                    self.settings, audio_capture,
                    on_packet=self.net.send_audio, on_error=self._record_error,
                )
                self._audio_encoder.start()

    def _start_media(self) -> None:
        with self._media_lock:
            self._start_media_locked()

    def _start_media_locked(self) -> None:
        # The sender paces against the frame rate, so it needs the settings the
        # encoder is actually running with.
        self.net.set_settings(self.settings)

        if self._capture is not None and self._capture.is_running:
            # A previous capture never let go of the device. Opening another on
            # it would fail, and keep failing, so wait for the governor's next
            # tick instead and say why.
            self._record_error(
                "Waiting for the previous capture to release the device."
            )
            return

        self._capture = VideoCapture(self.settings, on_error=self._record_error)
        self._encoder = VideoEncoder(
            self.settings,
            self._capture,
            on_frame=self._on_encoded,
            on_error=self._record_error,
        )
        self._capture.start()
        self._encoder.start()

        self._start_audio_locked()

    def _start_audio_locked(self) -> None:
        """Start audio capture and encode. Caller holds ``_media_lock``.

        Separate from the video half because the two devices release at their
        own pace: audio is routinely still held when video has already let go,
        so a settings change that reopens capture must be able to bring the
        picture back now and the sound back a moment later.
        """
        if not self.settings.audio_enabled:
            return

        if self._audio_capture is not None and self._audio_capture.is_running:
            # Same rule as video, and it was missing here: a capture that has
            # not let go is kept, never replaced. Assigning over it orphans a
            # thread still holding the microphone -- so the replacement cannot
            # open it either, and nothing is left holding a reference to the
            # one that has to be stopped first.
            self._record_error(
                "Waiting for the previous audio capture to release the device."
            )
            return

        self._audio_capture = AudioCapture(self.settings, on_error=self._record_error)
        self._audio_encoder = AudioEncoder(
            self.settings,
            self._audio_capture,
            on_packet=self.net.send_audio,
            on_error=self._record_error,
        )
        self._audio_capture.start()
        self._audio_encoder.start()

    def _start_rendezvous(self) -> None:
        """Register with the broker so distant viewers can punch to us.

        Registered on the **media** socket, with its own role. The gameplay
        server registers the same room from its own socket, so a room ends up
        holding two independent pairs -- which is why the broker needed a
        separate video leg rather than reusing the existing one.
        """
        cfg = self.config
        if not (cfg.broker_host and cfg.room_code):
            return

        try:
            from server.rendezvous import RendezvousClient
        except ImportError as exc:
            log.warning("Internet video unavailable: %s", exc)
            return

        client = RendezvousClient(
            cfg.broker_host,
            cfg.broker_port,
            cfg.room_code,
            send=self.net.send_raw,
            local_port=self.net.port,
            role="video-source",
            on_relay=self.set_relay_active,
        )
        if not client.resolve():
            log.error("Broker unreachable; video is LAN-only for this run")
            return

        self.net.rendezvous = client
        log.info("Registered for Internet video in room '%s'", cfg.room_code)

    def _stop_media(self) -> None:
        with self._media_lock:
            encoders = (self._audio_encoder, self._encoder)
            captures = (self._audio_capture, self._capture)

            # Clear the references first: a concurrent caller then sees an
            # empty pipeline rather than one being torn down under it.
            self._encoder = None
            self._audio_encoder = None

            for component in encoders:
                if component is None:
                    continue
                try:
                    component.stop()
                except Exception:
                    log.debug("Error stopping %s", type(component).__name__, exc_info=True)

            # Captures are cleared only once they have really let go. Holding a
            # stalled one is what stops us opening a second capture on a device
            # the first has not released -- see VideoCapture.stop.
            self._audio_capture = self._release(self._audio_capture, "audio capture")
            self._capture = self._release(self._capture, "capture")

    @staticmethod
    def _release(capture, label: str):
        """Stop a capture. Returns it back if it would not let go, else None."""
        if capture is None:
            return None
        try:
            if capture.stop():
                return None
        except Exception:
            log.debug("Error stopping %s", label, exc_info=True)
            return None
        log.warning("The %s is still holding its device; not opening another", label)
        return capture

    # -- configuration -----------------------------------------------------

    def apply_config(self, settings: VideoSettings, cfg_seq: int | None = None) -> None:
        """Adopt new settings, restarting only what actually changed.

        Restarting the capture/encode pair rather than reconfiguring it is a
        deliberate trade: a live codec context cannot portably change
        resolution or bitrate across the four encoders we support, and a ~200 ms
        gap when the operator moves a slider is a fair price for not having to
        trust that.
        """
        new = settings.clamped()
        old = self.settings

        with self._lock:
            self.settings = new
            self.config.settings = new
            self._configured_bitrate = new.bitrate_kbps
            if cfg_seq is not None:
                self.cfg_seq = cfg_seq

        if new.probe_devices:
            self.probe_devices()

        # Only reopening the camera when the *camera's* settings changed.
        # Everything else restarts the encoder alone, which touches no device.
        #
        # Reopening is the risky operation: a stalled camera can leave the old
        # capture thread holding it, and the replacement then cannot open it at
        # all. So a bitrate change -- which the governor makes on its own,
        # repeatedly -- must never cost a device reopen.
        capture_fields = (
            "backend", "device", "audio_device", "test_source",
            "width", "height", "fps", "audio_enabled",
        )
        encode_fields = (
            "bitrate_kbps", "encoder", "gop_s", "intra_refresh", "audio_bitrate_kbps",
        )

        capture_changed = [f for f in capture_fields if getattr(old, f) != getattr(new, f)]
        encode_changed = [f for f in encode_fields if getattr(old, f) != getattr(new, f)]

        if not self._running:
            return

        if capture_changed:
            log.info("Reopening capture: %s changed", ", ".join(capture_changed))
            self._active_bitrate = new.bitrate_kbps
            self._restart_media()
        elif encode_changed:
            log.info("Restarting the encoder: %s changed", ", ".join(encode_changed))
            self._active_bitrate = new.bitrate_kbps
            self._restart_encoders()

    def set_broker(self, broker: str, room: str) -> None:
        """Adopt broker details supplied by the Bluetooth server.

        Registers the video leg if we were not already, so turning the Internet
        path on at the server reaches an already-running source without anyone
        restarting it.
        """
        host, _, port = broker.partition(":")
        changed = (
            host != self.config.broker_host
            or room != self.config.room_code
            or (port.isdigit() and int(port) != self.config.broker_port)
        )
        if not changed:
            return

        self.config.broker_host = host
        if port.isdigit():
            self.config.broker_port = int(port)
        self.config.room_code = room

        client = self.net.rendezvous
        if client is not None:
            with contextlib.suppress(Exception):
                client.stop()
            self.net.rendezvous = None

        if self._running and host and room:
            self._start_rendezvous()

    def probe_devices(self) -> list[dict[str, str]]:
        """Refresh the device list. Never raises."""
        self.devices = enumerate_devices(self.settings.backend)
        return self.devices

    def set_relay_active(self, active: bool) -> None:
        """Note that a client's path fell back to the broker relay.

        Relayed video costs someone else's bandwidth, so the configured bitrate
        stops being the operator's decision alone and gets capped.
        """
        if active == self._relay_active:
            return
        self._relay_active = active
        log.info("Relay path %s; bitrate cap %s", "engaged" if active else "cleared",
                 f"{self.settings.relay_bitrate_kbps} kbps" if active else "removed")
        self._apply_bitrate(self._target_bitrate())

    # -- frame plumbing ----------------------------------------------------

    def _on_encoded(self, frame: Any) -> None:
        self.net.submit_frame(frame)

    def _on_idr_request(self) -> None:
        encoder = self._encoder
        if encoder is not None:
            encoder.request_idr()

    def _on_control(self, session, body: dict) -> None:
        """Route an inbound control message to the responder, if one is attached."""
        if self.responder is not None:
            self.responder.on_control(session, body)

    def set_viewer_password(self, password: str) -> None:
        """Adopt the players' password, so viewers can authenticate.

        Costs one Argon2id derivation, so it happens when the Bluetooth server
        tells us -- not per connection. Ignored when it has not changed, since
        re-deriving would drop every viewer for nothing.
        """
        dropped = self.net.sessions.set_viewer_password(password)
        if dropped:
            log.info("Viewer password changed; %d viewer(s) must reconnect", dropped)

    def latest_capture(self) -> Any:
        """Newest raw frame, for the preview encoder.

        Read from the capture's own latest-frame tap rather than the queue the
        encoder drains, so generating a preview never costs the stream a frame.

        Callers that intend to *reformat* the frame must go through
        :meth:`encode_preview` instead -- see the warning there.
        """
        capture = self._capture
        return capture.latest if capture is not None else None

    def encode_preview(self, encoder: Any) -> tuple[bytes | None, Any]:
        """Encode the newest frame as a preview JPEG. Returns ``(jpeg, captured)``.

        There are two preview consumers -- the local GUI window and the
        responder feeding the Bluetooth server's web preview -- and both read
        the one shared ``capture.latest``.

        The actual protection against the reformatter race lives in
        :class:`~videoserver.preview.PreviewEncoder`, which owns its scaler
        rather than using the one PyAV caches on the frame; that is what also
        covers the *encoder* thread, which reformats the very same object and
        cannot be put behind this lock without dragging preview work onto the
        hot path.

        This lock is the cheap belt-and-braces on top: one entry point for
        "take the newest frame and encode it", so the two previews cannot
        interleave even if someone later reaches for ``frame.reformat()``.
        Previews run at a few frames a second and a 320-wide encode is well
        under a millisecond, so it costs nothing.
        """
        with self._preview_lock:
            captured = self.latest_capture()
            if captured is None:
                return None, None
            return encoder.encode(captured.frame), captured

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)
            del self._errors[:-_MAX_ERRORS]

    # -- bitrate governor --------------------------------------------------

    def tick_governor(self) -> None:
        """Adjust bitrate from receiver reports. Called about once a second."""
        self._recover_capture_if_needed()
        self._recover_audio_if_needed()

        now = now_ns()
        if now - self._last_governor_ns < _GOVERNOR_INTERVAL_NS:
            return
        self._last_governor_ns = now

        worst = self._worst_loss()
        target = self._target_bitrate()

        if worst > _LOSS_THRESHOLD:
            reduced = max(int(self._active_bitrate * 0.75), _BITRATE_FLOOR_KBPS)
            if reduced < self._active_bitrate:
                log.info(
                    "Loss at %.1f%%; reducing bitrate %d -> %d kbps",
                    worst * 100, self._active_bitrate, reduced,
                )
                self._apply_bitrate(reduced)
                # A lower bitrate needs a fresh reference or the first frames
                # after the change are predicted from a much better picture.
                self._on_idr_request()
            self._last_recovery_ns = now
            return

        if self._active_bitrate < target and now - self._last_recovery_ns >= _RECOVERY_INTERVAL_NS:
            self._last_recovery_ns = now
            restored = min(int(self._active_bitrate * 1.1) + 1, target)
            log.info("Path clean; raising bitrate %d -> %d kbps",
                     self._active_bitrate, restored)
            self._apply_bitrate(restored)

    def _recover_capture_if_needed(self) -> None:
        """Start capture again once a stuck device has finally let go.

        Without this, one stalled restart would leave the video server running
        with no picture until somebody restarted it by hand -- which is exactly
        the state the old code got stuck in, and it looked like the capture
        card had died.
        """
        if not self._running:
            return

        capture = self._capture
        if capture is not None and (capture.is_running or self._encoder is not None):
            return

        with self._media_lock:
            if self._capture is not None and self._capture.is_running:
                return
            if self._capture is not None:
                # It has exited at last, so the device is free.
                self._capture = None
            log.info("Capture is not running; starting it again")
            self._start_media_locked()

    def _recover_audio_if_needed(self) -> None:
        """Bring audio back once its device has finally let go.

        Video recovers above, and audio needs its own pass: when only the
        microphone was still held, the picture restarts and this is the only
        thing that ever retries the sound. Without it a resolution change could
        leave a permanently silent stream -- with the video fine, so nothing
        looks broken enough to investigate.
        """
        if not self._running or not self.settings.audio_enabled:
            return

        capture = self._audio_capture
        if capture is not None and capture.is_running:
            return

        with self._media_lock:
            if not self._running or not self.settings.audio_enabled:
                return
            if self._capture is None or not self._capture.is_running:
                # Video is down too; its own recovery starts both.
                return
            if self._audio_capture is not None and self._audio_capture.is_running:
                return
            log.info("Audio capture is not running; starting it again")
            self._start_audio_locked()

    def _target_bitrate(self) -> int:
        if self._relay_active:
            return min(self._configured_bitrate, self.settings.relay_bitrate_kbps)
        return self._configured_bitrate

    def _worst_loss(self) -> float:
        worst = 0.0
        for entry in self.net.client_snapshot():
            report = entry.get("report") or {}
            received = float(report.get("slices_received", 0) or 0)
            lost = float(report.get("slices_lost", 0) or 0)
            total = received + lost
            if total > 100:      # ignore a report too small to mean anything
                worst = max(worst, lost / total)
        return worst

    def _apply_bitrate(self, kbps: int) -> None:
        if kbps == self._active_bitrate:
            return
        self._active_bitrate = kbps
        adjusted = VideoSettings(**{**self.settings.to_dict(), "bitrate_kbps": kbps})
        with self._lock:
            self.settings = adjusted
        if self._running:
            self._restart_media()

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, object]:
        """The body of a VIDEO_STATUS message, and what the GUI renders."""
        capture = self._capture
        encoder = self._encoder
        audio = self._audio_encoder

        fps = 0.0
        if capture is not None:
            interval = capture.interval.p50
            if interval:
                fps = round(1000.0 / interval, 1)

        encode_stats = encoder.encode.snapshot() if encoder is not None else {}
        return {
            # Frames actually coming out, not merely a thread being alive. On a
            # machine whose capture device is missing, the encoder opens fine
            # and then sits there forever: reporting that as "streaming" sends
            # the operator looking at the network when the problem is that
            # nothing is being captured at all.
            "streaming": bool(
                encoder is not None
                and encoder.is_running
                and encoder.encoder_name
                and encoder.frames_encoded > 0
            ),
            "capturing": bool(capture is not None and capture.frames_captured > 0),
            "encoder": encoder.encoder_name if encoder is not None else "",
            "width": self.settings.width,
            "height": self.settings.height,
            "fps": fps,
            "target_fps": self.settings.fps,
            "bitrate_kbps": self._active_bitrate,
            "configured_bitrate_kbps": self._configured_bitrate,
            "relay_capped": self._relay_active,
            "clients": self.net.client_count,
            "frames_encoded": encoder.frames_encoded if encoder is not None else 0,
            "encode_p50_ms": encode_stats.get("p50", 0.0),
            "encode_p99_ms": encode_stats.get("p99", 0.0),
            "audio": bool(audio is not None and audio.is_running),
            # The level travels with the status so the Bluetooth server's web
            # GUI can show the same meter as the local window: "is sound
            # actually being captured" is the question an operator asks from
            # whichever machine they are sitting at.
            "audio_level": round(audio.level_peak, 4) if audio is not None else 0.0,
            "audio_rms": round(audio.level_rms, 4) if audio is not None else 0.0,
            "audio_live": bool(
                audio is not None
                and audio.level_ns
                and now_ns() - audio.level_ns < 1_000_000_000
            ),
            "test_source": self.settings.test_source,
            "errors": list(self._errors),
        }

    def snapshot(self) -> dict[str, object]:
        """Full local view, for the standalone GUI."""
        return {
            "status": self.status(),
            "settings": self.settings.to_dict(),
            "net": self.net.snapshot(),
            "clients": self.net.client_snapshot(),
            "capture": self._capture.snapshot() if self._capture else {},
            "encoder": self._encoder.snapshot() if self._encoder else {},
            "audio": self._audio_encoder.snapshot() if self._audio_encoder else {},
            "devices": self.devices,
            "available_encoders": available_encoders(),
            "cfg_seq": self.cfg_seq,
        }
