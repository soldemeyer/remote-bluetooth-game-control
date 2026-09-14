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
    def test_saved_capture_settings_are_pushed_in_embedded_mode(self):
        """Where they *are* ours -- the source is this machine's subprocess.

        This used to assert the same for external mode, and that is exactly
        the behaviour which overwrote a remote server's capture settings
        before it could get a word in: a video server configured for 640x480
        streaming 1080p seconds after we connected. See
        `TestTheSourcesOwnSettingsSurviveOurConnecting`.
        """
        chosen = VideoSettings(width=1280, height=720, fps=60, device="Elgato")
        registry = _attached_registry(
            mode=MODE_EMBEDDED, settings=chosen, configured=True)

        message = registry.config_message()

        assert message["config"]["width"] == 1280
        assert message["config"]["device"] == "Elgato"

    def test_in_external_mode_the_source_keeps_its_own_device(self):
        chosen = VideoSettings(width=1280, height=720, device="Elgato")
        registry = _attached_registry(settings=chosen, configured=True)

        registry.update_status_from_link(_reported(device="ShadowCast 3"))

        assert registry.config_message()["config"]["device"] == "ShadowCast 3", (
            "we handed a remote capture machine a device name from our own config"
        )

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
        """Back-compat: a caller that hands over settings means to use them.

        Still true; it is only the *moment* of the first push that moved, to
        after the source has reported.
        """
        registry = _attached_registry(settings=VideoSettings(width=800, height=600))
        registry.update_status_from_link(_reported(width=800, height=600))
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
        acknowledged sequence. Bumping it on every status would send the source
        its own values back every two seconds for the life of the process, with
        each end believing the other was behind.

        The *first* status of a connection is the one exception, and it is
        deliberate -- see `test_the_first_status_asks_for_that_push`. So the
        property here is measured from the second onwards.
        """
        registry = _attached_registry()
        registry.set_config(VideoSettings(width=1280, height=720))
        registry.update_status_from_link(_reported(width=1920, height=1080))
        settled = registry.cfg_seq

        for _ in range(5):
            registry.update_status_from_link(_reported(width=1920, height=1080))

        assert registry.cfg_seq == settled

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


class TestTheSourcesOwnSettingsSurviveOurConnecting:
    """**The half the first attempt missed.**

    Stopping the web GUI from *editing* the capture settings, and mirroring
    what the source reports, is not enough on its own: `config_message` still
    pushed the whole block, and `VideoLink` pushes it the moment it connects --
    before any status has arrived. So this end's saved settings won every time
    and the mirror never got a look in.

    Reported as a video server configured for 640x480 whose own GUI still said
    640x480 while the Bluetooth server showed the stream as 1080p. It was
    1080p: we had told it to be.

    The block has no partial form -- `config` is a complete VideoSettings, so a
    field left out is read as a *default*, not as "keep yours" -- which is why
    the fix is to say nothing at all until the source has said something.
    """

    def test_nothing_is_pushed_before_the_first_status(self):
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)

        message = registry.config_message()

        assert "config" not in message, (
            "the source's capture settings were overwritten on connect"
        )

    def test_tickets_and_the_broker_still_get_through_meanwhile(self):
        """Withholding settings must not withhold admission, or a viewer waits
        forever on an advert."""
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)
        registry.ticket_for("client-a")

        message = registry.config_message()

        assert message["tickets"]
        assert "cfg_seq" in message

    def test_after_the_first_status_we_push_the_sources_own_values_back(self):
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)

        registry.update_status_from_link(_reported(width=640, height=480))
        message = registry.config_message()

        assert "config" in message, "our own settings never reach the source"
        assert (message["config"]["width"], message["config"]["height"]) == (640, 480), (
            "the push still overrides the source's resolution"
        )

    def test_our_own_settings_ride_that_push(self):
        """The point of resuming the push at all: the preview and the detector
        are ours in every mode, and the source has heard nothing about them
        while the block was withheld."""
        registry = _attached_registry(
            settings=VideoSettings(preview_fps=30, split_detect_enabled=True),
            configured=True,
        )

        registry.update_status_from_link(_reported(width=640, height=480))
        config = registry.config_message()["config"]

        assert config["preview_fps"] == 30
        assert config["split_detect_enabled"] is True

    def test_the_first_status_asks_for_that_push(self):
        """`needs_config_push` fires on a sequence the source has not
        acknowledged -- and by now it has acknowledged the one it was given, so
        nothing would ask. The withheld block has to be made up explicitly.

        Deliberately not a `cfg_seq` bump: the sequence means "the operator
        changed something", and bumping it here made an *acknowledgement*
        trigger another push, which broke the retry loop's one guarantee.
        """
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)
        seq = registry.cfg_seq
        registry.update_status_from_link(
            {"cfg_seq": seq, "media_port": 47810, "status": {},
             "settings": VideoSettings(width=640, height=480).to_dict()})

        assert registry.needs_config_push() is True, (
            "the source acknowledged an empty message and nothing asks again"
        )
        assert registry.cfg_seq == seq, "the sequence was disturbed"

    def test_and_stops_once_that_push_has_gone_out(self):
        """Every status asking would be the re-push storm: the source's own
        values handed back to it every two seconds, for ever."""
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)
        seq = registry.cfg_seq
        acknowledge = {"cfg_seq": seq, "media_port": 47810, "status": {},
                       "settings": VideoSettings(width=640, height=480).to_dict()}

        registry.update_status_from_link(acknowledge)
        assert registry.needs_config_push() is True
        assert "config" in registry.config_message()

        for _ in range(5):
            registry.update_status_from_link(acknowledge)
            registry._last_pushed_ns = 0
        assert registry.needs_config_push() is False

    def test_a_new_source_starts_the_whole_dance_again(self):
        """A replaced source is a different machine with different hardware,
        so what we learned about the last one must not be pushed at it."""
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)
        registry.update_status_from_link(_reported(width=640, height=480))
        assert "config" in registry.config_message()

        registry.attach_source_endpoint("192.168.1.20", 47810)

        assert "config" not in registry.config_message()

    def test_embedded_mode_pushes_immediately_as_before(self):
        """There the source is our own subprocess and we *are* responsible for
        what it is asked to encode -- `cap_for_embedded` exists for that."""
        registry = _attached_registry(
            mode=MODE_EMBEDDED,
            settings=VideoSettings(width=1280, height=720),
            configured=True,
        )

        assert "config" in registry.config_message()


class TestASourceThatReportsNoSettingsIsNotStuckForever:
    """Withholding the block waits for the source to speak, not for it to send
    settings. A source that reports none -- an older one, or a third-party --
    has still told us it is there, and has nothing of its own to preserve.

    Waiting for settings that never arrive would withhold the config for ever,
    and with it the preview and the split-screen detector. That reads as those
    settings silently doing nothing, on a link every counter calls healthy.
    """

    def test_a_status_without_settings_still_releases_the_config(self):
        registry = _attached_registry(
            settings=VideoSettings(split_detect_enabled=True), configured=True)

        registry.update_status_from_link(
            {"cfg_seq": 0, "media_port": 47810, "status": {"streaming": True}})

        message = registry.config_message()
        assert "config" in message, (
            "a source that reports no settings never receives ours"
        )
        assert message["config"]["split_detect_enabled"] is True

    def test_and_it_does_not_invent_capture_settings_for_it(self):
        """Nothing was reported, so nothing is mirrored -- what we hold stays
        what we held."""
        registry = _attached_registry(
            settings=VideoSettings(width=1920, height=1080), configured=True)

        registry.update_status_from_link(
            {"cfg_seq": 0, "media_port": 47810, "status": {}})

        assert registry.settings.width == 1920
