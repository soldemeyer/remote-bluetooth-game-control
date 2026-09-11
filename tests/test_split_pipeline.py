"""The layout travelling from the video server to the Bluetooth server.

Phase 4 of split-screen: the detector's verdict reaches ``VideoRegistry`` and
registers as something clients would notice. No cropping yet -- that is the
client's half.

The message-size tests here are not incidental. VIDEO_STATUS has a hard
1200-byte ceiling and ``encode_control`` refuses an oversized message whole
rather than truncating it, so a source that is streaming perfectly simply stops
reporting. That is exactly what adding eight settings did, and nothing but the
log said so.
"""

from __future__ import annotations

import pytest

from common import protocol
from common.screen_regions import FULL, QUAD_4, VERTICAL_2
from common.video import VideoSettings
from server.video import VideoRegistry


def app():
    from videoserver.config import VideoServerConfig
    from videoserver.pipeline import VideoServerApp

    return VideoServerApp(VideoServerConfig(password="pw"))


def status_message(source) -> bytes:
    """What ``_send_status`` actually builds, with realistic surroundings."""
    return protocol.encode_control(
        1,
        "VIDEO_STATUS",
        {
            "cfg_seq": 4096,
            "media_port": 47810,
            "lan_host": "192.168.100.200",
            "status": source.status(),
        },
    )


class TestTheStatusMessageFits:
    """A guard on the ceiling, because going over fails totally and quietly."""

    def test_a_status_fits_with_room_to_spare(self):
        size = len(status_message(app()))
        assert size <= protocol.MAX_DATAGRAM
        # Not merely "fits": the next person to add a field needs somewhere to
        # put it. This failing means the status has grown too fat again, and
        # the fix is to move rarely-changing parts onto the slow message --
        # not to shave a few characters off a name.
        assert protocol.MAX_DATAGRAM - size > 250, (
            f"VIDEO_STATUS is {size} bytes and nearly full"
        )

    def test_it_still_fits_with_detection_running(self):
        source = app()
        source.apply_config(VideoSettings(split_detect_enabled=True))
        source.sample_layout()
        assert len(status_message(source)) <= protocol.MAX_DATAGRAM

    def test_a_long_error_list_cannot_burst_it(self):
        """Errors are the other variable-length thing in the status."""
        source = app()
        for i in range(50):
            source._record_error(f"a fairly wordy capture failure number {i}")
        assert len(status_message(source)) <= protocol.MAX_DATAGRAM


class TestTheSlowStateMessageFits:
    def test_settings_and_a_sane_device_list_fit_together(self):
        from videoserver.control import _devices_that_fit

        source = app()
        payload = {"settings": source.settings.to_dict()}
        devices = [
            {
                "name": "AVerMedia Live Gamer HD 2",
                "id": "@device_pnp_0001",
                "kind": "video",
            },
            {
                "name": "Realtek Digital Audio Input",
                "id": "@device_cm_0002",
                "kind": "audio",
            },
        ]
        payload["devices"] = _devices_that_fit(payload, devices)
        assert payload["devices"] == devices
        size = len(protocol.encode_control(1, "VIDEO_STATUS", payload))
        assert size <= protocol.MAX_DATAGRAM

    def test_an_absurd_device_list_is_trimmed_rather_than_refused(self):
        """Most of the devices beats none of them, which is what refusing gives."""
        from videoserver.control import _devices_that_fit

        source = app()
        payload = {"settings": source.settings.to_dict()}
        devices = [
            {
                "name": f"Some Capture Device Model {i} (USB 3.0)",
                "id": f"@device_pnp_{i:04}",
                "kind": "video",
            }
            for i in range(64)
        ]
        kept = _devices_that_fit(payload, devices)
        assert 0 < len(kept) < len(devices)
        assert kept == devices[: len(kept)]
        payload["devices"] = kept
        size = len(protocol.encode_control(1, "VIDEO_STATUS", payload))
        assert size <= protocol.MAX_DATAGRAM

    def test_no_devices_is_not_an_error(self):
        from videoserver.control import _devices_that_fit

        assert _devices_that_fit({"settings": {}}, []) == []


class TestTheSourceReportsItsLayout:
    def test_a_fresh_source_reports_full(self):
        assert app().status()["layout"] == {
            "mode": FULL,
            "confidence": 0.0,
            "source": "auto",
            # No bars until a frame has been looked at, which is also the
            # value everything falls back to.
            "active": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
        }

    def test_detection_off_costs_nothing(self):
        """Not merely 'returns False' -- it must not look at a frame at all."""
        source = app()
        assert source.sample_layout() is False
        assert source.status()["detector"] == {}

    def test_an_override_is_reported_as_one(self):
        source = app()
        source.apply_config(VideoSettings(split_override=QUAD_4))
        block = source.status()["layout"]
        assert block["mode"] == QUAD_4
        assert block["source"] == "override"

    def test_an_override_survives_an_unrelated_config_change(self):
        source = app()
        source.apply_config(VideoSettings(split_override=QUAD_4, bitrate_kbps=6000))
        source.apply_config(VideoSettings(split_override=QUAD_4, bitrate_kbps=9000))
        assert source.status()["layout"]["mode"] == QUAD_4

    def test_detection_settings_do_not_restart_the_media(self):
        """They are a layout decision, not a capture or encode one. Restarting
        the device to change a confidence threshold would drop every frame in
        flight for nothing."""
        source = app()
        source._running = True
        restarts = []
        source._restart_media = lambda: restarts.append("media")
        source._restart_encoders = lambda: restarts.append("encoders")
        source.apply_config(
            VideoSettings(
                split_detect_enabled=True,
                split_detect_width=480,
                split_detect_confidence=0.9,
                split_override=VERTICAL_2,
            )
        )
        assert restarts == []


class TestTheRegistryAbsorbsIt:
    def test_nothing_reported_is_full(self):
        assert VideoRegistry().layout == FULL

    def test_it_takes_what_the_source_says(self):
        registry = VideoRegistry()
        registry.attach_source_endpoint("10.0.0.5", 47810)
        registry.update_status_from_link({"status": {"layout": {"mode": QUAD_4}}})
        assert registry.layout == QUAD_4

    def test_a_change_is_something_clients_would_notice(self):
        registry = VideoRegistry()
        registry.attach_source_endpoint("10.0.0.5", 47810)
        registry.update_status_from_link({"status": {"layout": {"mode": FULL}}})

        changed = registry.update_status_from_link(
            {"status": {"layout": {"mode": QUAD_4}}}
        )
        assert changed is True
        # And re-reporting the same layout is not, or every status would
        # re-push an advert to every client once a second.
        again = registry.update_status_from_link(
            {"status": {"layout": {"mode": QUAD_4}}}
        )
        assert again is False

    @pytest.mark.parametrize(
        "body",
        [
            {"status": {"layout": "nonsense"}},
            {"status": {"layout": {"mode": "QUAD_5"}}},
            {"status": {"layout": {}}},
            {"status": {}},
            {},
        ],
    )
    def test_anything_unreadable_is_full(self, body):
        registry = VideoRegistry()
        registry.attach_source_endpoint("10.0.0.5", 47810)
        registry.update_status_from_link(body)
        assert registry.layout == FULL

    def test_losing_the_source_forgets_the_layout(self):
        """A layout held over from a dead source would crop every client to a
        division of a picture that no longer exists -- and look like a working
        feature while doing it."""
        registry = VideoRegistry()
        registry.attach_source_endpoint("10.0.0.5", 47810)
        registry.update_status_from_link({"status": {"layout": {"mode": QUAD_4}}})
        assert registry.layout == QUAD_4

        registry.detach_source("video-link")
        assert registry.layout == FULL

    def test_a_new_source_does_not_inherit_the_old_one(self):
        registry = VideoRegistry()
        registry.attach_source_endpoint("10.0.0.5", 47810)
        registry.update_status_from_link({"status": {"layout": {"mode": QUAD_4}}})

        registry.attach_source_endpoint("10.0.0.6", 47810)
        assert registry.layout == FULL
