"""Who owns the video settings, and when.

A video server is configured in front of the machine it captures on -- that is
where the capture card is plugged in, and it is usually running before any
Bluetooth server hears of it. Connecting must not silently undo that work. The
reported symptom was "connecting to the bluetooth server seemed to change the
stream settings".

The first answer was a latch: adopt whatever the source was doing if nothing
had been configured here, and be authoritative from the moment anything was.
It closed the reported case and left the one underneath it open -- the operator
changes something on the video server *later*, and it is reverted, because by
then this end has latched.

**So ownership is now by field, not by when.** In external mode the video
server is somebody else's machine and owns how the picture is made; this server
owns what it does with the picture:

  * ``SOURCE_OWNED_FIELDS`` -- device, resolution, encoder, bitrate. Mirrored
    *from* every status so the GUI tells the truth, and stripped from anything
    pushed at it. The web GUI hides those controls in this mode for the same
    reason.
  * everything else -- the preview, which serves this server's own operator,
    and the split-screen detector, whose output this server turns into
    per-client crops. Ours in every mode.

Embedded is unchanged and stays fully authoritative: there the source is this
machine's own subprocess, which is why ``cap_for_embedded`` exists at all.

The latch still matters for the first status of all, and for the fields we do
own: a blank device is "keep using yours", never "reset to the first one
found".
"""

from __future__ import annotations

from common.video import VideoSettings
from server.video import (
    MODE_EMBEDDED,
    MODE_EXTERNAL,
    SOURCE_OWNED_FIELDS,
    VideoRegistry,
)


def _status(settings: VideoSettings, cfg_seq: int = 0) -> dict:
    return {
        "cfg_seq": cfg_seq,
        "media_port": 47810,
        "lan_host": "192.168.1.16",
        "status": {"streaming": True, "width": settings.width, "height": settings.height},
        "settings": settings.to_dict(),
    }


def _attached_registry(mode: str = MODE_EXTERNAL, **kwargs) -> VideoRegistry:
    registry = VideoRegistry(mode=mode, **kwargs)
    registry.attach_source_endpoint("192.168.1.16", 47810)
    return registry


def _reported(**settings) -> dict:
    """A VIDEO_STATUS carrying the source's own full settings."""
    return _status(VideoSettings(**settings))


class TestAnUnconfiguredServerDefersToTheSource:
    def test_it_sends_no_settings_before_it_has_any(self):
        registry = _attached_registry(settings=VideoSettings(), configured=False)

        message = registry.config_message()

        assert "config" not in message, (
            "defaults were pushed at the source, resetting the operator's setup"
        )

    def test_tickets_still_reach_the_source_meanwhile(self):
        """The push carries admission as well as settings; only settings wait."""
        registry = _attached_registry(settings=VideoSettings(), configured=False)
        registry.ticket_for("client-a")

        message = registry.config_message()

        assert message["tickets"], "a viewer would wait forever on an advert"
        assert "config" not in message

    def test_the_sources_settings_are_adopted_from_its_status(self):
        registry = _attached_registry(settings=VideoSettings(), configured=False)
        theirs = VideoSettings(
            device="MX Brio", width=1920, height=1080, fps=30, bitrate_kbps=12000
        )

        registry.update_status_from_link(_status(theirs))

        adopted = registry.settings
        assert adopted.device == "MX Brio"
        assert (adopted.width, adopted.height, adopted.fps) == (1920, 1080, 30)
        assert adopted.bitrate_kbps == 12000

    def test_after_adopting_it_pushes_them_back_unchanged(self):
        registry = _attached_registry(settings=VideoSettings(), configured=False)
        theirs = VideoSettings(device="MX Brio", width=1920, height=1080, fps=30)

        registry.update_status_from_link(_status(theirs))
        message = registry.config_message()

        assert message["config"]["device"] == "MX Brio"
        assert message["config"]["width"] == 1920

    def test_a_later_status_does_not_undo_a_setting_we_own(self):
        """The latch, which still holds for the fields that are ours.

        This used to assert it for *every* field, which was the second half of
        the reported bug rather than a fix for it: an operator who changed the
        resolution on the video server after we had latched watched it revert.
        """
        registry = _attached_registry(settings=VideoSettings(), configured=False)
        registry.update_status_from_link(_status(VideoSettings(preview_fps=5)))

        registry.set_config(VideoSettings(preview_fps=30))
        registry.update_status_from_link(_status(VideoSettings(preview_fps=5)))

        assert registry.settings.preview_fps == 30, "the source overrode the operator"


class TestAConfiguredServerIsAuthoritativeOverWhatItOwns:
    def test_saved_settings_are_pushed(self):
        chosen = VideoSettings(width=1280, height=720, fps=60, device="Elgato")
        registry = _attached_registry(settings=chosen, configured=True)

        message = registry.config_message()

        assert message["config"]["width"] == 1280
        assert message["config"]["device"] == "Elgato"

    def test_the_source_cannot_talk_it_out_of_what_is_ours(self):
        chosen = VideoSettings(preview_width=960, split_override="QUAD_4")
        registry = _attached_registry(settings=chosen, configured=True)

        registry.update_status_from_link(
            _status(VideoSettings(preview_width=320, split_override="auto")))

        assert registry.settings.preview_width == 960
        assert registry.settings.split_override == "QUAD_4"

    def test_but_the_capture_settings_follow_the_source(self):
        """The half this end does *not* own, in external mode.

        The operator at the capture card changed the resolution; we report
        what is actually running rather than what we last pushed, because the
        alternative is a GUI confidently describing a stream that is not the
        one being sent.
        """
        chosen = VideoSettings(width=1280, height=720)
        registry = _attached_registry(settings=chosen, configured=True)

        registry.update_status_from_link(_status(VideoSettings(width=640, height=480)))

        assert registry.settings.width == 640

    def test_passing_settings_without_saying_still_counts_as_configured(self):
        """Back-compat: a caller that hands over settings means to use them."""
        registry = _attached_registry(settings=VideoSettings(width=800, height=600))
        assert registry.config_message()["config"]["width"] == 800


class TestABlankDeviceKeepsTheLocalOne:
    """Applied on the video server, where the capture device actually is."""

    def _responder(self, current: VideoSettings):
        from videoserver.control import ControlResponder

        responder = ControlResponder.__new__(ControlResponder)
        responder._app = type("App", (), {"settings": current})()
        return responder

    def test_a_blank_device_does_not_replace_a_chosen_one(self):
        responder = self._responder(VideoSettings(device="MX Brio", backend="dshow"))

        merged = responder._merge_local_device(VideoSettings(device=""))

        assert merged.device == "MX Brio", (
            "connecting switched the capture away from the operator's camera"
        )

    def test_a_named_device_still_wins(self):
        responder = self._responder(VideoSettings(device="MX Brio"))

        merged = responder._merge_local_device(VideoSettings(device="ShadowCast 3"))

        assert merged.device == "ShadowCast 3", "the web GUI could not change the device"

    def test_the_audio_device_follows_the_same_rule(self):
        responder = self._responder(VideoSettings(audio_device="HD (ShadowCast 3)"))
        assert responder._merge_local_device(
            VideoSettings(audio_device="")
        ).audio_device == "HD (ShadowCast 3)"

    def test_auto_does_not_replace_a_chosen_backend(self):
        responder = self._responder(VideoSettings(backend="dshow"))
        assert responder._merge_local_device(VideoSettings(backend="auto")).backend == "dshow"

    def test_other_settings_are_taken_as_sent(self):
        """Only the device is machine-local; quality belongs to the operator."""
        responder = self._responder(
            VideoSettings(device="MX Brio", width=1920, height=1080, bitrate_kbps=20000)
        )

        merged = responder._merge_local_device(
            VideoSettings(width=1280, height=720, bitrate_kbps=8000)
        )

        assert (merged.width, merged.height) == (1280, 720)
        assert merged.bitrate_kbps == 8000


class TestTheFieldsAreDividedWithNothingLeftOver:
    def test_every_setting_has_an_owner(self):
        """A field in neither half is one nobody has thought about.

        Which matters in a specific way: an unlisted field is ours by default,
        so it is pushed to a remote source -- the behaviour this whole split
        exists to stop -- and nothing anywhere says so.
        """
        known = set(VideoSettings().to_dict())
        assert SOURCE_OWNED_FIELDS <= known, sorted(SOURCE_OWNED_FIELDS - known)

        ours = known - SOURCE_OWNED_FIELDS
        unclassified = [
            field for field in ours
            if not (field.startswith("preview_") or field.startswith("split_")
                    or field == "probe_devices")
        ]
        assert not unclassified, (
            "these settings are neither source-owned nor obviously ours; "
            f"decide which: {sorted(unclassified)}"
        )

    def test_the_preview_and_the_detector_are_never_the_source_s(self):
        """They serve *this* server: the preview is its operator's monitoring
        picture, and the detector's output is what it turns into per-client
        crops. A remote source has no view on either."""
        for field in ("preview_width", "preview_fps", "split_detect_enabled",
                      "split_crop_bars", "split_override"):
            assert field not in SOURCE_OWNED_FIELDS, field


# ---------------------------------------------------------------------------
# Mirroring the source's own settings
# ---------------------------------------------------------------------------


class TestExternalModeTakesTheSourceAsAuthority:
    def test_a_reported_capture_setting_becomes_ours(self):
        registry = _attached_registry()
        registry.set_config(VideoSettings(width=1280, height=720))

        registry.update_status_from_link(_reported(width=1920, height=1080))

        assert registry.settings.width == 1920, (
            "the source reported 1080p and we kept insisting on 720p"
        )
        assert registry.settings.height == 1080

    def test_it_does_not_bump_the_sequence(self):
        """**The re-push storm.**

        `needs_config_push` fires on a mismatch between `cfg_seq` and the
        acknowledged sequence. Bumping it here would send the source its own
        values back every two seconds for the life of the process, with each
        end believing the other was behind.
        """
        registry = _attached_registry()
        registry.set_config(VideoSettings(width=1280, height=720))
        before = registry.cfg_seq

        registry.update_status_from_link(_reported(width=1920, height=1080))

        assert registry.cfg_seq == before

    def test_our_own_settings_are_not_overwritten_by_the_source(self):
        registry = _attached_registry()
        registry.set_config(VideoSettings(preview_fps=30, split_detect_enabled=True))

        registry.update_status_from_link(
            _reported(preview_fps=5, split_detect_enabled=False))

        assert registry.settings.preview_fps == 30
        assert registry.settings.split_detect_enabled is True

    def test_an_unchanged_report_changes_nothing(self):
        registry = _attached_registry()
        registry.set_config(VideoSettings(width=1280, height=720))
        before = registry.settings.to_dict()

        registry.update_status_from_link(_reported(width=1280, height=720))

        assert registry.settings.to_dict() == before


class TestEmbeddedModeIsUnaffected:
    def test_the_source_does_not_get_to_dictate(self):
        """There the source is our own subprocess on this machine, and we are
        responsible for what it is asked to encode -- which is why
        `cap_for_embedded` exists at all."""
        registry = _attached_registry(mode=MODE_EMBEDDED)
        registry.set_config(VideoSettings(width=1280, height=720))

        registry.update_status_from_link(_reported(width=640, height=480))

        assert registry.settings.width == 1280
