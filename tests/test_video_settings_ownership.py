"""Who owns the video settings, and when.

A video server is configured in front of the machine it captures on -- that is
where the capture card is plugged in, and it is usually running before any
Bluetooth server hears of it. Connecting must not silently undo that work, so
the rule is:

  * nothing configured on the Bluetooth server -> adopt what the source is
    already doing, and send it no settings at all;
  * something configured there -> that is authoritative from then on;
  * a blank device is "keep using yours", never "reset to the first one found".

The reported symptom was simply "connecting to the bluetooth server seemed to
change the stream settings".
"""

from __future__ import annotations

from common.video import VideoSettings
from server.video import MODE_EXTERNAL, VideoRegistry


def _status(settings: VideoSettings, cfg_seq: int = 0) -> dict:
    return {
        "cfg_seq": cfg_seq,
        "media_port": 47810,
        "lan_host": "192.168.1.16",
        "status": {"streaming": True, "width": settings.width, "height": settings.height},
        "settings": settings.to_dict(),
    }


def _attached_registry(**kwargs) -> VideoRegistry:
    registry = VideoRegistry(mode=MODE_EXTERNAL, **kwargs)
    registry.attach_source_endpoint("192.168.1.16", 47810)
    return registry


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

    def test_adopting_happens_only_once(self):
        """A later status must not undo an operator's change."""
        registry = _attached_registry(settings=VideoSettings(), configured=False)
        registry.update_status_from_link(_status(VideoSettings(width=1920, height=1080)))

        registry.set_config(VideoSettings(width=1280, height=720))
        registry.update_status_from_link(_status(VideoSettings(width=1920, height=1080)))

        assert registry.settings.width == 1280, "the source overrode the operator"


class TestAConfiguredServerIsAuthoritative:
    def test_saved_settings_are_pushed(self):
        chosen = VideoSettings(width=1280, height=720, fps=60, device="Elgato")
        registry = _attached_registry(settings=chosen, configured=True)

        message = registry.config_message()

        assert message["config"]["width"] == 1280
        assert message["config"]["device"] == "Elgato"

    def test_the_source_cannot_talk_it_out_of_them(self):
        chosen = VideoSettings(width=1280, height=720)
        registry = _attached_registry(settings=chosen, configured=True)

        registry.update_status_from_link(_status(VideoSettings(width=640, height=480)))

        assert registry.settings.width == 1280

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
