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
_LOSS_THRESHOLD = 0.05
_BITRATE_FLOOR_KBPS = 1000

#: How often, and by how much, quality is given back once a path is clean.
#:
#: **The old values made recovery take minutes.** At +10% every 30 s, climbing
#: back from the floor to 6000 kbps needs sixteen steps -- eight minutes of
#: watching a degraded stream because of a problem that lasted thirty seconds.
#: Measured on hardware: after congestion cleared, the bitrate was still at
#: 2256 of 6000 kbps three minutes later.
#:
#: Down is still much faster than up, which is the right asymmetry -- but it
#: was roughly a hundred times faster, not a few times. 5 s and +25% climbs the
#: same range in about thirty seconds and six encoder rebuilds.
#:
#: Overshooting into congestion is now cheap to detect: the delay signal fires
#: within two seconds, long before loss. That is what makes a brisk ramp safe
#: here where it would not have been when loss was the only warning.
_RECOVERY_INTERVAL_NS = 5_000_000_000
_RECOVERY_STEP = 1.25

#: Standing queue, in milliseconds, that counts as congestion.
#:
#: **Loss is a lagging indicator.** By the time a router drops, its queue has
#: already been full for some time and every packet in it has been paying that
#: delay -- which on a 5 s window is seconds of latency before anything reacts.
#: A rising one-way delay is the same event seen earlier, and it is the only
#: signal available before the damage is done.
#:
#: 40 ms is comfortably above the jitter measured on a real WiFi path (p99
#: 5.93 ms, p99.9 18.09) so ordinary variation cannot trip it, and well below
#: the point where a player would call the stream broken.
_QUEUE_THRESHOLD_MS = 40.0

#: The delay check runs on its own, much faster cadence than the loss check.
#:
#: **This is the whole difference between the signal working and not.**
#: Measured against a 1 MB bufferbloat queue on a capped path: the buffer
#: filled in about three seconds, so with the delay check sitting behind the
#: 5 s loss gate there was never a tick where delay was high and loss was
#: still zero -- loss won the race every time and the early signal was
#: decoration. Reports arrive at 1 Hz, so checking faster than that reads the
#: same numbers twice.
_QUEUE_INTERVAL_NS = 1_000_000_000

#: Consecutive delay checks the queue must stay high before acting. One
#: reading can be an unlucky window; two a second apart is a standing queue,
#: and still well inside the time a bloated buffer takes to fill.
_QUEUE_CONFIRM_TICKS = 2

#: Gentler than the loss reduction (0.75). Delay is caught early, so there is
#: time to converge in steps rather than lunging -- and the loss path measured
#: on hardware overshot from 8000 all the way to the floor when 3900 would
#: have done.
_QUEUE_BACKOFF = 0.85

#: Slice loss at which a parity slice starts being worth its bandwidth.
#:
#: Loss amplifies badly without it. Measured at 1280x720@60, ~11 slices per
#: frame: 0.5% slice loss cost 14% of the frame rate, and 1% cost 24% -- a
#: frame dies to a single missing slice, the broken reference chain kills the
#: frames after it, and the keyframe requested to repair it is the biggest and
#: most loss-prone frame there is.
#:
#: 0.2% is below where that spiral starts and far above the zero a clean path
#: reports. Hysteresis is wide because parity costs ~9% of the bitrate and
#: flapping it would keep changing the encoder's budget.
_FEC_ON_LOSS = 0.002
_FEC_OFF_LOSS = 0.0005

#: Consecutive clean checks before parity is switched off again. Loss arrives
#: in bursts; giving up protection after one quiet second is how the next burst
#: goes unprotected.
_FEC_OFF_TICKS = 15

#: Keep the last few errors for the GUI and for VIDEO_STATUS. Bounded so a
#: flapping device cannot grow it without limit.
_MAX_ERRORS = 8


class VideoServerApp:
    """Everything the video server is, minus the user interface."""

    def __init__(self, config: VideoServerConfig) -> None:
        self.config = config
        self.settings = config.settings.clamped()


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

        self._init_governor()

    def _init_governor(self) -> None:
        """All of the governor's mutable state, in one place.

        Separate from ``__init__`` so it can be reset, and so a test can build
        the governor without a socket or a capture device. It was inlined, and
        every field added to it broke a test that enumerated the fields by
        hand -- three times. A control loop's state should be constructible in
        one call.
        """
        #: Guards `settings`, which the governor rewrites on every bitrate
        #: change. Created here rather than beside the other locks so this
        #: method really is the only thing needed to have a working governor --
        #: which is the property the tests rely on.
        self._lock = threading.Lock()

        self._configured_bitrate = self.settings.bitrate_kbps
        self._active_bitrate = self.settings.bitrate_kbps
        self._relay_active = False
        self._last_governor_ns = 0
        self._last_recovery_ns = 0
        #: Consecutive delay checks the worst client has reported a queue.
        self._queue_ticks = 0
        self._last_queue_ns = 0
        self._worst_queue_ms = 0.0
        #: Consecutive delay checks with the path clean enough to drop parity.
        self._fec_clean_ticks = 0

        #: Loss over the last sampling interval, and the counters it was
        #: differenced from. **One producer, many readers**: both the parity
        #: switch and the bitrate governor read this, and a differencing
        #: measurement that consumed its own state would give whichever
        #: called first the real number and the other a zero -- the same shape
        #: as the config-push flag documented in CLAUDE.md.
        self._recent_loss = 0.0
        self._loss_prev: dict[str, tuple[int, int]] = {}

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
            stun_servers=cfg.stun_servers,
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

        # Ahead of the loss gate below, and on a faster clock: the point of a
        # delay signal is to act before the loss it predicts, and it cannot do
        # that from behind a five-second interval.
        self._tick_queue(now)

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
            restored = min(int(self._active_bitrate * _RECOVERY_STEP) + 1, target)
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

    def _tick_queue(self, now: int) -> None:
        """Reduce bitrate on a standing queue, before it turns into loss."""
        if now - self._last_queue_ns < _QUEUE_INTERVAL_NS:
            return
        self._last_queue_ns = now

        # Sampled here, once, before anything reads it.
        self._sample_loss()
        self._tick_fec()

        queue = self._worst_queue()
        if queue <= _QUEUE_THRESHOLD_MS:
            self._queue_ticks = 0
            return

        self._queue_ticks += 1
        if self._queue_ticks < _QUEUE_CONFIRM_TICKS:
            return
        # Re-confirm before cutting again, so a persistent queue steps down
        # rather than collapsing to the floor in a couple of seconds.
        self._queue_ticks = 0

        reduced = max(int(self._active_bitrate * _QUEUE_BACKOFF), _BITRATE_FLOOR_KBPS)
        if reduced >= self._active_bitrate:
            return
        log.info(
            "Queue at %.0f ms; reducing bitrate %d -> %d kbps before loss starts",
            queue, self._active_bitrate, reduced,
        )
        self._apply_bitrate(reduced)
        # Hold off the recovery ramp: the path has just told us it is full.
        self._last_recovery_ns = now

    def _tick_fec(self) -> None:
        """Turn parity on when the path is losing, off when it stops.

        Adaptive rather than always-on because parity is not free: one extra
        slice per frame is roughly 9% of the bitrate at these settings, which
        on a clean path buys nothing at all. And not always-off because the
        measured cost of loss is far worse than 9% -- see `_FEC_ON_LOSS`.
        """
        loss = self._recent_loss
        if loss >= _FEC_ON_LOSS:
            self._fec_clean_ticks = 0
            if self.net.set_fec(True):
                log.info(
                    "Loss at %.2f%%; adding a parity slice per frame", loss * 100
                )
            return

        if not self.net.fec_enabled:
            return
        if loss > _FEC_OFF_LOSS:
            self._fec_clean_ticks = 0
            return

        self._fec_clean_ticks += 1
        if self._fec_clean_ticks >= _FEC_OFF_TICKS:
            self._fec_clean_ticks = 0
            if self.net.set_fec(False):
                log.info("Path clean; dropping the parity slice")

    def _worst_queue(self) -> float:
        """Largest standing queue any client is reporting, in milliseconds.

        Clients that cannot report it are skipped rather than counted as zero:
        an older peer sends a shorter report, and treating "did not say" as
        "no queue" would let one stale client mask a real one.
        """
        worst = 0.0
        for entry in self.net.client_snapshot():
            report = entry.get("report") or {}
            if not report.get("queue_reported"):
                continue
            worst = max(worst, float(report.get("queue_ms", 0.0) or 0.0))
        self._worst_queue_ms = worst
        return worst

    def _sample_loss(self) -> float:
        """Loss since the last sample, per client, worst of them.

        **Differenced, not cumulative.** The counters a client reports are
        lifetime totals, and dividing them gave the loss rate *since the
        session began* -- which is not a control signal. Once a session had
        one bad patch the ratio stayed elevated for good: measured directly,
        the parity slice switched on correctly when loss appeared and then
        never switched off again, twenty-five seconds after the path was
        completely clean. The same reading feeds the bitrate governor, which
        would equally have held a reduced bitrate on a path that had long
        since recovered.

        A client whose counters went backwards has reconnected and started
        again; its window is skipped rather than producing a negative rate.
        """
        worst = 0.0
        seen: set[str] = set()
        for entry in self.net.client_snapshot():
            report = entry.get("report") or {}
            client_id = str(entry.get("client_id", ""))
            seen.add(client_id)
            received = int(report.get("slices_received", 0) or 0)
            lost = int(report.get("slices_lost", 0) or 0)

            previous = self._loss_prev.get(client_id)
            self._loss_prev[client_id] = (received, lost)
            if previous is None:
                continue
            delta_received = received - previous[0]
            delta_lost = lost - previous[1]
            if delta_received < 0 or delta_lost < 0:
                continue                      # reconnected; counters restarted
            total = delta_received + delta_lost
            if total > 100:      # too small a window to mean anything
                worst = max(worst, delta_lost / total)

        # Departed clients must not accumulate for the life of the process.
        for gone in set(self._loss_prev) - seen:
            del self._loss_prev[gone]

        self._recent_loss = worst
        return worst

    def _worst_loss(self) -> float:
        """Loss over the most recent sample. See :meth:`_sample_loss`."""
        return self._recent_loss

    def _apply_bitrate(self, kbps: int) -> None:
        if kbps == self._active_bitrate:
            return
        self._active_bitrate = kbps
        adjusted = VideoSettings(**{**self.settings.to_dict(), "bitrate_kbps": kbps})
        with self._lock:
            self.settings = adjusted
        if self._running:
            # **Encoders only, never the capture device.** `apply_config` says
            # this outright -- "a bitrate change, which the governor makes on
            # its own, repeatedly, must never cost a device reopen" -- and then
            # this path called `_restart_media`, which reopens it.
            #
            # It is the governor's own action, so it happens most during
            # congestion, exactly when the stream can least afford a gap: each
            # step closed and reopened the capture card. Measured on hardware,
            # one congested minute produced eight reductions and therefore
            # eight reopens. A reopen can also fail outright if the previous
            # capture has not released the device yet, which turns a bitrate
            # adjustment into a dead stream.
            self._restart_encoders()

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
        capture_stats = capture.snapshot() if capture is not None else {}
        # Fan-out has been measured since this class was written and has
        # never been visible anywhere but the standalone GUI's own
        # snapshot, so an operator driving the source from the Bluetooth
        # server could not see the one source-side cost that is pure
        # deliberate delay -- the sender paces a frame across part of its
        # own interval. A measurement nobody can read is not a diagnostic.
        fanout_stats = self.net.fanout.snapshot()
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
            # What the delay-based half of the governor is seeing.
            "queue_ms": round(self._worst_queue_ms, 1),
            "fec_enabled": self.net.fec_enabled,
            "clients": self.net.client_count,
            "frames_encoded": encoder.frames_encoded if encoder is not None else 0,
            "encode_p50_ms": encode_stats.get("p50", 0.0),
            "encode_p99_ms": encode_stats.get("p99", 0.0),
            "fanout_p50_ms": fanout_stats.get("p50", 0.0),
            "fanout_p99_ms": fanout_stats.get("p99", 0.0),
            "pickup_p50_ms": capture_stats.get("pickup_ms", {}).get("p50", 0.0),
            "frames_superseded": capture_stats.get("frames_superseded", 0),
            # What the card actually negotiated, which the requested
            # width/height/fps do not tell anyone.
            "source_format": capture_stats.get("source_format", ""),
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
            "audio_capture": (
                self._audio_capture.snapshot() if self._audio_capture else {}
            ),
            "devices": self.devices,
            "available_encoders": available_encoders(),
            "cfg_seq": self.cfg_seq,
        }
