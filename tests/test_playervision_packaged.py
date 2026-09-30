"""Starting the identification worker, from a packaged build and from source.

Two faults that together made identification dead in every packaged video
server *and* froze the status message the Bluetooth server depends on:

* the worker was launched as ``sys.executable -m videoserver.playervision.child``,
  and in a packaged build ``sys.executable`` is the video server itself, which
  has no ``-m`` -- its own argument parser refused it with a usage error;
* the parent then waited up to thirty seconds for the worker to report, on the
  video server's control thread, and tried again every sample.

So the Bluetooth server held a status from a second after startup -- layout
FULL -- and every client stayed on the whole picture, whatever the detector or
the override said. Nothing here needs a packaged build: the entry point is the
same ``videoserver.main.main`` the bundle runs.
"""

from __future__ import annotations

import sys
import time

import pytest

from common.video import VideoSettings

from videoserver.playervision import runner as runner_module
from videoserver.playervision.child import WORKER_FLAG
from videoserver.playervision.runner import ProcessRunner, worker_command
from videoserver.playervision.service import PlayerVisionService

from tests.playervision_fakes import (
    MODULE_PATH,
    IsolatedBrightBoxBackend,
    registered,
)

STAND_IN = IsolatedBrightBoxBackend.name

#: A worker that dies the way the packaged one did: a usage error, code 2.
DIES_AT_START = [
    sys.executable, "-c",
    "import sys; sys.stderr.write('usage: rbgc-video: error: unrecognized "
    "arguments: -m\\n'); sys.exit(2)",
]

#: A worker that never reports, the way a slow model load looks from outside.
NEVER_REPORTS = [sys.executable, "-c", "import time; time.sleep(60)"]


def _poll(runner, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        caps = runner.poll_start()
        if caps is not None:
            return caps
        time.sleep(0.02)
    return None


class TestTheWorkerCommand:
    def test_from_source_it_is_a_module(self, monkeypatch):
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert worker_command() == [
            sys.executable, "-m", "videoserver.playervision.child",
        ]

    def test_packaged_it_is_the_program_itself(self, monkeypatch):
        """``rbgc-video.exe -m ...`` is refused by the video server's parser."""
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        command = worker_command()
        assert command == [sys.executable, WORKER_FLAG]
        assert "-m" not in command


class TestThePackagedEntryPoint:
    def test_the_flag_hands_straight_to_the_worker(self, monkeypatch):
        import videoserver.main as video_main
        import videoserver.playervision.child as child

        seen = []
        monkeypatch.setattr(child, "main", lambda argv: seen.append(argv) or 7)

        def no_console():
            raise AssertionError(
                "attached a console first: the worker's pipes would be replaced"
            )

        monkeypatch.setattr(video_main, "attach_console_if_needed", no_console)

        assert video_main.main([WORKER_FLAG, "--slot", "abc"]) == 7
        assert seen == [["--slot", "abc"]]

    def test_a_worker_started_through_it_identifies(self, monkeypatch):
        """The whole round trip through the entry point a bundle runs: pipes,
        capabilities and all. `-c` stands in for the frozen executable, which
        also puts the flag first in `sys.argv`."""
        monkeypatch.setattr(
            runner_module, "worker_command",
            lambda: [sys.executable, "-c",
                     "import sys; from videoserver.main import main; sys.exit(main())",
                     WORKER_FLAG],
        )
        runner = ProcessRunner(backend_module=MODULE_PATH)
        try:
            caps = runner.start(STAND_IN, 0.6)
            assert caps.available, caps.reason
            assert runner.snapshot()["alive"] is True
        finally:
            runner.stop()


class TestAWorkerThatDiesAtStart:
    def test_it_is_noticed_at_once_and_says_why(self, monkeypatch):
        monkeypatch.setattr(runner_module, "worker_command", lambda: list(DIES_AT_START))
        runner = ProcessRunner()
        try:
            began = time.monotonic()
            runner.begin(STAND_IN, 0.6)
            caps = _poll(runner)
            elapsed = time.monotonic() - began
        finally:
            runner.stop()

        assert caps is not None and caps.available is False
        assert elapsed < 5.0, f"took {elapsed:.1f}s -- the old wait ran to 30"
        assert "code 2" in caps.reason
        assert "unrecognized arguments" in caps.reason, caps.reason

    def test_begin_never_waits_for_the_child(self, monkeypatch):
        monkeypatch.setattr(runner_module, "worker_command", lambda: list(NEVER_REPORTS))
        runner = ProcessRunner()
        try:
            began = time.monotonic()
            runner.begin(STAND_IN, 0.6)
            assert time.monotonic() - began < 1.0
            assert runner.poll_start() is None
        finally:
            runner.stop()


def _on():
    return VideoSettings(player_id_enabled=True, player_id_backend="auto")


class TestTheServiceNeverWaitsForAWorker:
    """Sampling runs on the thread that sends the status message."""

    @pytest.fixture(autouse=True)
    def _isolated_stand_in(self):
        with registered(IsolatedBrightBoxBackend):
            yield

    def test_a_loading_worker_costs_a_sample_nothing(self, monkeypatch):
        monkeypatch.setattr(runner_module, "worker_command", lambda: list(NEVER_REPORTS))
        service = PlayerVisionService()
        try:
            began = time.monotonic()
            assert service.sample(object(), _on(), True, 1) is None
            assert time.monotonic() - began < 1.0
            assert service.active and not service.running
            assert service.snapshot().get("starting") is True
        finally:
            service.stop()
        assert not service.active

    def test_a_worker_that_cannot_start_is_reported_and_not_hammered(self, monkeypatch):
        monkeypatch.setattr(runner_module, "worker_command", lambda: list(DIES_AT_START))
        service = PlayerVisionService()
        try:
            now = 1
            service.sample(object(), _on(), True, now)
            deadline = time.monotonic() + 10.0
            while service.active and time.monotonic() < deadline:
                now += 2_000_000_000
                service.sample(object(), _on(), True, now)
                time.sleep(0.02)

            report = service.snapshot()
            assert report["available"] is False
            assert "code 2" in report["reason"]

            # The next sample, well inside the backoff, starts nothing.
            now += 2_000_000_000
            service.sample(object(), _on(), True, now)
            assert not service.active, "retried on the very next sample"
        finally:
            service.stop()

    def test_switching_it_off_and_on_retries_at_once(self, monkeypatch):
        monkeypatch.setattr(runner_module, "worker_command", lambda: list(DIES_AT_START))
        service = PlayerVisionService()
        try:
            now = 1
            service.sample(object(), _on(), True, now)
            deadline = time.monotonic() + 10.0
            while service.active and time.monotonic() < deadline:
                now += 2_000_000_000
                service.sample(object(), _on(), True, now)
                time.sleep(0.02)

            service.stop()
            now += 2_000_000_000
            service.sample(object(), _on(), True, now)
            assert service.active, "an explicit stop should clear the backoff"
        finally:
            service.stop()


class TestTheStatusSaysWhyItIsNotRunning:
    def test_a_failed_start_puts_the_reason_on_the_status(self):
        """It used to appear only while a worker was up, so a failed start
        reported nothing and the web GUI blamed a box that was already ticked."""
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp
        from videoserver.playervision.backends.base import Capabilities

        source = VideoServerApp(VideoServerConfig(password="pw"))
        source.config.playervision_allowed = True
        source.apply_config(VideoSettings(player_id_enabled=True))
        source._players._caps = Capabilities(
            backend="onnx", available=False,
            reason="the worker exited before starting (code 2)",
        )
        block = source.status()["player_id"]
        assert block["available"] is False
        assert "code 2" in block["reason"]

    def test_nothing_is_said_when_nobody_asked(self):
        from videoserver.config import VideoServerConfig
        from videoserver.pipeline import VideoServerApp
        from videoserver.playervision.backends.base import Capabilities

        source = VideoServerApp(VideoServerConfig(password="pw"))
        source.config.playervision_allowed = True
        source._players._caps = Capabilities(
            backend="onnx", available=False, reason="stale from last time",
        )
        assert "player_id" not in source.status()
