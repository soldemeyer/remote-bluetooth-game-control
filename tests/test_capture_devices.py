"""Capture device enumeration.

The DirectShow listing is the one place FFmpeg reports its answer through the
*log* rather than a return value or an exception, and getting that wrong fails
in the most misleading way available: the dropdown stays empty on every
machine, which reads as a driver or permissions problem rather than as a parser
that was reading the wrong string.

The parser is exercised against captured real output, so these run anywhere --
including CI boxes with no capture hardware at all.
"""

from __future__ import annotations

import pytest

pytest.importorskip("av", reason="video extras not installed")

from videoserver.capture import (  # noqa: E402
    _parse_dshow_listing,
    default_backend,
    enumerate_devices,
)

#: Real output, captured from FFmpeg on Windows. Note the name, the "(video",
#: and the ")" arrive as separate log records -- joining them is what puts the
#: newlines in the middle, and a parser that assumes one line per device finds
#: nothing here.
REAL_LISTING = (
    '"MX Brio"\n(video\n)\n\n'
    'Alternative name "@device_pnp_\\\\?\\usb#vid_046d&pid_0944&mi_00#7&7018144"\n'
    '"ShadowCast 3"\n(video\n)\n\n'
    'Alternative name "@device_pnp_\\\\?\\usb#vid_32ed&pid_3701&mi_00#c&2cd4f5ec"\n'
    '"HD (ShadowCast 3)"\n(audio\n)\n\n'
    'Alternative name "@device_cm_{33D9A762}\\wave_{4DEB7A3A}"\n'
)

#: Newer FFmpeg builds put it on one line. Both must work.
SINGLE_LINE_LISTING = (
    '[dshow @ 0000] "Integrated Camera" (video)\n'
    '[dshow @ 0000]   Alternative name "@device_pnp_\\\\?\\usb#vid_0001"\n'
    '[dshow @ 0000] "Microphone Array" (audio)\n'
)


class TestParsingTheListing:
    def test_devices_are_found_across_line_breaks(self):
        """The failure that shipped: FFmpeg splits one device over four records."""
        devices = _parse_dshow_listing(REAL_LISTING)

        names = [d["name"] for d in devices]
        assert names == ["MX Brio", "ShadowCast 3", "HD (ShadowCast 3)"]

    def test_video_and_audio_are_told_apart(self):
        devices = _parse_dshow_listing(REAL_LISTING)

        video = [d["name"] for d in devices if d["kind"] == "video"]
        audio = [d["name"] for d in devices if d["kind"] == "audio"]
        assert video == ["MX Brio", "ShadowCast 3"]
        assert audio == ["HD (ShadowCast 3)"]

    def test_a_name_containing_parentheses_survives(self):
        """'HD (ShadowCast 3)' must not be mistaken for a kind marker."""
        devices = _parse_dshow_listing(REAL_LISTING)
        assert any(d["name"] == "HD (ShadowCast 3)" for d in devices)

    def test_the_single_line_format_also_parses(self):
        devices = _parse_dshow_listing(SINGLE_LINE_LISTING)
        assert [d["name"] for d in devices] == ["Integrated Camera", "Microphone Array"]
        assert devices[0]["kind"] == "video"
        assert devices[1]["kind"] == "audio"

    def test_the_alternative_name_goes_to_the_right_device(self):
        """Two identical cards produce two identical names; this tells them apart."""
        devices = _parse_dshow_listing(REAL_LISTING)
        by_name = {d["name"]: d for d in devices}

        assert "vid_046d" in by_name["MX Brio"]["alternative"]
        assert "vid_32ed" in by_name["ShadowCast 3"]["alternative"]

    def test_a_device_with_no_alternative_name_is_still_listed(self):
        devices = _parse_dshow_listing(SINGLE_LINE_LISTING)
        assert devices[1]["alternative"] == ""
        assert devices[1]["name"] == "Microphone Array"

    def test_the_alternative_form_is_never_offered_as_a_device(self):
        """It appears in quotes too, and would otherwise be listed twice."""
        devices = _parse_dshow_listing(REAL_LISTING)
        assert not any(d["name"].startswith("@device") for d in devices)

    def test_empty_output_yields_nothing_rather_than_raising(self):
        assert _parse_dshow_listing("") == []
        assert _parse_dshow_listing("no devices here") == []


class TestEnumeration:
    def test_it_never_raises(self):
        """A machine with no devices must produce an empty list, not an error."""
        assert isinstance(enumerate_devices(), list)

    def test_an_unknown_backend_is_harmless(self):
        assert enumerate_devices("nonsense-backend") == []

    def test_every_entry_has_the_fields_the_gui_reads(self):
        for entry in enumerate_devices():
            assert entry["id"]
            assert entry["name"]
            assert entry["kind"] in ("video", "audio")

    @pytest.mark.skipif(
        default_backend() != "dshow", reason="DirectShow is Windows-only"
    )
    def test_a_windows_machine_finds_at_least_one_device(self):
        """Effectively always true -- every laptop has a microphone.

        Skipped rather than asserted elsewhere because a headless CI box
        genuinely may have none.
        """
        devices = enumerate_devices()
        if not devices:
            pytest.skip("this machine really has no capture devices")
        assert any(d["kind"] == "audio" for d in devices) or any(
            d["kind"] == "video" for d in devices
        )


class TestOpeningIsTolerant:
    """A camera offers a fixed menu of (format, size, rate) combinations.

    Asking for one that is not on it fails the open outright, and DirectShow
    reports only "Could not set video options" -- which reads as a broken
    device rather than as an unsupported mode. Since capture size and encode
    size are independent (the encoder reformats whatever arrives), refusing to
    start over a mode preference is the wrong trade.
    """

    def _settings(self, **overrides):
        from common.video import VideoSettings

        # test_source deliberately off: it short-circuits to the lavfi test
        # pattern, which never touches a device and so never exercises any of
        # this.
        base = {
            "backend": "dshow",
            "device": "Some Camera",
            "test_source": False,
            "width": 1920,
            "height": 1080,
            "fps": 60,
            "audio_enabled": False,
        }
        base.update(overrides)
        return VideoSettings(**base)

    def test_a_refused_mode_falls_back_rather_than_failing(self, monkeypatch):
        from videoserver import capture as capture_mod

        tried: list[dict] = []

        class FakeCodec:
            width, height = 640, 480

        class FakeStream:
            codec_context = FakeCodec()

        class FakeStreams:
            video = [FakeStream()]

        class FakeContainer:
            streams = FakeStreams()

        def fake_open(target, format=None, options=None):
            tried.append(dict(options or {}))
            # Refuse anything that pins both size and rate, as a camera would.
            if "video_size" in options and "framerate" in options:
                raise OSError("Could not set video options")
            return FakeContainer()

        import av

        monkeypatch.setattr(av, "open", fake_open)
        settings = self._settings(backend="dshow", device="Some Camera")

        container = capture_mod._open_video_container(settings)

        assert container is not None, "a refused mode stopped capture entirely"
        assert len(tried) >= 2, "it never tried anything but the requested mode"

    def test_the_requested_mode_is_tried_first(self, monkeypatch):
        from videoserver import capture as capture_mod

        tried: list[dict] = []

        class FakeContainer:
            class streams:
                video = [type("S", (), {"codec_context": type("C", (), {"width": 1920, "height": 1080})()})()]

        def fake_open(target, format=None, options=None):
            tried.append(dict(options or {}))
            return FakeContainer()

        import av

        monkeypatch.setattr(av, "open", fake_open)
        capture_mod._open_video_container(self._settings(backend="dshow", device="Cam"))

        assert tried[0].get("video_size") == "1920x1080"
        assert tried[0].get("framerate") == "60"
        assert len(tried) == 1, "it kept trying after the first one worked"

    def test_a_device_that_refuses_everything_still_raises(self, monkeypatch):
        from videoserver import capture as capture_mod
        from videoserver.capture import CaptureError

        def fake_open(target, format=None, options=None):
            raise OSError("device is in use")

        import av

        monkeypatch.setattr(av, "open", fake_open)

        with pytest.raises(CaptureError) as excinfo:
            capture_mod._open_video_container(self._settings(backend="dshow", device="Cam"))
        assert "device is in use" in str(excinfo.value)


class TestStopReportsHonestly:
    """A stalled camera leaves the capture thread parked inside FFmpeg's read
    with the device still open. Reporting success there let a restart open a
    *second* capture on the same device, which DirectShow refuses forever.
    """

    def test_stopping_a_capture_that_never_started_succeeds(self):
        from common.video import VideoSettings
        from videoserver.capture import VideoCapture

        assert VideoCapture(VideoSettings(test_source=True)).stop() is True

    def test_a_thread_that_will_not_exit_is_reported(self):
        import threading

        from common.video import VideoSettings
        from videoserver.capture import VideoCapture

        capture = VideoCapture(VideoSettings(test_source=True))

        # Stand in for a thread wedged inside a device read.
        wedged = threading.Event()
        capture._thread = threading.Thread(target=wedged.wait, daemon=True)
        capture._thread.start()
        try:
            assert capture.stop(timeout=0.3) is False
            assert capture._thread is not None, (
                "the reference was dropped while the device was still held"
            )
        finally:
            wedged.set()
