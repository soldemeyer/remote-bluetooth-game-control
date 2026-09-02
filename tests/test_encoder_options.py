"""Encoder options that must actually reach the encoder in use.

`intra_refresh` was a setting that did nothing on the encoder this project
actually runs. The flag was applied only inside the ``libx264`` branch of
`configure_low_latency`, so on any machine that picks NVENC -- which is every
machine with an NVIDIA card, including the reference machine -- switching it on
in the web GUI changed nothing and said nothing.

That is the same shape as the ``vendor_id`` / ``product_id`` and
``advertise_host`` dead parameters CLAUDE.md already documents: the feature
looks present and is absent. These tests exist so the next option added has to
prove it reaches every encoder rather than just the first branch someone wrote.
"""

from __future__ import annotations

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from common.video import VideoSettings                      # noqa: E402
from videoserver.encode import (                            # noqa: E402
    ENCODER_CHAIN_PC,
    ENCODER_CHAIN_PI,
    configure_low_latency,
    intra_refresh_options,
    keyframe_options,
)

#: Encoders that are supposed to support intra refresh. h264_v4l2m2m is
#: deliberately absent: it has no such control, and claiming one would be the
#: original bug in a new place.
INTRA_CAPABLE = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf")


def _configured(name: str, *, intra: bool) -> dict:
    """The options `configure_low_latency` would hand this encoder."""
    ctx = av.CodecContext.create(name, "w")
    ctx.width, ctx.height = 640, 480
    settings = VideoSettings(
        width=640, height=480, fps=30, bitrate_kbps=2000, intra_refresh=intra
    )
    configure_low_latency(ctx, name, settings)
    return dict(ctx.options)


def _applied(name: str, options: dict) -> bool:
    want = intra_refresh_options(name)
    if not want:
        return False
    for key, value in want.items():
        if key == "x264-params":
            if value not in options.get(key, ""):
                return False
        elif options.get(key) != value:
            return False
    return True


class TestIntraRefreshReachesEveryEncoder:
    @pytest.mark.parametrize("name", INTRA_CAPABLE)
    def test_it_is_applied_when_asked_for(self, name):
        if name not in av.codecs_available:
            pytest.skip(f"{name} is not in this PyAV build")
        assert _applied(name, _configured(name, intra=True)), (
            f"intra_refresh does not reach {name}"
        )

    @pytest.mark.parametrize("name", INTRA_CAPABLE)
    def test_it_is_absent_when_not_asked_for(self, name):
        if name not in av.codecs_available:
            pytest.skip(f"{name} is not in this PyAV build")
        assert not _applied(name, _configured(name, intra=False))

    def test_nvenc_specifically(self):
        """The encoder the bug was invisible on.

        Named on its own rather than left to the parametrised case, because
        this is the one that was silently broken and the one most machines
        will pick.
        """
        if "h264_nvenc" not in av.codecs_available:
            pytest.skip("NVENC is not in this PyAV build")
        options = _configured("h264_nvenc", intra=True)
        assert options.get("intra-refresh") == "1"

    def test_x264_keeps_its_other_parameters(self):
        """x264 takes everything in one colon-separated string.

        Assigning over it rather than appending would silently drop
        `sliced-threads`, `rc-lookahead=0` and `scenecut=0` -- every one of
        which the module docstring explains is load-bearing for latency.
        """
        options = _configured("libx264", intra=True)
        params = options["x264-params"]
        for required in ("sliced-threads=1", "rc-lookahead=0", "scenecut=0",
                         "intra-refresh=1"):
            assert required in params, f"{required} was lost from x264-params"


class TestAnEncoderWithoutTheFeatureSaysSo:
    def test_v4l2m2m_claims_nothing(self):
        # The Pi's hardware encoder has no intra-refresh control. Returning a
        # plausible-looking option for it would put the original bug back.
        assert intra_refresh_options("h264_v4l2m2m") == {}

    def test_an_unknown_encoder_claims_nothing(self):
        assert intra_refresh_options("h264_something_new") == {}


class TestEveryEncoderInTheChainIsAccountedFor:
    """A new encoder must be a deliberate decision, not an omission."""

    @pytest.mark.parametrize("name", sorted(set(ENCODER_CHAIN_PC + ENCODER_CHAIN_PI)))
    def test_intra_refresh_support_is_stated_either_way(self, name):
        supported = bool(intra_refresh_options(name))
        expected = name in INTRA_CAPABLE
        assert supported == expected, (
            f"{name} is in the encoder chain but its intra-refresh support "
            f"is undeclared; state it in intra_refresh_options()"
        )

    @pytest.mark.parametrize("name", sorted(set(ENCODER_CHAIN_PC + ENCODER_CHAIN_PI)))
    def test_keyframe_support_is_stated_either_way(self, name):
        # libx264 and v4l2m2m need nothing; the hardware encoders do.
        needs_none = name in ("libx264", "h264_v4l2m2m")
        assert bool(keyframe_options(name)) != needs_none
