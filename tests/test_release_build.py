"""The release build driver, guarded the way the packaging specs are.

Nothing here runs PyInstaller or Nuitka -- both need many minutes and one of
them needs a Linux host. What it pins is the set of facts that produce a
*plausible-looking* release which fails on somebody else's machine:

  * an archive whose AppImage arrives without the executable bit, which fails
    with a bare "permission denied" and reads as a corrupt download;
  * a shell script written with CRLF endings, which bash rejects with "bad
    interpreter" naming a file that plainly exists;
  * a version that drifts because it is written down twice;
  * a checksum file that covers only what the last run happened to build.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

from tools import build_release

BASE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def script() -> str:
    return (BASE / "packaging" / "linux" / "build-appimage.sh").read_text(
        encoding="utf-8"
    )


def commands(shell_script: str) -> str:
    """Just the lines that execute, with comments and blanks removed.

    A generated script carries the comments explaining *why* it does what it
    does -- including, in one case, a comment naming the exact broken call it
    replaced. Asserting against the raw text therefore matches the explanation
    rather than the behaviour, which is how the first version of these tests
    managed to fail on a correct script.
    """
    return "\n".join(
        line for line in shell_script.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


class TestTheAppsAreRealTargets:
    """Every field names something that exists, or the failure is at build time."""

    def test_there_is_a_client_and_a_video_server(self):
        assert {app.key for app in build_release.APPS} == {"client", "video"}

    def test_every_pyinstaller_spec_exists(self):
        for app in build_release.APPS:
            assert (BASE / "packaging" / app.spec).is_file(), app.key

    def test_every_appimage_target_is_one_the_script_accepts(self):
        script = (BASE / "packaging" / "linux" / "build-appimage.sh").read_text(
            encoding="utf-8"
        )
        for app in build_release.APPS:
            assert f'"$TARGET" = {app.appimage_target}' in script, app.key

    def test_the_binary_names_match_what_the_specs_produce(self):
        for app in build_release.APPS:
            spec = (BASE / "packaging" / app.spec).read_text(encoding="utf-8")
            assert f'name="{app.binary}"' in spec, app.key

    def test_the_config_filenames_match_the_apps(self):
        """The README tells the user where settings live; a wrong path is worse
        than none, because it sends them deleting a file that is not there."""
        from client.config import CONFIG_FILENAME as CLIENT_CONFIG
        from videoserver.config import CONFIG_FILENAME as VIDEO_CONFIG

        by_key = {app.key: app for app in build_release.APPS}
        assert by_key["client"].config_file == CLIENT_CONFIG
        assert by_key["video"].config_file == VIDEO_CONFIG


class TestTheVersionIsWrittenDownOnce:
    def test_it_is_read_from_pyproject(self):
        text = (BASE / "pyproject.toml").read_text(encoding="utf-8")
        expected = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE).group(1)

        assert build_release.repo_version() == expected

    def test_the_appimage_script_does_not_hardcode_one(self):
        """It did, and a second copy of a version drifts silently -- the only
        place it shows up is file properties nobody reads."""
        script = (BASE / "packaging" / "linux" / "build-appimage.sh").read_text(
            encoding="utf-8"
        )
        assert "--file-version=$" in script or '--file-version="$VERSION"' in script
        assert "--file-version=0." not in script

    def test_reading_it_strips_carriage_returns(self):
        """A repo checked out on Windows and built through WSL has CRLF
        endings, so the capture keeps a trailing \\r -- and Nuitka writes the
        value straight into a C header:

            #define NUITKA_FILE_VERSION "0.1.0
            "

        The build then dies with "missing terminating \\" character" in
        generated code, naming nothing that appears in any file you wrote.
        Measured: this is exactly how the first AppImage build failed.
        """
        script = (BASE / "packaging" / "linux" / "build-appimage.sh").read_text(
            encoding="utf-8"
        )
        version_line = next(
            line for line in script.splitlines() if line.startswith("VERSION=$(sed")
        )
        block = script[script.index(version_line):]
        assert "tr -d '\\r'" in block.split("VERSION=${VERSION")[0]

    def test_the_version_pattern_stops_at_the_closing_quote(self):
        """`.*` is greedy and would swallow a trailing comment on the line."""
        script = (BASE / "packaging" / "linux" / "build-appimage.sh").read_text(
            encoding="utf-8"
        )
        assert '\\([^"]*\\)' in script


class TestTheArchiveIsUsableOnTheOtherEnd:
    def _zip(self, tmp_path: Path, name: str, *, executable: bool) -> zipfile.ZipInfo:
        source = tmp_path / name
        source.write_bytes(b"binary")
        target = tmp_path / "out.zip"
        with zipfile.ZipFile(target, "w") as archive:
            build_release._zip_add(archive, source, name, executable=executable)
        with zipfile.ZipFile(target) as archive:
            return archive.getinfo(name)

    def test_an_executable_entry_carries_the_executable_bit(self, tmp_path):
        """Whatever the source filesystem reported is not to be trusted: a file
        copied out of WSL onto a DrvFs mount routinely loses +x."""
        info = self._zip(tmp_path, "RBGC-Client-x86_64.AppImage", executable=True)

        assert (info.external_attr >> 16) & 0o777 == 0o755

    def test_an_ordinary_file_does_not(self, tmp_path):
        info = self._zip(tmp_path, "README.txt", executable=False)

        assert (info.external_attr >> 16) & 0o777 == 0o644

    def test_entries_are_marked_as_regular_files(self, tmp_path):
        """Without the file-type bits an extractor can treat the mode as 0."""
        info = self._zip(tmp_path, "thing.AppImage", executable=True)

        assert (info.external_attr >> 16) & 0o170000 == 0o100000


class TestTheReadmeAnswersTheQuestionsAUserActuallyHas:
    @pytest.mark.parametrize("app", build_release.APPS, ids=lambda a: a.key)
    def test_the_linux_one_says_to_chmod(self, app):
        """A zip does not reliably carry +x across platforms, so the AppImage
        arrives non-executable often enough that this has to be written down."""
        text = build_release.readme_for(app, "1.2.3", "linux", "x86_64")

        assert "chmod +x" in text

    @pytest.mark.parametrize("app", build_release.APPS, ids=lambda a: a.key)
    def test_the_linux_one_warns_about_fuse(self, app):
        """An AppImage on a FUSE-3-only host dies with a bare dlopen error that
        reads as a bad download."""
        text = build_release.readme_for(app, "1.2.3", "linux", "x86_64")

        assert "libfuse" in text

    @pytest.mark.parametrize("app", build_release.APPS, ids=lambda a: a.key)
    def test_the_windows_one_warns_about_smartscreen(self, app):
        """The build is unsigned, so every recipient meets this and a fair
        number read it as malware."""
        text = build_release.readme_for(app, "1.2.3", "windows", "x86_64")

        assert "SmartScreen" in text

    @pytest.mark.parametrize("app", build_release.APPS, ids=lambda a: a.key)
    def test_it_names_the_version_and_the_config_location(self, app):
        windows = build_release.readme_for(app, "1.2.3", "windows", "x86_64")
        linux = build_release.readme_for(app, "1.2.3", "linux", "x86_64")

        assert "1.2.3" in windows and "1.2.3" in linux
        assert app.config_file in windows
        assert app.config_file in linux

    def test_the_video_readme_keeps_the_two_passwords_apart(self):
        """Sharing them would hand a denied player the one credential that is
        exempt from viewing tickets."""
        video = next(a for a in build_release.APPS if a.key == "video")
        text = build_release.readme_for(video, "1.2.3", "windows", "x86_64")

        assert "NOT" in text and "password" in text


class TestChecksums:
    def test_it_covers_every_archive_present(self, tmp_path):
        for name in ("a.zip", "b.zip"):
            (tmp_path / name).write_bytes(name.encode())
        (tmp_path / "ignored.txt").write_text("not an archive")

        target = build_release.write_checksums(tmp_path)
        lines = target.read_text(encoding="utf-8").strip().splitlines()

        assert len(lines) == 2
        assert all(len(line.split()[0]) == 64 for line in lines)
        assert "ignored.txt" not in target.read_text(encoding="utf-8")

    def test_it_is_rebuilt_rather_than_appended(self, tmp_path):
        """--collect adds another machine's artifacts afterwards, and the file
        has to describe the whole set rather than half of it."""
        (tmp_path / "a.zip").write_bytes(b"a")
        build_release.write_checksums(tmp_path)
        (tmp_path / "b.zip").write_bytes(b"b")

        target = build_release.write_checksums(tmp_path)
        lines = target.read_text(encoding="utf-8").strip().splitlines()

        assert len(lines) == 2

    def test_nothing_to_checksum_is_not_an_error(self, tmp_path):
        assert build_release.write_checksums(tmp_path) is None

    def test_it_is_written_with_lf_endings(self, tmp_path):
        """`Path.write_text` on Windows translates to CRLF, and `sha256sum -c`
        then fails on every line with "No such file or directory" naming a file
        that plainly exists -- the \\r is part of the name it looked for.

        This file is read on Linux almost by definition, so a Windows build
        producing a CRLF one ships a checksum nobody can use.
        """
        (tmp_path / "a.zip").write_bytes(b"a")

        target = build_release.write_checksums(tmp_path)

        assert b"\r" not in target.read_bytes()


class TestLineEndingsFollowTheTargetPlatform:
    def _readme(self, tmp_path: Path, kind: str) -> bytes:
        app = build_release.APPS[0]
        target = tmp_path / "out.zip"
        with zipfile.ZipFile(target, "w") as archive:
            build_release._zip_text(
                archive, "README.txt",
                build_release.readme_for(app, "1.0", kind, "x86_64"),
                crlf=(kind == "windows"),
            )
        with zipfile.ZipFile(target) as archive:
            return archive.read("README.txt")

    def test_the_windows_readme_is_crlf(self, tmp_path):
        """A LF-only file opens as one unbroken line in several Windows
        viewers that are still in everyday use."""
        assert b"\r\n" in self._readme(tmp_path, "windows")

    def test_the_linux_readme_is_lf(self, tmp_path):
        assert b"\r" not in self._readme(tmp_path, "linux")


class TestTheGeneratedShellScriptsRunUnderBash:
    def test_they_are_written_with_lf_endings(self, tmp_path):
        """CRLF makes bash read the shebang as /bin/bash\\r and fail with 'bad
        interpreter', naming a file that plainly exists."""
        target = tmp_path / "x.sh"
        build_release._write_sh(target, "#!/usr/bin/env bash\necho hello\n")

        assert b"\r\n" not in target.read_bytes()


class TestTheSetupVerificationDoesNotInventFailures:
    """It reported failure after provisioning had entirely succeeded.

    `nuitka` defines no `__version__`, so importing it and reading that raised
    AttributeError -- under `set -e`, at the very end, after every package was
    installed. The operator is told the setup failed and has no way to tell
    that from a setup that genuinely did.
    """

    def test_it_asks_nuitka_for_its_version_the_supported_way(self):
        """Asserted against the generated script, not this module's source.

        The first version of this test grepped the source and failed on the
        *comment* explaining the fix -- which is the exact weakness CLAUDE.md
        records about grep-the-source tests. The script text is the thing that
        actually runs.
        """
        script = commands(build_release.setup_script("/mnt/f/repo"))

        assert "-m nuitka --version" in script
        assert "nuitka.__version__" not in script

    def test_it_checks_the_dynamic_import_blind_spots(self):
        """Each is loaded in a way static analysis cannot see, so a missing one
        builds cleanly and dies at first use -- twenty minutes later."""
        script = build_release.setup_script("/mnt/f/repo")

        for package in ("av", "nacl", "sdl2", "PySide6"):
            assert package in script, package

    def test_it_does_not_ask_sudo_to_preserve_the_environment(self):
        """`sudo -E` is refused by many sudoers configurations, which warns on
        every run; provision.sh sets what it needs itself."""
        script = commands(build_release.setup_script("/mnt/f/repo"))

        assert "sudo -E" not in script
        assert "sudo bash packaging/linux/provision.sh" in script


class TestLibSDL2IsActuallyBundled:
    """`--include-package-data=sdl2dll` does not bring `libSDL2-2.0.so`.

    Measured on a finished AppImage: it collected sdl2dll's *dependencies*
    (libopus, libwebp, libogg, ...) and left out the one library that matters,
    because Nuitka classifies it as a DLL rather than package data and expects
    an importer to pull it in. Nothing does -- PySDL2 dlopens it through ctypes
    at runtime, which no static analysis can see.

    The bundle compiled cleanly, started, printed sdl2dll's own banner, and then
    failed `import sdl2` -- reported to the player as "PySDL2 is not installed",
    on a build that ships it.
    """

    def test_the_library_is_included_explicitly(self, script):
        body = commands(script)

        assert "--include-data-files=" in body
        assert "libSDL2*.so*=sdl2dll/dll/" in body

    def test_the_source_directory_is_resolved_from_the_package(self, script):
        """A hardcoded site-packages path is wrong on every other machine."""
        body = commands(script)

        assert "import os, sdl2dll" in body

    def test_a_missing_library_fails_before_the_compile(self, script):
        """Twenty minutes earlier than discovering it in the finished bundle."""
        body = commands(script)

        assert 'if [ ! -f "${sdl_dlls}/libSDL2-2.0.so" ]; then' in body


class TestTheBackendSaysWhySdlIsMissing:
    def test_it_reports_the_actual_import_error(self):
        """'Not installed' is wrong in a packaged build, and points the player
        at `pip install` for a program with no Python to install into."""
        from client.input import sdl2_backend

        # Real environment either way: the contract is that the reason is
        # available, not that it is empty or non-empty here.
        detail = sdl2_backend.import_error()
        assert isinstance(detail, str)
        assert (detail == "") is sdl2_backend.is_available()

    def test_the_error_message_carries_it(self, monkeypatch):
        from client.input import InputBackendError, create_backend
        from client.input import sdl2_backend

        monkeypatch.setattr(sdl2_backend, "is_available", lambda: False)
        monkeypatch.setattr(
            sdl2_backend, "import_error",
            lambda: "libSDL2-2.0.so: cannot open shared object file",
        )

        with pytest.raises(InputBackendError) as caught:
            create_backend("sdl2")

        assert "libSDL2-2.0.so" in str(caught.value)


class TestClearingAPreviousBuild:
    """Antivirus holding a freshly written .exe is routine, not exceptional.

    PyInstaller cleans its own output directory and gives up on the first
    refusal -- `PermissionError: [WinError 5]` on a file no process is running.
    Measured twice: five seconds of retries was enough on an idle machine and
    not enough when a test run was sharing the CPU, failing a twenty-minute
    build at the very start of it.
    """

    def test_it_waits_far_longer_than_a_scan_takes(self):
        import inspect

        signature = inspect.signature(build_release._clear_output)
        assert signature.parameters["timeout_s"].default >= 60

    def test_the_handler_matches_this_python(self):
        """`onexc` replaced `onerror` in 3.12; the project supports 3.11."""
        import sys

        expected = "onexc" if sys.version_info >= (3, 12) else "onerror"
        assert build_release._RMTREE_HANDLER == expected

    def test_a_locked_tree_is_eventually_reported_not_hung(self, tmp_path, monkeypatch):
        folder = tmp_path / "dist-app"
        folder.mkdir()
        (folder / "app.exe").write_bytes(b"x")

        monkeypatch.setattr(
            build_release.shutil, "rmtree",
            lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")),
        )

        with pytest.raises(build_release.BuildError) as caught:
            build_release._clear_output(folder, timeout_s=0.1)

        assert "locked" in str(caught.value) or "cannot clear" in str(caught.value)

    def test_a_clear_tree_returns_immediately(self, tmp_path):
        folder = tmp_path / "gone"
        build_release._clear_output(folder, timeout_s=0.1)   # must not raise

    def test_the_failure_names_the_process_when_there_is_one(self, tmp_path):
        """"Something is holding a file open" is true and useless.

        The two causes need opposite responses: a scanner clears on its own, a
        running copy of the app never does. Measured -- a build waited the full
        90 s on an orphaned `rbgc-client.exe` left by an earlier test, and the
        message sent the operator looking at antivirus.
        """
        folder = tmp_path / "dist-app"
        folder.mkdir()
        (folder / "rbgc-client.exe").write_bytes(b"x")

        text = build_release._who_holds(folder)

        # Whichever branch this machine takes, it must be actionable.
        assert "antivirus" in text or "Still running" in text
        assert text.strip()

    def test_it_distinguishes_the_two_causes(self, tmp_path, monkeypatch):
        folder = tmp_path / "dist-app"
        folder.mkdir()
        (folder / "rbgc-client.exe").write_bytes(b"x")
        monkeypatch.setattr(build_release.sys, "platform", "win32")

        class _Done:
            stdout = '"rbgc-client.exe","12424","Console","1","50,000 K"\n'

        monkeypatch.setattr(
            build_release.subprocess, "run", lambda *a, **k: _Done()
        )
        held = build_release._who_holds(folder)
        assert "12424" in held and "taskkill" in held

        class _Empty:
            stdout = '"explorer.exe","900","Console","1","50,000 K"\n'

        monkeypatch.setattr(
            build_release.subprocess, "run", lambda *a, **k: _Empty()
        )
        free = build_release._who_holds(folder)
        assert "antivirus" in free


class TestNuitkaOutputPaths:
    """Nuitka names its output directory after the *entry script*.

    `client/main.py` produces `main.dist` containing a binary called
    `rbgc-client` -- `--output-filename` renames the binary, not the directory.
    The script assumed otherwise and failed with `cp: cannot stat
    '.../rbgc-client.dist/.'` *after* a full compile, every time.

    It survived in a committed script because nothing in CI runs Nuitka: it
    needs a Linux host and many minutes. This is the cheap half of that.
    """

    def test_the_dist_path_is_derived_from_the_entry_script(self, script):
        body = commands(script)

        assert "entry_base=$(basename \"$entry\" .py)" in body
        assert '${outdir}/${entry_base}.dist' in body

    def test_each_app_gets_its_own_output_directory(self, script):
        """Both entry points are `main.py`, so a shared --output-dir would have
        the second build silently overwrite the first."""
        body = commands(script)

        assert '--output-dir="$outdir"' in body
        assert '--output-dir="$WORK"' not in body

    def test_a_missing_dist_is_reported_rather_than_cp_failing(self, script):
        body = commands(script)

        assert 'if [ ! -d "$dist" ]; then' in body

    def test_the_binary_inside_it_is_checked(self, script):
        """A dist directory with no executable is a build that half-worked."""
        body = commands(script)

        assert 'if [ ! -x "${dist}/${name}" ]; then' in body

    def test_the_work_directory_is_cleaned_not_just_the_dist(self, script):
        body = commands(script)

        assert 'rm -rf "$outdir" "$appdir"' in body


class TestTheGeneratedBuildScript:
    def test_it_builds_the_targets_it_is_given(self):
        script = build_release.build_script("/mnt/f/repo", "client video")

        assert "for target in client video" in script

    def test_it_refuses_early_when_the_toolchain_is_missing(self):
        """Better than discovering it part-way through a long compile."""
        script = build_release.build_script("/mnt/f/repo", "client")

        assert "appimagetool missing" in script
        assert "nuitka missing" in script

    def test_it_works_off_the_drvfs_mount_and_copies_back(self):
        script = build_release.build_script("/mnt/f/repo", "client")

        assert 'export RBGC_WORK_DIR="$HOME' in script
        assert 'export RBGC_OUT_DIR="$HOME' in script
        assert "cp -f" in script and "dist/linux/" in script

    def test_it_prefers_the_build_venv(self):
        script = build_release.build_script("/mnt/f/repo", "client")

        assert "/bin/python3" in script and "export PATH=" in script


class TestTheAppImageScriptCanBeRedirected:
    """The Linux build must be able to happen off the DrvFs mount.

    Not a preference under WSL: Nuitka's many small writes are an order of
    magnitude slower there, and appimagetool cannot reliably set the executable
    bit on what it produces on that filesystem.
    """

    def test_the_output_directory_is_overridable(self, script):
        assert "${RBGC_OUT_DIR:-" in script

    def test_the_work_directory_is_overridable(self, script):
        assert "${RBGC_WORK_DIR:-" in script

    def test_the_defaults_are_unchanged(self, script):
        """A plain Linux host must build exactly where it always did."""
        assert "${BASE}/dist/linux" in script
        assert "${BASE}/build/linux" in script

    def test_the_driver_points_them_at_the_wsl_filesystem(self):
        source = Path(build_release.__file__).read_text(encoding="utf-8")

        assert "RBGC_WORK_DIR" in source and "RBGC_OUT_DIR" in source
        assert "$HOME" in build_release.WSL_WORK


class TestWslFailuresAreDiagnosed:
    """All three present identically at the call site -- the build does not
    happen -- and the fix for each is completely different."""

    def test_a_missing_wsl_is_named_as_such(self, monkeypatch):
        monkeypatch.setattr(build_release.shutil, "which", lambda _: None)

        ok, detail = build_release.wsl_probe()

        assert ok is False
        assert "wsl --install" in detail

    def test_a_broken_service_suggests_an_update(self, monkeypatch):
        """Seen on this machine: WSL 2.2.4.0 against a much newer Windows.
        The service starts, fails instantly, and --shutdown does not help."""
        monkeypatch.setattr(build_release.shutil, "which", lambda _: "wsl.exe")

        class _Result:
            returncode = 1
            stdout = ""
            stderr = "Catastrophic failure\nError code: Wsl/Service/E_UNEXPECTED"

        monkeypatch.setattr(build_release, "_wsl", lambda *a, **k: _Result())

        ok, detail = build_release.wsl_probe()

        assert ok is False
        assert "wsl --update" in detail

    def test_a_working_wsl_reports_its_architecture(self, monkeypatch):
        monkeypatch.setattr(build_release.shutil, "which", lambda _: "wsl.exe")

        class _Result:
            returncode = 0
            stdout = "aarch64\n"
            stderr = ""

        monkeypatch.setattr(build_release, "_wsl", lambda *a, **k: _Result())

        ok, detail = build_release.wsl_probe()

        assert ok is True
        assert detail == "aarch64"


class TestArchitectureNaming:
    def test_the_two_platforms_spellings_agree(self, monkeypatch):
        """Windows says AMD64 and Linux says x86_64 for the same chip. An
        archive named for the host's spelling would ship two names for one
        thing."""
        for reported, expected in (
            ("AMD64", "x86_64"),
            ("x86_64", "x86_64"),
            ("ARM64", "aarch64"),
            ("aarch64", "aarch64"),
        ):
            monkeypatch.setattr(build_release.platform, "machine", lambda r=reported: r)
            assert build_release.host_arch() == expected
