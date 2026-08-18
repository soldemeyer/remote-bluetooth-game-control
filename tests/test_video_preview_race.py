"""Three threads, one shared frame.

``VideoCapture._publish`` puts a single ``CapturedFrame`` into both ``latest``
and the encoder's queue, so **the same PyAV frame object** is reformatted by
the encoder thread, by the GUI's preview and by the responder's web preview.
The second preview appears the moment a Bluetooth server connects, which is
what made this look like a networking fault.

``frame.reformat()`` routes all of them through a reformatter cached on the
frame. Two threads inside it do not raise -- one **wedges and never returns**
while the other runs on untouched. That is a video server window Windows
offers to close, or a web preview frozen on one picture, with nothing logged
either way.

The fix is that each consumer owns its ``VideoReformatter``, which is what
covers the encoder thread as well; a lock around the two previews alone never
could, because the encoder cannot go behind it without dragging preview work
onto the hot path.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("av", reason="video extras not installed")


class _CountingEncoder:
    """Stands in for PreviewEncoder, recording concurrent entries."""

    def __init__(self, width: int, tracker: "_Overlap") -> None:
        self._width = width
        self._tracker = tracker
        self.calls = 0

    def encode(self, frame) -> bytes:
        with self._tracker.enter():
            self.calls += 1
            # Real work on the shared frame, so a genuine race would show.
            frame.reformat(
                width=self._width, height=self._width * 9 // 16, format="yuvj420p"
            )
            return b"\xff\xd8jpeg"


class _Overlap:
    """Records the maximum number of threads inside encode() at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def enter(self):
        tracker = self

        class _Ctx:
            def __enter__(self):
                with tracker._lock:
                    tracker.current += 1
                    tracker.peak = max(tracker.peak, tracker.current)
                # Widen the window a real encode would occupy.
                time.sleep(0.002)

            def __exit__(self, *exc):
                with tracker._lock:
                    tracker.current -= 1

        return _Ctx()


class _FakeApp:
    """Just the preview half of VideoServerApp, with the real lock and method."""

    def __init__(self, frame) -> None:
        self._preview_lock = threading.Lock()
        self._frame = frame

    def latest_capture(self):
        return type("Captured", (), {"frame": self._frame, "capture_ts": 0})()

    # The method under test, verbatim in behaviour.
    encode_preview = None  # bound below


@pytest.fixture()
def app():
    from av.video.frame import VideoFrame

    from videoserver.pipeline import VideoServerApp

    fake = _FakeApp(VideoFrame(640, 360, "yuv420p"))
    # Borrow the real implementation rather than restating it.
    _FakeApp.encode_preview = VideoServerApp.encode_preview
    return fake


class TestPreviewEncodingIsSerialised:
    def test_two_threads_never_encode_at_once(self, app):
        """The fix: whatever the callers do, only one is inside at a time."""
        tracker = _Overlap()
        encoders = [_CountingEncoder(w, tracker) for w in (320, 640)]

        def worker(encoder):
            for _ in range(20):
                app.encode_preview(encoder)

        threads = [threading.Thread(target=worker, args=(e,)) for e in encoders]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), "a preview thread wedged"
        assert tracker.peak == 1, (
            f"{tracker.peak} threads reformatted the shared frame at once; "
            "this is the wedge that froze the video server"
        )
        assert all(e.calls == 20 for e in encoders)

    def test_it_returns_both_the_jpeg_and_the_frame_it_came_from(self, app):
        """The caller needs capture_ts to stamp the slice it sends."""
        jpeg, captured = app.encode_preview(_CountingEncoder(320, _Overlap()))
        assert jpeg == b"\xff\xd8jpeg"
        assert captured is not None

    def test_no_frame_yet_is_not_an_error(self, app):
        app.latest_capture = lambda: None
        assert app.encode_preview(_CountingEncoder(320, _Overlap())) == (None, None)


class TestConsumersOwnTheirScaler:
    """The actual fix: no reformatter is shared, so nothing can wedge."""

    def test_the_real_consumers_survive_sharing_one_frame(self):
        """The whole hazard, end to end, with the classes that ship.

        Two PreviewEncoders and a stand-in for the encoder thread's scaling
        step, all hammering the single object capture publishes to every one of
        them. Before the fix this deadlocked; the assertion is simply that
        every thread came back.
        """
        from av.video.frame import VideoFrame
        from av.video.reformatter import VideoReformatter

        from videoserver.preview import PreviewEncoder

        frame = VideoFrame(1280, 720, "yuv420p")
        done: dict[str, int] = {"gui": 0, "web": 0, "encoder": 0}

        def preview(name: str, width: int) -> None:
            encoder = PreviewEncoder(width=width)
            for _ in range(60):
                if encoder.encode(frame):
                    done[name] += 1

        def encoder_thread() -> None:
            # Mirrors VideoEncoder._prepare: its own reformatter, scaling the
            # same frame to the encode size.
            scaler = VideoReformatter()
            for _ in range(60):
                scaler.reformat(frame, width=960, height=540, format="yuv420p")
                done["encoder"] += 1

        threads = [
            threading.Thread(target=preview, args=("gui", 640), daemon=True),
            threading.Thread(target=preview, args=("web", 320), daemon=True),
            threading.Thread(target=encoder_thread, daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not any(t.is_alive() for t in threads), (
            f"a consumer wedged on the shared frame; progress was {done}"
        )
        assert all(count == 60 for count in done.values()), done

    def test_two_owned_scalers_never_wedge(self):
        from av.video.frame import VideoFrame
        from av.video.reformatter import VideoReformatter

        frame = VideoFrame(1280, 720, "yuv420p")

        def hammer(width: int) -> None:
            scaler = VideoReformatter()          # one per thread
            for _ in range(400):
                scaler.reformat(
                    frame, width=width, height=width * 9 // 16, format="yuvj420p"
                )

        threads = [
            threading.Thread(target=hammer, args=(w,), daemon=True)
            for w in (320, 640)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert not any(t.is_alive() for t in threads), (
            "owning a reformatter was not sufficient; re-examine the fix"
        )


class TestTheUnderlyingHazardIsReal:
    """Guards the reason all of this exists, so it is not 'simplified' away."""

    def test_concurrent_reformat_of_one_frame_wedges(self):
        from av.video.frame import VideoFrame

        frame = VideoFrame(1280, 720, "yuv420p")
        progress = {320: 0, 640: 0}

        def hammer(width: int) -> None:
            for i in range(400):
                frame.reformat(
                    width=width, height=width * 9 // 16, format="yuvj420p"
                )
                progress[width] = i + 1

        threads = [
            threading.Thread(target=hammer, args=(w,), daemon=True) for w in progress
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=8)

        wedged = [t for t in threads if t.is_alive()]
        if not wedged:
            pytest.skip(
                "this PyAV/FFmpeg build tolerates it; the lock is still required "
                "on the builds we ship"
            )
        # Reproduced: one thread is stuck for good while the other sailed past.
        assert max(progress.values()) > min(progress.values())
