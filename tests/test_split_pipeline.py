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

    def test_it_still_fits_with_player_identification_running(self):
        """The case that was 8 bytes from refusing, and that this class
        missed for a release.

        The old guard only ever built a status with identification **off**,
        and the live three-process run used the *inline* backend with two
        players -- 1073 bytes, comfortably inside. An isolated backend with a
        four-player roster reached 1187 of 1195, and grew past the ceiling as
        soon as a counter gained a digit.
        """
        source = app()
        source._players._runner = _FakeRunner()
        source._players._caps = _caps()
        size = len(status_message(source))
        assert size <= protocol.MAX_DATAGRAM
        assert protocol.MAX_DATAGRAM - size > 250, (
            f"VIDEO_STATUS is {size} bytes with identification running"
        )

    def test_the_states_that_used_to_burst_it(self):
        """An hour of play, a backend that gave up, and a provider that is
        unavailable with its reason.

        The last two are the states where a status is worth having, which is
        what made the old failure so unkind: it went quiet exactly when
        somebody needed it to speak.
        """
        cases = {
            "an hour of play": (_FakeRunner(big=True), _caps()),
            "a backend that gave up": (
                _FakeRunner(failed="RuntimeError: CUDA error: out of memory"),
                _caps(),
            ),
            "unavailable, with a reason": (
                _FakeRunner(),
                _caps(
                    available=False,
                    device="",
                    reason=(
                        "onnxruntime is not installed -- pip install "
                        '"remote-bluetooth-game-control[playervision]"'
                    ),
                ),
            ),
        }
        for name, (runner, caps) in cases.items():
            source = app()
            # With split detection running too, so each axis's strength rides
            # the layout block -- the readouts that let an operator see why a
            # layout is being held. Measured without them this guard would say
            # nothing about the state that actually carries them.
            source.apply_config(VideoSettings(split_detect_enabled=True))
            source._layout_state.vertical = 0.87
            source._layout_state.horizontal = 0.04
            source._players._runner = runner
            source._players._caps = caps
            message = status_message(source)
            assert b'"v":0.87' in message, name
            size = len(message)
            assert size <= protocol.MAX_DATAGRAM, f"{name}: {size} bytes, refused"
            assert protocol.MAX_DATAGRAM - size > 250, f"{name}: {size} bytes"

    def test_an_unbounded_reason_cannot_burst_it(self):
        """A model path or an ORT stack trace is the only unbounded string in
        the block, and this message refuses whole rather than truncating."""
        source = app()
        source._players._runner = _FakeRunner()
        source._players._caps = _caps(available=False, reason="deep/path/" * 200)
        assert len(status_message(source)) <= protocol.MAX_DATAGRAM

    def test_the_detail_is_not_on_the_status(self):
        """It rides the slow message instead. Asserted by name, because the
        fix is only a fix while these stay off the fast one."""
        source = app()
        source._players._runner = _FakeRunner()
        source._players._caps = _caps()
        block = source.status()["player_id"]
        for key in ("identity", "tracks", "slot_reads", "slot_torn", "skipped",
                    # On `Capabilities` for the child-to-parent channel, and
                    # not here: adding it splatted 33 bytes onto this message
                    # without anybody asking.
                    "input_width", "input_height"):
            assert key not in block, f"{key} is back on the status message"

    def test_the_fields_the_web_gui_reads_are_the_shape_it_expects(self):
        """A second live bug, found while slimming this.

        `report.update(runner.snapshot())` replaced the capability `backend`
        -- a string -- with the child's nested backend dict, so the web GUI's
        `Running ${report.backend}` would have rendered `[object Object]`.
        The same class of mistake as the Video tile reading four fields the
        status never had: a plausible read of the wrong object, which shows as
        a confidently wrong display rather than a missing one.
        """
        source = app()
        source._players._runner = _FakeRunner()
        source._players._caps = _caps()
        block = source.status()["player_id"]

        assert isinstance(block["backend"], str)
        assert isinstance(block["device"], str)
        assert isinstance(block["reason"], str)
        assert isinstance(block["available"], bool)
        assert isinstance(block["embeddings"], bool)

    def test_a_long_error_list_cannot_burst_it(self):
        """Errors are the other variable-length thing in the status."""
        source = app()
        for i in range(50):
            source._record_error(f"a fairly wordy capture failure number {i}")
        assert len(status_message(source)) <= protocol.MAX_DATAGRAM


def _caps(**over):
    from videoserver.playervision.backends.base import Capabilities

    values = dict(
        backend="onnx", available=True, reason="",
        device="CUDAExecutionProvider", embeddings=True,
    )
    values.update(over)
    return Capabilities(**values)


class _FakeRunner:
    """An isolated runner mid-session, with the counters it really reports."""

    def __init__(self, *, big=False, failed=""):
        self._big = big
        self._failed = failed

    def snapshot(self):
        scale = 265 if self._big else 1
        return {
            "runner": "process", "pid": 29424, "alive": True,
            "restarts": 0, "failed": self._failed,
            "slot": {"writes": 812 * scale, "oversized": 0},
            "frames": 806 * scale, "failures": 0,
            "layout": "QUAD_4", "players": 4,
            "tracks": {
                "live": 7, "created": 164 * scale, "dropped": 157 * scale,
            },
            "identity": {
                "players": 4,
                "exemplars": {"1": 8, "2": 8, "3": 6, "4": 7},
                "refused": 12 * scale,
                "assignments": 1893 * scale,
                "ambiguous": 41 * scale,
            },
            "slot_reads": 806 * scale, "slot_torn": 0,
            "backend": {
                "backend": "onnx", "provider": "CUDAExecutionProvider",
                "layout": "post_nms", "frames": 806 * scale,
                "detections": 2418 * scale, "embed_failures": 0,
            },
        }

    def latest(self):
        return []


class TestTheDetailRidesTheSlowMessage:
    """Where the counters went, and what asks for them.

    They were on the status, which has no room. `player_id_debug` was a
    setting declared with no job; this is the job.
    """

    @staticmethod
    def _payload(source):
        """What `_send_player_stats` builds -- a message of its own."""
        if not source.settings.player_id_debug:
            return {}
        stats = source.player_id_stats()
        return {"player_id_stats": stats} if stats else {}

    def test_nothing_is_carried_when_the_debug_view_is_off(self):
        source = app()
        source._players._runner = _FakeRunner()
        source._players._caps = _caps()
        assert "player_id_stats" not in self._payload(source)

    def test_the_counters_are_carried_when_it_is_on(self):
        source = app()
        source.apply_config(VideoSettings(player_id_debug=True))
        source._players._runner = _FakeRunner()
        source._players._caps = _caps()
        stats = self._payload(source)["player_id_stats"]
        assert stats["identity"]["assignments"]
        assert stats["tracks"]["created"]

    def test_nothing_is_carried_when_identification_is_not_running(self):
        source = app()
        source.apply_config(VideoSettings(player_id_debug=True))
        assert "player_id_stats" not in self._payload(source)

    def test_a_busy_session_fits_its_own_message(self):
        """An hour of counters, alone on the wire.

        Sharing the slow message with the settings and the device list came to
        1369 bytes -- measured -- and `encode_control` refuses whole. That is
        the third time two variable-length structures in one message has
        broken this, which is why these get their own.
        """
        source = app()
        source.apply_config(VideoSettings(player_id_debug=True))
        source._players._runner = _FakeRunner(big=True)
        source._players._caps = _caps()

        size = len(protocol.encode_control(1, "VIDEO_STATUS", self._payload(source)))
        assert size <= protocol.MAX_DATAGRAM, f"{size} bytes"
        assert protocol.MAX_DATAGRAM - size > 250, f"{size} bytes and nearly full"

    def test_the_slow_message_did_not_grow(self):
        """The counters went somewhere else, not somewhere else *as well*."""
        from videoserver.control import _devices_that_fit

        source = app()
        source.apply_config(VideoSettings(player_id_debug=True))
        source._players._runner = _FakeRunner(big=True)
        source._players._caps = _caps()

        payload = {"settings": source.settings.to_dict()}
        assert "player_id_stats" not in payload
        devices = _devices_that_fit(
            payload,
            [{"name": f"Capture Device Number {i}", "kind": "video"} for i in range(8)],
        )
        if devices:
            payload["devices"] = devices
        size = len(protocol.encode_control(1, "VIDEO_STATUS", payload))
        assert size <= protocol.MAX_DATAGRAM, f"{size} bytes"


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
