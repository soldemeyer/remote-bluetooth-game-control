"""The preview only flows while somebody is looking at it.

Preview slices are decoded and reassembled on the **datapath thread**, the one
with a sub-millisecond budget. The preview used to stream from the moment a
source connected, whether or not any browser had the panel open -- so the cost
was permanent, and it had to be kept small and slow (320 px, 5 fps) to be
affordable at all.

Gating on demand is what pays for the resolution: nothing flows until the web
GUI fetches a frame, and it stops again shortly after it stops asking.
"""

from __future__ import annotations

from common.video import VideoSettings
from server.video import MODE_EXTERNAL, PREVIEW_DEMAND_NS, VideoRegistry


def _registry(**overrides) -> VideoRegistry:
    settings = VideoSettings(**overrides)
    registry = VideoRegistry(mode=MODE_EXTERNAL, settings=settings, configured=True)
    registry.attach_source_endpoint("192.168.1.16", 47810)
    return registry


class TestNobodyIsLooking:
    def test_a_fresh_server_does_not_ask_for_previews(self):
        registry = _registry()

        assert registry.preview_wanted() is False
        assert registry.config_message()["preview_wanted"] is False

    def test_fetching_a_frame_turns_it_on(self):
        registry = _registry()

        registry.preview()      # what the web GUI's poll does

        assert registry.preview_wanted() is True
        assert registry.config_message()["preview_wanted"] is True

    def test_it_lapses_once_the_panel_is_closed(self):
        registry = _registry()
        registry.preview()

        # Wind the clock back rather than sleeping five seconds.
        registry._preview_asked_ns -= PREVIEW_DEMAND_NS + 1

        assert registry.preview_wanted() is False
        assert registry.config_message()["preview_wanted"] is False

    def test_demand_never_rides_on_a_persisted_setting(self):
        """The bug that killed the preview for good.

        The server has nothing saved, so it adopts the source's settings when
        it connects -- and the source holds whatever it was last pushed. Put a
        transient "somebody is looking" signal into `preview_enabled` and a
        restart while nobody was looking adopts `false` as the operator's own
        choice, with no control in either GUI able to undo it.
        """
        registry = _registry()
        message = registry.config_message()

        assert message["config"]["preview_enabled"] is True, (
            "demand leaked into a setting the source reports back and we adopt"
        )
        assert message["preview_wanted"] is False


class TestDemandDrivesAPush:
    def _drain(self, registry: VideoRegistry) -> None:
        """Settle into "the source has everything", so later pushes are attributable.

        `config_message()` is what records the preview state we last sent;
        acknowledging the sequence is what the source's status would do.
        """
        registry.config_message()
        registry._applied_seq = registry._cfg_seq
        registry._last_pushed_ns = 0

    def test_opening_the_panel_schedules_a_push(self):
        registry = _registry()
        self._drain(registry)
        assert not registry.needs_config_push(), "nothing changed yet"

        registry.preview()

        assert registry.needs_config_push(), (
            "the source was never told to start sending previews"
        )

    def test_closing_it_schedules_one_too(self):
        registry = _registry()
        registry.preview()
        self._drain(registry)

        registry._preview_asked_ns -= PREVIEW_DEMAND_NS + 1
        registry._last_pushed_ns = 0

        assert registry.needs_config_push(), (
            "previews would keep costing the datapath with nobody watching"
        )

    def test_a_steady_state_does_not_churn(self):
        registry = _registry()
        registry.preview()
        self._drain(registry)
        registry._last_pushed_ns = 0

        registry.preview()      # still watching

        assert not registry.needs_config_push()

    def test_demand_is_not_persisted_as_a_setting(self):
        """It is a live fact about who is looking, not an operator choice.

        Storing it would leave the preview switched off after a restart, with
        the checkbox still saying it is on.
        """
        registry = _registry()
        registry.preview()
        registry.config_message()

        assert registry.settings.preview_enabled is True


class TestTheSizeIsTheOperatorsChoice:
    def test_the_default_is_no_longer_tiny(self):
        assert VideoSettings().preview_width == 640

    def test_it_is_clamped_to_something_a_jpeg_fits_in(self):
        assert VideoSettings(preview_width=4000).clamped().preview_width == 1280
        assert VideoSettings(preview_width=1).clamped().preview_width == 160

    def test_the_frame_rate_ceiling_was_raised(self):
        assert VideoSettings(preview_fps=30).clamped().preview_fps == 30
        assert VideoSettings(preview_fps=99).clamped().preview_fps == 30

    def test_it_travels_to_the_source(self):
        registry = _registry(preview_width=960, preview_fps=15)

        config = registry.config_message()["config"]

        assert config["preview_width"] == 960
        assert config["preview_fps"] == 15


class TestTheFrameCapAllowsTheLargestPreview:
    def test_the_two_caps_agree(self):
        """The reassembler must accept whatever the source will send."""
        pytest_importorskip = __import__("pytest").importorskip
        pytest_importorskip("av", reason="video extras not installed")

        from videoserver.preview import MAX_PREVIEW_BYTES as source_cap

        from server.video import MAX_PREVIEW_BYTES as server_cap

        assert server_cap >= source_cap, (
            "a large preview frame would cross the network and then be dropped"
        )
