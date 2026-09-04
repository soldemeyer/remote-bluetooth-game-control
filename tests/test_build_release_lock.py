"""`_who_holds` has to find the executable PyInstaller actually produced.

The function exists to tell two failures apart: a scanner mid-pass, which
clears on its own, and a running copy of the app, which never will. It said
"no matching process is running" while an `rbgc-video.exe` from a test launch
thirty hours earlier held the very file the build was trying to delete.

The cause was a non-recursive glob. PyInstaller's onedir layout is
`dist/<app>/<app>.exe`, so `dist/*.exe` matched nothing and the candidate set
was always empty -- the branch that names a process was unreachable, and every
lock was reported as antivirus.

That is worse than no diagnosis: it names the one cause whose advice is "wait,
it will clear", for the one cause that will not.
"""

from __future__ import annotations

import sys

import pytest

from tools.build_release import _who_holds


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows lock path")
def test_it_looks_where_pyinstaller_puts_the_executable(tmp_path, monkeypatch) -> None:
    """A onedir bundle keeps its exe one level below `dist/`."""
    (tmp_path / "rbgc-video").mkdir()
    (tmp_path / "rbgc-video" / "rbgc-video.exe").write_bytes(b"")

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": '"rbgc-video.exe","65000","Console","1","10 K"\n'})(),
    )

    message = _who_holds(tmp_path)
    assert "rbgc-video.exe" in message, message
    assert "65000" in message, message
    assert "antivirus" not in message, (
        "a running process was found, so this must not be reported as a scanner"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows lock path")
def test_antivirus_is_only_blamed_when_nothing_matches(tmp_path, monkeypatch) -> None:
    """The fallback is still right when no copy of the app is running."""
    (tmp_path / "rbgc-video").mkdir()
    (tmp_path / "rbgc-video" / "rbgc-video.exe").write_bytes(b"")

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: type("R", (), {"stdout": '"explorer.exe","4242","Console","1","10 K"\n'})(),
    )

    assert "antivirus" in _who_holds(tmp_path)


def test_the_posix_branch_names_a_tool_that_can_answer(tmp_path, monkeypatch) -> None:
    """Off Windows there is no tasklist, so the message has to be actionable."""
    monkeypatch.setattr(sys, "platform", "linux")
    message = _who_holds(tmp_path)
    assert "lsof" in message
