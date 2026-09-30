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
from videoserver.playervision.service import PlayerVisionService, resolve_backend
from videoserver.playervision.types import Detection, PlayerHint
from videoserver.playervision.worker import MAX_CONSECUTIVE_FAILURES, VisionWorker

from tests.playervision_fakes import BrightBoxBackend, registered

W, H = 320, 180


@pytest.fixture(autouse=True)
def _stand_in_detector():
    """The tests' own detector, registered for every test here.

    There is no model-free backend in the product any more, and nothing in
    this file is about detection quality -- it is about Off, the ladder, the
    worker's failure handling and the plumbing, all of which need *a* backend.
    """
    with registered(BrightBoxBackend):
        yield


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
        player_id_enabled=True, player_id_backend="auto", **kwargs
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
    def test_auto_tries_a_registered_backend_first(self):
        """How the tests drive the chain without a model."""
        backend, caps = resolve_backend("auto")
        assert caps.available is True
        assert backend.name == "brightbox"

    def test_an_explicit_request_is_honoured(self):
        backend, caps = resolve_backend("brightbox")
        assert backend.name == "brightbox"
        assert caps.available is True

    def test_without_a_model_auto_says_so_rather_than_guessing(self, monkeypatch):
        """There is no model-free fallback. A machine with no model reports
        identification unavailable, with the reason, rather than running a
        detector that labels HUD icons and other karts as the player."""
        import videoserver.playervision.service as module

        monkeypatch.setattr(module, "_REGISTERED", {})
        monkeypatch.setenv("RBGC_PLAYERVISION_MODELS", "/nonexistent/rbgc-models")
        backend, caps = resolve_backend("auto")
        assert caps.available is False
        assert isinstance(backend, NullBackend)
        assert caps.reason
        assert "heuristic" not in caps.reason

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
        caps = Capabilities(backend="onnx", available=True, device="CPU")
        text = " ".join(caps.describe())
        assert "onnx" in text and "CPU" in text


class TestTheNoModelBackendIsGone:
    """Removed on purpose: it found what moved, and in a chase-camera game the
    player is the one thing that does not move in its own viewport."""

    def test_it_cannot_be_imported(self):
        with pytest.raises(ImportError):
            import videoserver.playervision.backends.heuristic  # noqa: F401

    @pytest.mark.parametrize("saved", ["heuristic", "torch", "banana", ""])
    def test_a_saved_choice_that_no_longer_exists_becomes_auto(self, saved):
        """A config written while it existed must not name a backend nothing
        can load. `torch` was accepted and never implemented."""
        assert VideoSettings(player_id_backend=saved).clamped().player_id_backend == "auto"

    def test_the_model_is_still_a_valid_request(self):
        assert VideoSettings(player_id_backend="onnx").clamped().player_id_backend == "onnx"


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
        worker = VisionWorker(BrightBoxBackend())
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
        worker = VisionWorker(BrightBoxBackend())
        worker.configure(layout=QUAD_4, hints=(PlayerHint(3, ("lower_left",)),))
        worker.configure(layout=FULL)
        assert worker.snapshot()["players"] == 1

    def test_a_departed_player_is_forgotten(self):
        """Otherwise the first thing that happens when somebody new takes
        their adapter is that they are mistaken for them."""
        worker = VisionWorker(BrightBoxBackend())
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


class TestTheSampleSize:
    """How wide a frame is reduced to before a backend sees it.

    It was a hard 320, so a 640-input detector got a quarter of the detail
    and then paid full price to fake it back.
    """

    def test_it_falls_back_to_the_old_constant(self):
        from videoserver.playervision.service import SAMPLE_WIDTH

        assert PlayerVisionService().sample_width() == SAMPLE_WIDTH

    def test_a_backend_can_ask_for_more(self):
        """A class attribute, because for an isolated backend the instance
        held here is never started -- there is nothing else to read."""

        class Hungry(BrightBoxBackend):
            wants_width = 640

        service = PlayerVisionService()
        service._backend = Hungry()
        assert service.sample_width() == 640

    def test_the_loaded_model_beats_the_class_attribute(self):
        """It comes from the file the operator actually supplied."""

        class Hungry(BrightBoxBackend):
            wants_width = 640

        service = PlayerVisionService()
        service._backend = Hungry()
        service._caps = Capabilities(input_width=416, input_height=416)
        assert service.sample_width() == 416

    def test_it_is_clamped_to_what_the_slot_can_carry(self):
        """Against the *same* constant the shared-memory slot is sized from,
        so an oversized frame cannot happen by construction -- a refused
        write moves only `oversized` while everything else reads healthy."""
        from videoserver.playervision.shm import MAX_SAMPLE_WIDTH

        service = PlayerVisionService()
        service._caps = Capabilities(input_width=99999)
        assert service.sample_width() == MAX_SAMPLE_WIDTH

    def test_a_nonsense_size_does_not_produce_a_nonsense_frame(self):
        service = PlayerVisionService()
        service._caps = Capabilities(input_width=-5)
        assert service.sample_width() >= 2

    def test_a_capture_narrower_than_asked_for_is_not_upscaled(self):
        """Upscaling in swscale invents pixels and costs time; the backend
        letterboxes what it is given."""
        av = pytest.importorskip("av")

        class Hungry(BrightBoxBackend):
            wants_width = 640

        service = PlayerVisionService()
        service._backend = Hungry()
        sample = service._to_sample(av.VideoFrame(320, 180, "yuv420p"), 1)
        assert sample is not None and sample.width <= 320


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
        assert report["available"] is True
        # A **string**, from the capabilities. This test used to assert
        # `report["backend"]["backend"]`, which pinned a bug as a
        # requirement: the runner's snapshot was overwriting the capability
        # name with the child's nested backend dict, and the web GUI would
        # have rendered `Running [object Object]`.
        assert report["backend"] == "brightbox"

    def test_the_status_block_is_small_enough_to_send(self):
        """It rides VIDEO_STATUS, which refuses whole over 1200 bytes.

        The detail lives in `debug_snapshot` and goes on the slow message --
        see `tests/test_split_pipeline.py` for the sizes that made that
        necessary."""
        service = PlayerVisionService()
        service._ensure_worker(_on())
        block = service.snapshot()
        for key in ("identity", "tracks", "slot_reads", "slot_torn", "skipped"):
            assert key not in block, f"{key} belongs on the slow message"

    def test_the_debug_snapshot_keeps_everything(self):
        """Nothing was lost when the status was slimmed -- it moved."""
        service = PlayerVisionService()
        service._ensure_worker(_on())
        detail = service.debug_snapshot()
        for key in ("identity", "tracks", "skipped", "backend", "running"):
            assert key in detail, f"{key} vanished rather than moving"

    def test_the_debug_snapshot_is_empty_when_nothing_runs(self):
        assert PlayerVisionService().debug_snapshot()["running"] is False
