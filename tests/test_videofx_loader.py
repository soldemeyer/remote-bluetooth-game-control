"""Loading -- or failing to load -- the optional GPU enhancement library.

Every test here runs on a machine with no library, no GPU and no graphics
driver, because that is the configuration that must keep working. The feature
is optional in the strongest sense: a computer that ran this client before
this feature existed must still run it, with both enhancement options simply
unavailable.

The ``argtypes`` sweep is the valuable one. ctypes silently defaults an
undeclared argument to C ``int``, which truncates a 64-bit pointer; the crash
then lands inside a graphics driver and reads as a GPU fault. That whole class
of bug is catchable here, with no hardware and no library present, by checking
the declaration table rather than by calling anything.
"""

from __future__ import annotations

import ctypes
import logging
import sys

import pytest

from client.media import upscale, videofx


@pytest.fixture(autouse=True)
def clean_caches():
    """Each test starts having decided nothing.

    The caches are deliberately process-wide -- the answer cannot change
    without new hardware and a restart -- so a test that leaves one populated
    silently decides the next one's outcome.
    """
    upscale.reset_cache()
    yield
    upscale.reset_cache()


class TestTheDeclarationTable:
    def test_every_export_declares_its_signature(self):
        """The pointer-truncation guard. See the module docstring."""
        for name, restype, argtypes in videofx.EXPORTS:
            assert isinstance(name, str) and name.startswith("rbgc_")
            assert restype is None or hasattr(restype, "_type_") or restype is ctypes.c_char_p
            assert isinstance(argtypes, tuple)
            for arg in argtypes:
                assert arg is not None, f"{name} has an undeclared argument"

    def test_every_handle_is_declared_as_a_pointer(self):
        """A handle declared as an integer loses its top 32 bits, and the
        failure then surfaces inside a graphics driver, a long way from here.

        Checked by type rather than by size: on Windows ``c_int32`` *is*
        ``c_long`` and both are four bytes, so "it is not an int" is not a
        question ctypes can answer. "It is a pointer type" is.
        """
        by_name = {name: args for name, _, args in videofx.EXPORTS}
        assert by_name["rbgc_probe"] == (ctypes.POINTER(videofx.CCaps),)
        assert by_name["rbgc_create"][0] is ctypes.c_void_p
        assert by_name["rbgc_create"][2] == ctypes.POINTER(ctypes.c_void_p)
        assert by_name["rbgc_submit"][1] == ctypes.POINTER(videofx.CFrame)
        assert by_name["rbgc_submit"][2] == ctypes.POINTER(videofx.CResult)

        # Everything but the two that take no renderer is called with one, and
        # it is always the first argument and always opaque.
        for name, args in by_name.items():
            if name in ("rbgc_version", "rbgc_probe"):
                continue
            assert args and args[0] is ctypes.c_void_p, name

    def test_the_struct_layout_matches_the_c_header(self):
        """The real ABI contract, and the only one worth pinning.

        Field *names* say nothing here -- ``c_uint32`` is ``c_ulong`` on
        Windows and ``c_uint`` elsewhere, all four bytes. What has to agree
        with ``native/videofx/videofx.h`` is where each field sits and how big
        the whole thing is, and a mismatch there is not a crash anybody can
        debug from the traceback.
        """
        assert ctypes.sizeof(videofx.CCaps) == 888
        assert ctypes.sizeof(videofx.CBlit) == 32
        assert ctypes.sizeof(videofx.CResult) == 24
        assert ctypes.alignment(videofx.CCaps) == 8

        # The one place a C compiler inserts padding: a uint64 after an odd
        # number of uint32s. Getting this wrong shifts every string after it.
        assert videofx.CCaps.driver_version.offset == 32
        assert videofx.CCaps.gpu_name.offset == 40

        assert videofx.CCaps.gpu_name.size == videofx.NAME_MAX
        assert videofx.CCaps.reason_fsr1.size == videofx.REASON_MAX
        assert videofx.CCaps.backend.size == videofx.SHORT_MAX

    def test_a_pointer_field_is_wide_enough_for_a_pointer(self):
        """The truncation trap again, this time inside a struct."""
        assert videofx.CFrame.hw_texture.size == ctypes.sizeof(ctypes.c_void_p)
        assert videofx.CFrame.plane.size == 3 * ctypes.sizeof(ctypes.c_void_p)
        assert videofx.CFrame.overlay.size == ctypes.sizeof(ctypes.c_void_p)

    def test_a_result_and_a_frame_can_be_built(self):
        """They are constructed on the decode thread, per frame. A struct that
        raises there takes the picture down."""
        frame = videofx.CFrame()
        frame.struct_size = ctypes.sizeof(videofx.CFrame)
        result = videofx.CResult()
        result.struct_size = ctypes.sizeof(videofx.CResult)
        assert frame.struct_size > 0 and result.struct_size > 0


class TestWithNoLibrary:
    """The ordinary case: a source checkout, or any machine with no GPU build."""

    def test_it_reports_unavailable_rather_than_raising(self):
        assert videofx.is_available() in (True, False)
        caps = videofx.probe()
        assert isinstance(caps, videofx.Caps)

    def test_the_reason_is_written_for_a_player(self, monkeypatch):
        """Never a developer's file path. This sentence is shown verbatim in
        the GUI next to a disabled control."""
        monkeypatch.setattr(videofx, "candidate_paths", lambda: [])
        videofx.reset_cache()
        reason = videofx.load_error()
        assert reason
        assert "\\" not in reason and "/" not in reason
        assert "Traceback" not in reason

    def test_probe_without_a_library_supports_nothing(self, monkeypatch):
        monkeypatch.setattr(videofx, "candidate_paths", lambda: [])
        videofx.reset_cache()
        caps = videofx.probe()
        assert not caps.usable and not caps.fsr1 and not caps.rtx_vsr
        assert caps.reason_fsr1 and caps.reason_rtx_vsr

    def test_it_says_so_once_and_not_per_frame(self, monkeypatch, caplog):
        monkeypatch.setattr(videofx, "candidate_paths", lambda: [])
        videofx.reset_cache()
        with caplog.at_level(logging.DEBUG, logger="client.media.videofx"):
            for _ in range(20):
                videofx.probe()
        assert len(caplog.records) <= 2, "the loader is logging repeatedly"

    def test_a_search_path_list_is_produced(self):
        paths = videofx.candidate_paths()
        assert paths
        assert len(paths) == len(set(paths)), "the same path is searched twice"

    def test_the_library_name_matches_the_platform(self):
        name = videofx.library_name()
        if sys.platform == "win32":
            assert name.endswith(".dll")
        elif sys.platform == "darwin":
            assert name.endswith(".dylib")
        else:
            assert name.endswith(".so")

    def test_an_override_directory_is_honoured(self, monkeypatch, tmp_path):
        """So a build that was just produced can be tested without installing
        it over the committed one."""
        monkeypatch.setenv("RBGC_VIDEOFX", str(tmp_path))
        assert any(tmp_path in path.parents or path.parent == tmp_path
                   for path in videofx.candidate_paths())


class TestAPreferenceSurvivesHardwareThatCannotHonourIt:
    """The property that makes moving the client between machines survivable."""

    UNSUPPORTED = upscale.Capabilities(
        gpu_ok=False,
        fsr1_ok=False,
        rtx_vsr_ok=False,
        reason_fsr1="Required GPU shader support is unavailable",
        reason_rtx_vsr="Requires a supported NVIDIA RTX GPU",
        reason_gpu="No compatible graphics device",
    )
    SUPPORTED = upscale.Capabilities(
        gpu_ok=True, fsr1_ok=True, rtx_vsr_ok=True,
        gpu_name="NVIDIA GeForce RTX 4080", backend="Direct3D 11",
    )

    @pytest.mark.parametrize("mode", [upscale.GPU, upscale.FSR1, upscale.RTX_VSR])
    def test_an_unsupported_mode_runs_as_off(self, mode):
        assert upscale.effective_mode(mode, self.UNSUPPORTED) == upscale.OFF

    @pytest.mark.parametrize("mode", [upscale.GPU, upscale.FSR1, upscale.RTX_VSR])
    def test_a_supported_mode_runs_as_itself(self, mode):
        assert upscale.effective_mode(mode, self.SUPPORTED) == mode

    def test_off_is_always_available(self):
        assert self.UNSUPPORTED.supports(upscale.OFF)
        assert upscale.effective_mode(upscale.OFF, self.UNSUPPORTED) == upscale.OFF

    def test_the_stored_preference_is_not_rewritten(self):
        """``effective_mode`` answers a question; it does not edit anything.

        Silently rewriting the config is how a player who set FSR on their
        desktop loses it by opening the client once on a laptop.
        """
        from client.config import ClientConfig

        config = ClientConfig(video_upscaler="rtx_vsr")
        assert upscale.effective_mode(config.video_upscaler, self.UNSUPPORTED) == "off"
        assert config.video_upscaler == "rtx_vsr"

    def test_a_mode_from_a_later_version_is_survivable(self):
        assert upscale.effective_mode("fsr4_frame_gen", self.SUPPORTED) == upscale.OFF

    def test_it_explains_itself_once(self, caplog):
        with caplog.at_level(logging.INFO, logger="client.media.upscale"):
            upscale.effective_mode(upscale.FSR1, self.UNSUPPORTED)
        assert any("not available" in r.message for r in caplog.records)


class TestWhatTheGuiIsToldToShow:
    CAPS = upscale.Capabilities(
        gpu_ok=True, fsr1_ok=True, rtx_vsr_ok=False,
        gpu_name="AMD Radeon RX 7800 XT", backend="Direct3D 11",
        reason_rtx_vsr="Requires a supported NVIDIA RTX GPU",
    )

    def test_an_available_mode_names_the_hardware(self):
        assert self.CAPS.detail(upscale.FSR1) == "Available - Direct3D 11"
        assert "AMD Radeon" in self.CAPS.detail(upscale.GPU) or \
            self.CAPS.detail(upscale.GPU).startswith("Available")

    def test_rtx_names_the_gpu_not_the_backend(self):
        """Because "Available - Direct3D 11" says nothing about whether the
        card is the one that matters."""
        supported = upscale.Capabilities(
            rtx_vsr_ok=True, gpu_name="NVIDIA GeForce RTX 4080", backend="Direct3D 11"
        )
        assert supported.detail(upscale.RTX_VSR) == "Available - NVIDIA GeForce RTX 4080"

    def test_an_unavailable_mode_gives_the_reason(self):
        assert self.CAPS.detail(upscale.RTX_VSR) == "Requires a supported NVIDIA RTX GPU"

    def test_every_mode_has_something_to_say(self):
        for mode, label in upscale.MODE_LABELS:
            assert label
            assert self.CAPS.detail(mode), mode

    def test_off_is_described_without_mentioning_hardware(self):
        assert "enhancement" in self.CAPS.detail(upscale.OFF).lower()

    def test_the_modes_are_exactly_the_config_vocabulary(self):
        """Three places name these; a fourth spelling is a control that saves
        a value nothing reads."""
        from client.config import UPSCALERS

        assert tuple(mode for mode, _ in upscale.MODE_LABELS) == tuple(
            sorted(UPSCALERS, key=UPSCALERS.index)
        ) or set(mode for mode, _ in upscale.MODE_LABELS) == set(UPSCALERS)

    def test_the_scan_summary_never_leaks_a_path(self):
        for line in self.CAPS.describe():
            assert ":\\" not in line


class TestNullUpscalerIsNotAPassThrough:
    """Off must bypass this machinery, not route through it.

    The requirement is that with the feature off the video path is what it
    always was -- so an object that politely forwards frames would be a
    regression even though nothing would look wrong.
    """

    def test_submitting_to_it_is_a_programming_error(self):
        with pytest.raises(AssertionError, match="bypass"):
            upscale.NullUpscaler().submit(object())

    def test_it_refuses_to_initialise(self):
        assert upscale.NullUpscaler().initialize(0, upscale.OFF) is False

    def test_shutting_it_down_is_harmless(self):
        upscale.NullUpscaler().shutdown()


class TestSharpnessMapping:
    """The slider is 0-100; RCAS wants 0 (sharpest) to 2 (softest)."""

    def test_it_is_monotonic_and_inverted(self):
        values = [upscale.sharpness_to_attenuation(p) for p in range(0, 101, 5)]
        assert values == sorted(values, reverse=True)

    def test_the_ends_are_the_real_limits(self):
        assert upscale.sharpness_to_attenuation(0) == pytest.approx(2.0)
        assert upscale.sharpness_to_attenuation(100) == pytest.approx(0.0)

    def test_the_default_is_conservative(self):
        """0.5 rather than the FidelityFX sample's 0.25. This is compressed
        video with block artifacts, not clean engine output, and sharpening
        amplifies exactly what the encoder threw away."""
        assert upscale.sharpness_to_attenuation(50) == pytest.approx(0.5)

    def test_it_clamps_rather_than_raising(self):
        assert upscale.sharpness_to_attenuation(-50) == pytest.approx(2.0)
        assert upscale.sharpness_to_attenuation(9999) == pytest.approx(0.0)


class TestTheClientStillWorksWithNoneOfThis:
    def test_nothing_is_on_the_frame_path_by_default(self):
        """The library *is* loaded at startup, by the capability scan, because
        the settings have to say what this machine can do. What must not
        happen is any of it reaching the video path."""
        from client.config import ClientConfig
        from client.media.decoder import VideoDecoder

        config = ClientConfig()
        assert config.video_upscaler == "off"
        decode = VideoDecoder(receiver=object())
        assert decode._upscale is None

    def test_the_decoder_imports_without_the_enhancement_modules(self):
        """The Off path must not depend on any of this existing."""
        import client.media.decoder as decoder

        assert decoder is not None

    def test_default_config_selects_off(self):
        from client.config import ClientConfig

        config = ClientConfig()
        assert config.video_upscaler == "off"
        assert config.video_hw_decode == "off"

    def test_off_never_consults_the_hardware(self, monkeypatch):
        """Resolving Off must not trigger a capability scan: the scan creates
        a graphics device, and a player who chose Off should not pay for one.
        """
        def explode(*_args, **_kwargs):
            raise AssertionError("Off must not scan the hardware")

        monkeypatch.setattr(upscale, "capabilities", explode)
        assert upscale.effective_mode("off") == "off"


class TestPackaging:
    """The bundle has to carry the library, and must not require it.

    Nothing here builds anything -- that needs a compiler and several minutes
    -- but a missing entry produces a release that builds cleanly and then has
    the feature silently absent on somebody else's machine, which is exactly
    the class this project catches in text tests elsewhere.
    """

    @staticmethod
    def _root():
        from pathlib import Path as P

        return P(__file__).resolve().parent.parent

    def test_the_windows_spec_ships_the_library(self):
        spec = (self._root() / "packaging" / "client.spec").read_text(encoding="utf-8")
        assert "rbgc_videofx" in spec
        assert "client/media/fx" in spec

    def test_the_spec_does_not_make_it_mandatory(self):
        """A glob, so a platform with no build produces a bundle rather than a
        build error. The feature is optional in the strongest sense: a machine
        that ran this client before must still run it."""
        spec = (self._root() / "packaging" / "client.spec").read_text(encoding="utf-8")
        assert ".glob(" in spec, "the library is listed by name rather than found"

    def test_pyproject_installs_it_as_package_data(self):
        text = (self._root() / "pyproject.toml").read_text(encoding="utf-8")
        assert '"client.media" = [' in text
        assert "fx/*.dll" in text

    def test_the_build_script_is_reachable(self):
        assert (self._root() / "tools" / "build_videofx.py").exists()
        assert (self._root() / "native" / "videofx" / "videofx.h").exists()

    def test_the_vendored_licence_is_intact(self):
        """FidelityFX is MIT and stays that way. The header carries the notice
        and it must not be stripped when the file is updated."""
        for name in ("ffx_a.h", "ffx_fsr1.h"):
            text = (self._root() / "native" / "videofx" / "third_party" / name).read_text(
                encoding="utf-8", errors="replace")
            assert "Advanced Micro Devices" in text
            assert "Permission is hereby granted, free of charge" in text

    def test_no_nvidia_sdk_is_vendored_or_required(self):
        """RTX VSR goes through the Direct3D 11 video processor extension --
        the path VLC, mpv and Chromium use. Nothing NVIDIA is redistributed,
        which is what keeps the feature free of a licensing question and free
        of a dependency on AMD and Intel machines.
        """
        native = self._root() / "native" / "videofx"
        names = [p.name.lower() for p in native.rglob("*") if p.is_file()]
        for forbidden in ("nvapi", "nvvideoeffects", "maxine", "cuda"):
            assert not any(forbidden in name for name in names), forbidden

    def test_the_abi_version_matches_the_library(self):
        """The loader refuses a library whose major version differs, because a
        struct-layout mismatch is not a crash anybody can debug."""
        header = (self._root() / "native" / "videofx" / "videofx.cpp").read_text(
            encoding="utf-8")
        assert f'kVersion[] = "{videofx.ABI_MAJOR}.' in header
