"""Driving the worker, on this thread and in another process.

The subprocess exists for one reason: a CUDA kernel fault does not raise, it
takes the process down, and in external mode nothing restarts a video server
on somebody else's machine. So the tests that matter here are the violent
ones -- kill the worker and check the caller is undisturbed and the worker
comes back.

Everything else is the contract both runners share: ``submit`` never waits,
configuration survives a restart, and nothing raises at the caller.
"""

from __future__ import annotations

import time

import pytest

from common.screen_regions import QUAD_4, Rect

from videoserver.playervision.backends.base import Capabilities, SampleFrame
from videoserver.playervision.backends.heuristic import HeuristicBackend
from videoserver.playervision.runner import (
    MAX_IMMEDIATE_FAILURES,
    InlineRunner,
    ProcessRunner,
    make_runner,
)
from videoserver.playervision.shm import MAX_PAYLOAD, FrameSlot
from videoserver.playervision.types import PlayerHint
from videoserver.playervision.worker import VisionWorker

W, H = 320, 180


def gray(square=None, *, bg=40, fg=220):
    buf = bytearray([bg]) * (W * H)
    if square:
        x, y, size = square
        for row in range(y, min(H, y + size)):
            base = row * W
            for col in range(x, min(W, x + size)):
                buf[base + col] = fg
    return SampleFrame(memoryview(bytes(buf)), W, H, W)


def wait_for(predicate, timeout=25.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class TestTheSlot:
    """Latest-wins, never a queue. The writer is the video server's control
    thread and must never be made to wait on a busy worker."""

    def test_a_frame_round_trips(self):
        writer = FrameSlot(create=True)
        reader = FrameSlot(writer.name)
        try:
            payload = bytes(range(256)) * 40
            assert writer.write(payload, 64, 40, 256, 3, 12345)
            got = reader.read()
            assert got is not None
            data, width, height, stride, channels, pts = got
            assert (width, height, stride, channels, pts) == (64, 40, 256, 3, 12345)
            assert data == payload
        finally:
            reader.close()
            writer.close()

    def test_the_same_frame_is_not_read_twice(self):
        writer = FrameSlot(create=True)
        reader = FrameSlot(writer.name)
        try:
            writer.write(b"x" * 100, 10, 10, 10, 1, 1)
            assert reader.read() is not None
            assert reader.read() is None
        finally:
            reader.close()
            writer.close()

    def test_a_slow_reader_gets_the_newest_not_a_backlog(self):
        """The whole discipline. Identity does not change in the frames a
        worker skipped, and latency that grows without bound is the failure
        this project keeps having to find."""
        writer = FrameSlot(create=True)
        reader = FrameSlot(writer.name)
        try:
            for pts in range(1, 21):
                writer.write(bytes([pts]) * 100, 10, 10, 10, 1, pts)
            got = reader.read()
            assert got is not None and got[5] == 20
            assert reader.read() is None, "a backlog was kept"
        finally:
            reader.close()
            writer.close()

    def test_an_empty_slot_reads_as_nothing(self):
        writer = FrameSlot(create=True)
        reader = FrameSlot(writer.name)
        try:
            assert reader.read() is None
        finally:
            reader.close()
            writer.close()

    def test_an_oversized_frame_is_refused_and_counted(self):
        """A sizing mistake rather than a transient -- and a slot that
        silently accepted nothing would look like a worker finding nothing."""
        writer = FrameSlot(create=True)
        try:
            assert writer.write(b"x" * 10, 1, 1, MAX_PAYLOAD + 1, 3, 1) is False
            assert writer.snapshot()["oversized"] == 1
        finally:
            writer.close()

    def test_closing_twice_is_safe(self):
        slot = FrameSlot(create=True)
        slot.close()
        slot.close()


class TestChoosingARunner:
    def test_a_cheap_backend_runs_inline(self):
        """Isolating it would buy a process, a segment and a supervisor in
        exchange for nothing, on a feature that is off by default."""
        backend = HeuristicBackend()
        runner = make_runner(backend, VisionWorker(backend))
        assert isinstance(runner, InlineRunner)

    def test_a_model_backend_gets_its_own_process(self):
        class Modelish(HeuristicBackend):
            isolated = True

        backend = Modelish()
        runner = make_runner(backend, VisionWorker(backend))
        assert isinstance(runner, ProcessRunner)

    def test_the_backend_decides_rather_than_a_setting(self):
        """One answer, and no way for a configuration and a capability to
        disagree about whether a model is loaded in this process."""
        import inspect

        from videoserver.playervision import runner as module

        source = inspect.getsource(module.make_runner)
        assert "isolated" in source


class TestInline:
    def _runner(self):
        backend = HeuristicBackend()
        backend.start()
        return InlineRunner(backend, VisionWorker(backend))

    def test_submit_returns_this_frames_rows(self):
        runner = self._runner()
        runner.configure(layout=QUAD_4, hints=(PlayerHint(1, ("upper_left",)),))
        runner.submit(gray(), 1)
        for step in range(6):
            rows = runner.submit(gray((20 + step * 14, 20, 30)), (step + 2) * 10**8)
        assert rows == runner.latest()

    def test_stop_is_safe_twice(self):
        runner = self._runner()
        runner.stop()
        runner.stop()

    def test_it_says_which_runner_it_is(self):
        assert self._runner().snapshot()["runner"] == "inline"


class TestProcess:
    """A real child process. Slower than the rest of the suite, and the only
    thing that can show the isolation actually isolates."""

    @pytest.fixture
    def runner(self):
        runner = ProcessRunner()
        caps = runner.start("heuristic", 0.6)
        if not caps.available:
            runner.stop()
            pytest.skip(f"the worker would not start: {caps.reason}")
        yield runner
        runner.stop()

    def test_it_starts_and_reports_what_it_loaded(self, runner):
        report = runner.snapshot()
        assert report["runner"] == "process"
        assert report["alive"] is True
        assert report["pid"]

    def test_frames_reach_it_and_rows_come_back(self, runner):
        runner.configure(layout=QUAD_4, hints=(PlayerHint(1, ("upper_left",)),))
        step = 0
        def push():
            nonlocal step
            step += 1
            runner.submit(gray((20 + step * 6, 20, 40)), step * 10**8)
            return bool(runner.latest())
        assert wait_for(push), "no rows ever came back from the worker"

    def test_the_player_is_identified_across_the_boundary(self, runner):
        """Not just that bytes crossed: the roster went down, the frames went
        down, and an identified row came back."""
        runner.configure(layout=QUAD_4, hints=(PlayerHint(1, ("upper_left",)),))
        step = 0
        def push():
            nonlocal step
            step += 1
            runner.submit(gray((20 + (step % 12) * 6, 20, 40)), step * 10**8)
            return any(row.player_id == 1 for row in runner.latest())
        assert wait_for(push), "the worker never identified the viewport owner"

    def test_submit_does_not_wait_for_a_result(self, runner):
        """The caller is the thread that sends the status message."""
        runner.configure(layout=QUAD_4)
        worst = 0.0
        for step in range(40):
            started = time.perf_counter()
            runner.submit(gray((20 + step, 20, 40)), (step + 1) * 10**8)
            worst = max(worst, time.perf_counter() - started)
        assert worst < 0.05, f"a submit took {worst * 1000:.1f} ms"

    def test_killing_the_worker_does_not_raise_at_the_caller(self, runner):
        """The reason the boundary exists. A CUDA fault does not raise -- it
        takes the process down."""
        import signal

        runner._process.send_signal(signal.SIGTERM)
        runner._process.wait(timeout=10)
        for step in range(5):
            runner.submit(gray((20 + step, 20, 40)), (step + 1) * 10**8)
            assert runner.latest() == [] or True   # must simply not raise

    def test_a_killed_worker_is_restarted(self, runner):
        first = runner.snapshot()["pid"]
        runner._process.kill()
        runner._process.wait(timeout=10)

        def poke():
            runner.submit(gray(), 10**8)
            report = runner.snapshot()
            return report["alive"] and report["pid"] != first

        assert wait_for(poke, timeout=40.0), "the worker was never restarted"
        assert runner.restarts >= 1

    def test_configuration_is_replayed_after_a_restart(self, runner):
        """A worker that came back knowing nothing would identify nobody
        until the Bluetooth server's next slow push."""
        runner.configure(layout=QUAD_4, hints=(PlayerHint(2, ("lower_right",)),))
        runner._process.kill()
        runner._process.wait(timeout=10)

        def poke():
            runner.submit(gray(), 10**8)
            return runner.snapshot().get("alive")

        assert wait_for(poke, timeout=40.0)

        def replayed():
            runner.submit(gray(), 10**8)
            return runner.snapshot().get("players") == 1

        assert wait_for(replayed, timeout=25.0), (
            "the roster was not replayed to the new worker"
        )

    def test_the_slot_counters_are_the_ones_that_can_move(self, runner):
        """This process writes the slot and never reads it, so a `reads`
        reported from here would be structurally always zero -- the same trap
        as `reports_sent`, where the healthy-looking counter could not have
        answered the question being asked."""
        runner.configure(layout=QUAD_4)
        step = 0

        def push():
            nonlocal step
            step += 1
            runner.submit(gray((20 + step, 20, 40)), step * 10**8)
            return runner.snapshot().get("slot_reads", 0) > 0

        assert wait_for(push), "the worker never reported reading a frame"
        slot = runner.snapshot()["slot"]
        assert set(slot) == {"writes", "oversized"}
        assert slot["writes"] > 0

    def test_stop_takes_the_worker_with_it(self, runner):
        process = runner._process
        runner.stop()
        assert process.poll() is not None, "the worker outlived stop()"

    def test_stop_is_safe_twice(self, runner):
        runner.stop()
        runner.stop()


class TestFallingBehind:
    """The worker cannot keep up. It must drop frames, not accumulate them.

    This is the claim the whole design rests on: a model that is too slow for
    the frame rate costs accuracy, never latency, and never the stream. The
    slot is what enforces it -- there is one buffer, so there is nowhere for a
    backlog to form.
    """

    def test_submitting_far_faster_than_the_worker_never_queues(self):
        runner = ProcessRunner()
        caps = runner.start("heuristic", 0.6)
        if not caps.available:
            runner.stop()
            pytest.skip(f"the worker would not start: {caps.reason}")
        try:
            runner.configure(layout=QUAD_4)
            worst = 0.0
            for step in range(400):
                started = time.perf_counter()
                runner.submit(gray((20 + step % 60, 20, 40)), (step + 1) * 10**7)
                worst = max(worst, time.perf_counter() - started)

            # The writer is the thread that also sends the status message.
            assert worst < 0.05, f"a submit took {worst * 1000:.1f} ms"

            # Four hundred frames offered, and the worker read only the ones
            # it got to. Anything that had queued would show as reads
            # approaching writes -- and as labels arriving later and later.
            def settled():
                runner.submit(gray(), 10**8)
                return runner.snapshot().get("slot_reads", 0) > 0

            assert wait_for(settled), "the worker never read a frame"
            report = runner.snapshot()
            assert report["slot"]["writes"] > report["slot_reads"], (
                "the worker kept up with 400 frames, so this proves nothing"
            )
            assert report["slot"]["oversized"] == 0
        finally:
            runner.stop()


class TestGivingUp:
    def test_a_worker_that_will_not_start_is_not_retried_for_ever(self):
        """Not a crash: something it cannot get past. Restarting will not fix
        it, and retrying would spend the machine's time producing nothing
        while every counter read healthy."""
        runner = ProcessRunner()
        try:
            caps = runner.start("definitely-not-a-backend", 0.6)
            assert caps.available is False
            assert caps.reason
        finally:
            runner.stop()

    def test_the_limit_is_stated_rather_than_implied(self):
        assert MAX_IMMEDIATE_FAILURES > 0
