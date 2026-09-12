"""Three faults that all presented as "the picture is wrong", and none of
which any counter reported.

Reported together: a frozen screen with hardware decoding on, the floating
control bar disappearing whenever an upscaler was selected, and a small blur
every second and a half. They are unrelated to each other, and only the second
is anywhere near the GPU code the report arrived alongside.

What they share is the shape this project keeps meeting: the software says it
is working, every counter agrees, and the picture disagrees.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6", reason="client GUI extras not installed")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


# ==========================================================================
# 1. A decode error must not rebuild a hardware decoder
# ==========================================================================


class Boom(Exception):
    pass


class FakeCodec:
    """Raises on decode, and counts how often it was reset."""

    def __init__(self) -> None:
        self.flushes = 0

    def parse(self, data):
        return [object()]

    def decode(self, packet):
        raise Boom("invalid data")

    def flush_buffers(self):
        self.flushes += 1


class FakeReceiver:
    clock_locked = True
    clock_offset_ns = 0

    def __init__(self, frames) -> None:
        self._frames = list(frames)
        self.idr_requests = 0
        self.decode_stats = None
        from common.timing import LatencyStats

        self.present_stats = LatencyStats()

    def get_frame(self, timeout: float = 0.1):
        return self._frames.pop(0) if self._frames else None

    def request_idr(self, *args, **kwargs):
        self.idr_requests += 1


class Frame:
    data = b"\x00\x00\x00\x01\x41rubbish"
    capture_ts = 0


def run_decode_loop(frames, hw_device=""):
    """Drive the real `_run` over a fixed list of frames, and report what it
    did: the codec it was given, how many it built, and what it asked for."""
    pytest.importorskip("av", reason="video extras not installed")
    from client.media.decoder import VideoDecoder

    receiver = FakeReceiver(frames)
    decoder = VideoDecoder(receiver=receiver)
    decoder._hw_wanted = hw_device
    decoder._hw_device = hw_device

    codec = FakeCodec()
    builds = []

    def build(av_module):
        builds.append(1)
        return codec

    decoder._build_codec = build

    # Stop once the frames run out, rather than blocking on the timeout.
    original = receiver.get_frame

    def draining(timeout: float = 0.1):
        frame = original(timeout)
        if frame is None:
            decoder._stop.set()
        return frame

    receiver.get_frame = draining

    decoder._run()
    return codec, len(builds), receiver.idr_requests


class TestADecodeErrorResetsRatherThanRebuilds:
    """**Rebuilding is what froze the picture under hardware decoding.**

    Measured: building a software decoder costs 0.0 ms and a d3d11va one
    **133 ms median, 179 ms worst** -- eight frames' worth at 60 fps. The loop
    rebuilt on every bad frame, so the replacement was handed another P-frame
    and rebuilt again; escaping needed a keyframe to land in the gap between
    two rebuilds. Intermittent, and it cleared as soon as debug logging slowed
    the loop down, which is the signature of a race rather than a bad stream.
    """

    def test_it_resets_the_decoder(self):
        codec, _, _ = run_decode_loop([Frame(), Frame(), Frame()])
        assert codec.flushes >= 3, "the decoder was not reset after a failure"

    def test_it_does_not_rebuild(self):
        _, builds, _ = run_decode_loop([Frame(), Frame(), Frame()])
        assert builds == 1, (
            f"the decoder was rebuilt {builds} times; on the hardware path "
            "that is 133 ms each and the recovery cannot win the race"
        )

    def test_a_run_of_empty_frames_asks_for_a_keyframe(self):
        """Resetting alone leaves the decoder waiting for the next *periodic*
        keyframe -- up to `gop_s`, two seconds by default -- with the picture
        frozen for all of it. Asking bounds it by a round trip instead."""
        _, _, idrs = run_decode_loop([Frame(), Frame(), Frame(), Frame()])
        assert idrs >= 1, "nothing asked for a keyframe, so recovery waits out the GOP"

    def test_changing_the_device_still_rebuilds(self):
        """The one case a rebuild is genuinely required: a decoder is built
        for one device, so changing it cannot be a reset."""
        pytest.importorskip("av", reason="video extras not installed")
        from client.media.decoder import VideoDecoder

        receiver = FakeReceiver([])
        decoder = VideoDecoder(receiver=receiver)
        builds = []
        decoder._build_codec = lambda av: (builds.append(1), FakeCodec())[1]
        decoder._hw_wanted = "d3d11va"
        decoder._hw_device = ""

        def draining(timeout: float = 0.1):
            decoder._stop.set()
            return None

        receiver.get_frame = draining
        decoder._run()

        assert len(builds) == 2, "a device change must build a new decoder"
        assert receiver.idr_requests >= 1, "a new decoder has no reference chain"


# ==========================================================================
# 2. The control bar has to be woken by a method that exists
# ==========================================================================


class QuietDecoder:
    version = 0
    upscaler_fault = ""
    last_path = ""
    last_path_code = 0

    def set_frame_listener(self, listener):
        pass

    def set_viewport(self, width, height):
        pass

    def set_upscaler(self, upscaler):
        pass

    def set_overlay(self, overlay):
        pass

    def latest(self):
        return None

    def stats(self):
        return {}


class QuietReceiver:
    from common.timing import LatencyStats
    from client.net.video import VideoStreamState

    present_stats = LatencyStats()
    decode_stats = LatencyStats()
    clock_locked = True
    clock_offset_ns = 0
    audio_underruns = 0
    frames_decoded = 0
    idr_requests = 0
    # paintEvent runs the moment the window is shown and reads these, so a
    # thinner stand-in fails on the first repaint rather than on the
    # behaviour under test.
    state = VideoStreamState.STREAMING
    state_detail = ""
    connection_mode = "direct"

    def stats(self):
        return {}


class TestTheControlBarSurvivesTheGpuPath:
    """A native child window draws above every Qt sibling and takes the
    pointer with it, so the stage's event filter on the surface stops firing
    the moment a renderer attaches. The surface forwards the activity instead
    -- and it forwarded it to `note_activity`, which `VideoStage` has never
    had. The `hasattr` guard turned that into silence, so with any upscaler
    selected there was no volume, mute, fullscreen or overlay toggle, and
    nothing to say why.
    """

    def test_the_stage_has_the_method_the_surface_calls(self):
        """Pinned by name, because the guard cannot tell a typo from a
        standalone window with no stage above it."""
        pytest.importorskip("PySide6", reason="client GUI extras not installed")
        from client.gui.shell import VideoStage

        assert callable(getattr(VideoStage, "wake_controls", None))
        assert not hasattr(VideoStage, "note_activity"), (
            "note_activity is back; the surface calls wake_controls"
        )

    def test_activity_on_the_native_child_wakes_the_bar(self, qt_app):
        pytest.importorskip("PySide6", reason="client GUI extras not installed")
        from client.gui.shell import VideoStage
        from client.gui.video_window import VideoWindow

        stage = VideoStage()
        # Shown, because a child of a hidden parent reports isVisible() False
        # however it was asked -- the test would then pass against the broken
        # code as readily as the fixed one.
        stage.resize(640, 360)
        stage.show()
        qt_app.processEvents()
        window = VideoWindow(QuietDecoder(), QuietReceiver(), stage)
        try:
            stage.controls.hide()
            qt_app.processEvents()
            assert not stage.controls.isVisible()

            window._on_surface_activity()
            qt_app.processEvents()

            assert stage.controls.isVisible(), (
                "the bar stayed hidden, which is what the report described"
            )
        finally:
            window.deleteLater()
            stage.close()
            stage.deleteLater()

    def test_the_filter_watches_the_window_the_platform_delivers_to(self, qt_app):
        """**The second half of this bug, and the reason the first fix was not
        enough.**

        `createWindowContainer` returns a placeholder widget that manages
        geometry; the thing on screen is the QWindow inside it, and a native
        child window is what the platform delivers pointer events to. The
        filter was installed on the container alone, so it never saw a real
        pointer -- measured: a MouseMove sent to the container reached it and
        one sent to the window did not.

        Both halves of `NativeSurface` ride on that filter -- waking the bar,
        and forwarding clicks into it -- so the bar neither appeared nor would
        have worked if it had.
        """
        pytest.importorskip("PySide6", reason="client GUI extras not installed")
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QWidget

        from client.gui.video_surface import NativeSurface

        host = QWidget()
        host.resize(400, 300)
        surface = NativeSurface(host)
        host.show()
        qt_app.processEvents()
        try:
            seen = []
            surface.activity.connect(lambda: seen.append(1))

            def move(target):
                seen.clear()
                qt_app.sendEvent(target, QMouseEvent(
                    QEvent.Type.MouseMove, QPointF(5, 5), QPointF(5, 5),
                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier))
                return len(seen)

            assert move(surface._window) == 1, (
                "the QWindow is what the platform delivers to, and the filter "
                "did not see it"
            )
            # The container is still watched: Qt does route through the widget
            # on some paths, and watching both costs one comparison.
            assert move(surface._container) == 1
        finally:
            # **Close, never delete.** Deleting a widget that hosts a
            # `createWindowContainer` QWindow segfaults the interpreter at
            # teardown on this PySide6 -- measured, and it predates this
            # test: the committed surface crashes identically. The app's own
            # path deletes the *surface* (detach_gpu), which is safe; it is
            # deleting the host around it that is not. Same rule CLAUDE.md
            # already records for the client's GUI fixtures.
            surface.release()
            host.close()

    def test_the_whole_chain_from_the_window_to_the_bar(self, qt_app):
        """Pointer on the native child -> activity -> the stage wakes its bar.

        Written as one test on purpose: both links have now been wrong, one
        each time, and each was individually plausible.
        """
        pytest.importorskip("PySide6", reason="client GUI extras not installed")
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from client.gui.shell import VideoStage
        from client.gui.video_surface import NativeSurface
        from client.gui.video_window import VideoWindow

        host = QWidget()
        host.resize(800, 450)
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        stage = VideoStage()
        layout.addWidget(stage)
        window = VideoWindow(QuietDecoder(), QuietReceiver(), stage)
        stage.set_surface(window)
        host.show()
        qt_app.processEvents()
        try:
            surface = NativeSurface(window)
            window._gpu_surface = surface
            surface.activity.connect(window._on_surface_activity)

            stage.controls.hide()
            qt_app.processEvents()
            assert not stage.controls.isVisible()

            qt_app.sendEvent(surface._window, QMouseEvent(
                QEvent.Type.MouseMove, QPointF(5, 5), QPointF(5, 5),
                Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier))
            qt_app.processEvents()

            assert stage.controls.isVisible(), (
                "a pointer over the native child did not reach the bar"
            )
        finally:
            surface.release()
            host.close()

    def test_a_standalone_window_is_unharmed(self, qt_app):
        """No stage above it, so there is nothing to wake -- the guard's real
        purpose, and why it cannot simply be removed."""
        pytest.importorskip("PySide6", reason="client GUI extras not installed")
        from client.gui.video_window import VideoWindow

        window = VideoWindow(QuietDecoder(), QuietReceiver())
        try:
            window._on_surface_activity()
        finally:
            window.deleteLater()


# ==========================================================================
# 3. The VBV has to be deep enough to hold a keyframe
# ==========================================================================


class TestTheVbvCanHoldAKeyframe:
    """At one and a half frames deep the rate controller had nowhere to borrow
    from, so every periodic IDR was coded at a visibly worse quantiser and
    sharpened again over the following frames.

    Measured as the PSNR dip at the keyframe, 1280x720p60 at 8000 kbps:
    **19.4 dB on libx264 and 24.4 dB on h264_nvenc**, falling to -0.2 dB and
    0.6 dB at 24 frames. Reported as "a small blur every second and a half,
    obvious on menus and in split-screen" -- motion masks it, static content
    does not.
    """

    def test_the_buffer_holds_several_frames(self):
        from videoserver.encode import _VBV_FRAMES

        assert _VBV_FRAMES >= 12, (
            "a keyframe is five to ten ordinary frames' worth of bits; a VBV "
            "shallower than that can only pay for it with quantiser"
        )

    def test_it_reaches_the_encoder_options(self):
        pytest.importorskip("av", reason="video extras not installed")
        import av

        from common.video import VideoSettings
        from videoserver.encode import _VBV_FRAMES, configure_low_latency

        settings = VideoSettings(width=1280, height=720, fps=60,
                                 bitrate_kbps=8000, gop_s=2.0)
        ctx = av.CodecContext.create("libx264", "w")
        ctx.width, ctx.height = settings.width, settings.height
        configure_low_latency(ctx, "libx264", settings)

        expected = int(settings.bitrate_kbps * 1000 / settings.fps * _VBV_FRAMES)
        assert int(ctx.options["bufsize"]) == expected
        # The cap itself was never the problem and must stay: without it the
        # stream has no bitrate ceiling at all.
        assert int(ctx.options["maxrate"]) == settings.bitrate_kbps * 1000

    def test_it_scales_with_the_frame_rate(self):
        """Expressed in frames, not seconds, so a 30 fps stream does not get
        twice the burst window a 60 fps one does."""
        pytest.importorskip("av", reason="video extras not installed")
        import av

        from common.video import VideoSettings
        from videoserver.encode import configure_low_latency

        sizes = {}
        for fps in (30, 60):
            settings = VideoSettings(width=1280, height=720, fps=fps,
                                     bitrate_kbps=8000, gop_s=2.0)
            ctx = av.CodecContext.create("libx264", "w")
            ctx.width, ctx.height = settings.width, settings.height
            configure_low_latency(ctx, "libx264", settings)
            sizes[fps] = int(ctx.options["bufsize"])

        assert sizes[30] == sizes[60] * 2, (
            "a frame's worth of bits is twice as big at half the frame rate"
        )
