"""The backend ladder, the worker's failure handling, and Off.

The most important test here is the dullest: with the feature off, nothing is
constructed. No backend, no worker, no reformatter, no model. That is the
claim this feature is judged on, and it is the one a later refactor is most
likely to break without noticing, because everything still *works* when it
breaks -- it just stops being free.
"""

from __future__ import annotations

import pytest

from common.screen_regions import FULL, QUAD_4, Rect
from common.video import VideoSettings

from videoserver.playervision.backends.base import (
    Capabilities,
    SampleFrame,
    NullBackend,
    PlayerVisionBackend,
)
from videoserver.playervision.backends.heuristic import (
    BACKGROUND_SHIFT,
    CELL,
    HeuristicBackend,
)
from videoserver.playervision.service import PlayerVisionService, resolve_backend
from videoserver.playervision.types import Detection, PlayerHint
from videoserver.playervision.worker import MAX_CONSECUTIVE_FAILURES, VisionWorker

W, H = 320, 180


def gray(square=None, *, bg=40, fg=220):
    """A gray frame, optionally with one bright square (x, y, size)."""
    buf = bytearray([bg]) * (W * H)
    if square:
        x, y, size = square
        for row in range(y, min(H, y + size)):
            base = row * W
            for col in range(x, min(W, x + size)):
                buf[base + col] = fg
    return SampleFrame(memoryview(bytes(buf)), W, H, W)


def _on(**kwargs):
    return VideoSettings(
        player_id_enabled=True, player_id_backend="heuristic", **kwargs
    )


class TestOffIsTheOriginalPath:
    def test_the_feature_off_constructs_nothing(self):
        """Not 'produces no labels' -- constructs nothing. No backend, no
        worker, no reformatter, no model."""
        service = PlayerVisionService()
        assert service.sample(object(), VideoSettings(), True, 1) is None
        assert service.running is False
        assert service._backend is None
        assert service._reformatter is None

    def test_consent_withheld_constructs_nothing(self):
        """The capture machine's own switch. 'Put a model on your GPU' is the
        decision of whoever owns the GPU."""
        service = PlayerVisionService()
        assert service.sample(object(), _on(), False, 1) is None
        assert service.running is False
        assert service._backend is None

    def test_both_switches_are_needed(self):
        service = PlayerVisionService()
        assert service.due(VideoSettings(), True, 1) is False
        assert service.due(_on(), False, 1) is False
        assert service.due(_on(), True, 1) is True

    def test_due_is_the_cheap_question(self):
        """Asked before the frame lock is taken, so a tick that is not due
        costs nothing at all."""
        service = PlayerVisionService()
        settings = _on(player_id_hz=6.0)
        assert service.due(settings, True, 1_000_000_000) is True
        service.sample(None, settings, True, 1_000_000_000)
        # 10 ms later, nowhere near due at 6 Hz.
        assert service.due(settings, True, 1_010_000_000) is False


class TestNullBackend:
    def test_detect_raises_rather_than_returning_nothing(self):
        """Not a pass-through and must never become one. A polite null that
        returned no detections would make Off untestable."""
        with pytest.raises(AssertionError, match="bypass the detection path"):
            NullBackend().detect(gray())

    def test_it_reports_itself_unavailable_with_a_reason(self):
        caps = NullBackend.probe()
        assert caps.available is False
        assert caps.reason

    def test_stop_is_safe_twice(self):
        backend = NullBackend()
        backend.stop()
        backend.stop()


class TestBackendLadder:
    def test_auto_finds_the_no_model_backend(self):
        """It is always available, which is what makes the whole chain
        demonstrable with no GPU and no models."""
        backend, caps = resolve_backend("auto")
        assert caps.available is True
        assert backend.name in ("onnx", "heuristic")

    def test_an_explicit_request_is_honoured(self):
        backend, caps = resolve_backend("heuristic")
        assert backend.name == "heuristic"
        assert caps.available is True

    def test_an_unknown_backend_is_refused_with_a_reason(self):
        """Never silently downgraded: a mode that quietly fell back is
        indistinguishable from one that is working."""
        backend, caps = resolve_backend("banana")
        assert isinstance(backend, NullBackend)
        assert caps.available is False
        assert "banana" in caps.reason

    def test_an_unavailable_request_does_not_fall_back(self):
        """Asking for onnx on a machine without it must say so, not quietly
        run the weak backend while reporting success."""
        _, caps = resolve_backend("none")
        assert caps.available is False

    def test_a_probe_that_raises_does_not_escape(self):
        class Exploding(PlayerVisionBackend):
            name = "exploding"

            @classmethod
            def probe(cls):
                raise RuntimeError("no")

        import videoserver.playervision.service as module

        original = module._backend_class
        module._backend_class = lambda name: Exploding if name == "x" else None
        try:
            backend, caps = resolve_backend("x")
        finally:
            module._backend_class = original
        assert isinstance(backend, NullBackend)
        assert caps.available is False

    def test_capabilities_describe_themselves_for_a_log(self):
        caps = Capabilities(backend="heuristic", available=True, device="CPU")
        text = " ".join(caps.describe())
        assert "heuristic" in text and "CPU" in text


class TestHeuristicBackend:
    def test_the_first_frame_reports_nothing(self):
        """Nothing to compare against; calling the whole grid foreground would
        be far worse than reporting nothing."""
        backend = HeuristicBackend()
        backend.start()
        assert backend.detect(gray((50, 50, 30))) == []

    def test_it_finds_something_that_is_not_the_background(self):
        backend = HeuristicBackend()
        backend.start()
        backend.detect(gray())
        found = backend.detect(gray((50, 50, 30)))
        assert len(found) == 1
        assert 0.0 <= found[0].box.x < 1.0

    def test_one_blob_per_entity_not_two(self):
        """The regression that background subtraction exists for. Differencing
        consecutive frames lights up both where an entity left and where it
        arrived, so one moving character reads as two short-lived ones and no
        track ever lives long enough to own a viewport."""
        backend = HeuristicBackend()
        backend.start()
        backend.detect(gray())
        for step in range(5):
            found = backend.detect(gray((40 + step * 14, 50, 30)))
        assert len(found) == 1

    def test_a_scene_wide_flash_is_not_an_entity(self):
        """The camera panned, or the scene cut. Handing a viewport's identity
        to the background would be the worst available answer."""
        backend = HeuristicBackend()
        backend.start()
        backend.detect(gray())
        assert backend.detect(gray(bg=250)) == []

    def test_a_still_entity_fades_into_the_background(self):
        """The documented limitation, pinned so it is a known property rather
        than a surprise. Continuity is what carries a player through it."""
        backend = HeuristicBackend()
        backend.start()
        backend.detect(gray())
        still = gray((50, 50, 30))
        for _ in range(4 << BACKGROUND_SHIFT):
            found = backend.detect(still)
        assert found == []

    def test_it_reports_no_appearance_vectors(self):
        """Identity then has viewport ownership, continuity and controller
        correlation, and nothing else -- which is enough for a split screen
        and nothing at all for appearance matching."""
        assert HeuristicBackend.embeddings is False
        assert HeuristicBackend.probe().embeddings is False

    def test_it_needs_no_subprocess(self):
        """Nothing here can fault a GPU driver, so there is nothing to isolate."""
        assert HeuristicBackend.isolated is False

    def test_a_degenerate_frame_is_not_an_error(self):
        backend = HeuristicBackend()
        backend.start()
        assert backend.detect(SampleFrame(memoryview(b""), 0, 0, 0)) == []

    def test_a_resized_capture_starts_again_rather_than_comparing_nonsense(self):
        backend = HeuristicBackend()
        backend.start()
        backend.detect(gray())
        backend.detect(gray((50, 50, 30)))
        small = SampleFrame(memoryview(bytes(bytearray([40]) * (160 * 90))), 160, 90, 160)
        assert backend.detect(small) == []


class _Boom(PlayerVisionBackend):
    name = "boom"

    def start(self):
        return Capabilities(backend=self.name, available=True)

    def detect(self, frame):
        raise RuntimeError("the model fell over")

    def stop(self):
        return None


class TestWorkerFailure:
    def test_a_backend_that_raises_does_not_propagate(self):
        """The caller is the video server, and the whole promise is that this
        cannot disturb the stream."""
        worker = VisionWorker(_Boom())
        assert worker.process(gray(), 1) == []
        assert worker.failures == 1

    def test_it_gives_up_rather_than_failing_forever(self):
        """A backend failing every frame is not having a bad day. Retrying it
        would spend the machine's time producing nothing while every counter
        read healthy."""
        worker = VisionWorker(_Boom())
        for index in range(MAX_CONSECUTIVE_FAILURES + 2):
            worker.process(gray(), index + 1)
        assert worker.failed
        assert worker.snapshot()["failed"]

    def test_a_run_of_successes_clears_the_count(self):
        class Flaky(PlayerVisionBackend):
            name = "flaky"

            def __init__(self):
                self.calls = 0

            def start(self):
                return Capabilities(backend=self.name, available=True)

            def detect(self, frame):
                self.calls += 1
                if self.calls % 3:
                    return []
                raise RuntimeError("intermittent")

            def stop(self):
                return None

        worker = VisionWorker(Flaky())
        for index in range(30):
            worker.process(gray(), index + 1)
        assert not worker.failed, "an intermittent fault switched the feature off"


class TestWorkerConfiguration:
    def test_a_layout_change_does_not_drop_continuity(self):
        """A player is the same player whether the picture is split two ways
        or four, and every track's region is recomputed anyway."""
        worker = VisionWorker(HeuristicBackend())
        worker.configure(layout=QUAD_4, hints=(PlayerHint(1, ("upper_left",)),))
        worker._backend.start()
        worker.process(gray(), 1)
        for step in range(6):
            worker.process(gray((20 + step * 14, 20, 30)), (step + 2) * 100_000_000)
        before = worker.snapshot()["tracks"]["live"]
        worker.configure(layout=FULL)
        assert worker.snapshot()["tracks"]["live"] == before

    def test_naming_one_field_does_not_reset_the_others(self):
        """The layout, the roster and the input traces arrive on three
        different messages at three different rates."""
        worker = VisionWorker(HeuristicBackend())
        worker.configure(layout=QUAD_4, hints=(PlayerHint(3, ("lower_left",)),))
        worker.configure(layout=FULL)
        assert worker.snapshot()["players"] == 1

    def test_a_departed_player_is_forgotten(self):
        """Otherwise the first thing that happens when somebody new takes
        their adapter is that they are mistaken for them."""
        worker = VisionWorker(HeuristicBackend())
        worker.configure(hints=(PlayerHint(1), PlayerHint(2)))
        worker._identity.gallery(1).add((1.0, 0.0), 1.0)
        worker._identity.gallery(2).add((0.0, 1.0), 1.0)
        worker.configure(hints=(PlayerHint(2),))
        worker.forget_absent_players()
        assert "1" not in worker.snapshot()["identity"]["exemplars"]


class TestServiceConfiguration:
    def test_the_roster_survives_arriving_before_the_first_frame(self):
        """The normal order: the Bluetooth server pushes the player map on its
        own periodic message, which routinely lands before any frame. A
        service that only forwarded to a live worker dropped it and then
        identified nobody, with every counter healthy."""
        service = PlayerVisionService()
        service.configure(layout=QUAD_4, hints=(PlayerHint(1, ("upper_left",)),))
        runner = service._ensure_worker(_on())
        assert runner is not None
        assert runner.snapshot()["players"] == 1
        assert runner.snapshot()["layout"] == QUAD_4

    def test_the_roster_survives_a_stop(self):
        """It is what we were *told*, not what we worked out, and the
        Bluetooth server re-pushes only on its slow cadence."""
        service = PlayerVisionService()
        service.configure(layout=QUAD_4, hints=(PlayerHint(2, ("upper_right",)),))
        service._ensure_worker(_on())
        service.stop()
        runner = service._ensure_worker(_on())
        assert runner.snapshot()["players"] == 1

    def test_moving_the_threshold_does_not_throw_away_the_galleries(self):
        """Rebuilding would lose every exemplar -- and, for a model backend,
        reload the model -- for a number the identity manager can be told."""
        service = PlayerVisionService()
        runner = service._ensure_worker(_on(player_id_confidence=0.6))
        runner._worker._identity.gallery(1).add((1.0, 0.0), 1.0)
        again = service._ensure_worker(_on(player_id_confidence=0.7))
        assert again is runner, "the runner was rebuilt for a threshold change"
        assert len(again._worker._identity.gallery(1)) == 1

    def test_changing_the_backend_does_rebuild(self):
        service = PlayerVisionService()
        first = service._ensure_worker(_on())
        second = service._ensure_worker(
            VideoSettings(player_id_enabled=True, player_id_backend="none")
        )
        assert second is None
        assert service.capabilities.available is False

    def test_stop_is_safe_twice(self):
        service = PlayerVisionService()
        service._ensure_worker(_on())
        service.stop()
        service.stop()
        assert service.running is False


class TestConsentWithdrawn:
    def test_a_running_backend_is_released_when_the_machine_says_no(self):
        """The *other* switch. It reaches the pipeline by a different route
        from the operator's, and without this a model stays in memory doing
        nothing for the life of the process."""
        pytest.importorskip("av")
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp

        cfg = VideoServerConfig(
            password="x" * 8, media_port=0, playervision_allowed=True,
            settings=_on(player_id_hz=1000.0),
        )
        app = VideoServerApp(cfg)
        assert app._players._ensure_worker(app.settings) is not None
        assert app._players.running is True

        cfg.playervision_allowed = False
        app.sample_vision()
        assert app._players.running is False, "the backend stayed loaded"


class TestServiceFrames:
    def test_a_bad_frame_is_skipped_not_raised(self):
        service = PlayerVisionService()
        settings = _on(player_id_hz=1000.0)
        assert service.sample(object(), settings, True, 1_000_000_000) is None
        assert service.skipped >= 1

    def test_none_means_we_did_not_look(self):
        """Distinct from an empty list, which means we looked and there is
        nobody -- and which is what takes a departed player's label away."""
        service = PlayerVisionService()
        settings = _on(player_id_hz=6.0)
        service.sample(None, settings, True, 1_000_000_000)
        assert service.sample(None, settings, True, 1_001_000_000) is None

    def test_it_owns_one_reformatter_and_reuses_it(self):
        """``frame.reformat()`` runs through a scaler cached **on the frame**,
        and one ``CapturedFrame`` goes to the encoder and both previews at
        once; two threads inside that cached scaler wedge one of them
        permanently, with no exception and nothing logged. This is the fourth
        consumer of that frame.

        Pinned as behaviour rather than by grepping the source, which cannot
        tell an intention in a docstring from what the code does -- a mistake
        this project has already made once and written down.

        Reuse is half the property: a reformatter built per frame would be
        safe but would rebuild a scaler on every sample.
        """
        av = pytest.importorskip("av")
        from av.video.reformatter import VideoReformatter

        service = PlayerVisionService()
        settings = _on(player_id_hz=1000.0)
        frame = av.VideoFrame(64, 64, "yuv420p")

        service.sample(frame, settings, True, 1_000_000_000)
        first = service._reformatter
        assert isinstance(first, VideoReformatter), "not our own scaler"

        service.sample(frame, settings, True, 2_000_000_000)
        assert service._reformatter is first, "a scaler was rebuilt per frame"


class TestSnapshot:
    def test_it_reports_unavailable_before_anything_runs(self):
        assert PlayerVisionService().snapshot()["running"] is False

    def test_it_reports_what_is_running(self):
        service = PlayerVisionService()
        service._ensure_worker(_on())
        report = service.snapshot()
        assert report["running"] is True
        assert report["backend"]["backend"] == "heuristic"
        assert report["available"] is True
