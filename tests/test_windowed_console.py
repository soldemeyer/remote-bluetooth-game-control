"""Both apps ship windowed, and still have somewhere to print.

A windowed Windows build (`console=False`) is what stops a black console
window sitting behind the GUI for the whole session -- which is what somebody
double-clicking the video server saw. The cost is that the process starts with
`sys.stdout` and `sys.stderr` set to `None`, so `--help`, `--headless` and
`--list-devices` would print into the void or raise on `None.write`.

`common.console.attach_console_if_needed` is what buys the second half back,
and the **order of its three cases is the thing worth pinning**: there were two
copies of this and they had drifted. The video server's opened `CONOUT$`
directly, which works from a terminal and silently discards redirection --
`rbgc-video --headless > log.txt` wrote to the console and left an empty file.

The Windows-specific paths cannot run here, so what is tested is the contract
that holds on every platform, the structure of the decision, and that both
entry points and both specs agree with it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from common.console import NullWriter, attach_console_if_needed

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"


class TestItIsANoOpWhereThereIsNothingToFix:
    def test_a_normal_run_is_untouched(self):
        """Both streams already exist, so it must not replace them -- a test
        runner's captured stdout being swapped for a sink would be an
        interesting afternoon."""
        before = (sys.stdout, sys.stderr)

        attach_console_if_needed()

        assert (sys.stdout, sys.stderr) == before

    @pytest.mark.skipif(sys.platform == "win32", reason="this is the Windows path")
    def test_it_does_nothing_off_windows(self, monkeypatch):
        """Even with the streams missing: there is no windowed build to repair
        on Linux, and `ctypes.windll` does not exist to try."""
        monkeypatch.setattr(sys, "stdout", None)
        monkeypatch.setattr(sys, "stderr", None)

        attach_console_if_needed()

        assert sys.stdout is None


class TestTheSinkKeepsPrintAlive:
    """Case three: launched from Explorer with no redirection, there is nowhere
    to write at all -- and the alternative to a sink is every `print` raising.
    """

    def test_writing_is_allowed_and_discarded(self):
        assert NullWriter().write("anything") == 0

    def test_it_is_not_a_terminal(self):
        """Anything asking is deciding whether to colour its output."""
        assert NullWriter().isatty() is False

    def test_flush_is_a_no_op(self):
        assert NullWriter().flush() is None

    def test_asking_for_a_descriptor_fails_honestly(self):
        """It has none. Returning a plausible number would send somebody
        else's write to a descriptor this object does not own."""
        with pytest.raises(OSError):
            NullWriter().fileno()

    def test_print_works_against_it(self):
        print("hello", file=NullWriter())


class TestTheOrderOfTheThreeCases:
    """Binding the descriptors comes **first**, and that is the half the video
    server's copy was missing.

    Searched over the function's *code*, with its docstring dropped: this
    module's own prose explains the ordering and names `CONOUT$` as the thing
    that went wrong, so a plain grep matches the explanation and passes
    whatever the code does. This repo has been bitten by that twice.
    """

    def code(self) -> str:
        from common import console

        source = Path(console.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "attach_console_if_needed"
        )
        body = function.body
        if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant
        ):
            body = body[1:]          # the docstring
        return chr(10).join(ast.unparse(node) for node in body)

    def test_the_descriptors_are_tried_before_the_console(self):
        code = self.code()

        assert code.index("_bind(1)") < code.index("AttachConsole"), (
            "attaching to a console before binding fds sends redirected output "
            "to the terminal instead of the file somebody asked for"
        )

    def test_the_console_is_only_a_fallback(self):
        """Attached only when neither descriptor was usable. Attaching
        unconditionally is harmless on its own and puts the output in the wrong
        place when there was a pipe."""
        code = self.code()

        assert code.index("stdout is None and stderr is None") < code.index(
            "AttachConsole"
        )

    def test_conout_is_not_used(self):
        """The old video-server copy opened `CONOUT$`, which is a console
        handle: it cannot represent a pipe or a file, so redirection was lost
        with no error anywhere."""
        from common import console

        source = Path(console.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        code = chr(10).join(
            ast.unparse(node) for node in tree.body
            if not (isinstance(node, ast.Expr)
                    and isinstance(getattr(node, "value", None), ast.Constant))
        )

        assert "CONOUT$" not in code


class TestBothAppsUseTheOneCopy:
    """There were two, and they had already drifted."""

    @pytest.mark.parametrize("module", ["client/main.py", "videoserver/main.py"])
    def test_the_entry_point_calls_it(self, module):
        source = (ROOT / module).read_text(encoding="utf-8")

        assert "from common.console import attach_console_if_needed" in source
        assert "attach_console_if_needed()" in source

    @pytest.mark.parametrize("module", ["client/main.py", "videoserver/main.py"])
    def test_it_runs_before_the_parser(self, module):
        """argparse writes `--help` and usage errors to stderr, so a windowed
        build has to have one before `parse_args` is reached."""
        source = (ROOT / module).read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        calls = [
            node.func.id if isinstance(node.func, ast.Name) else
            getattr(node.func, "attr", "")
            for node in ast.walk(main) if isinstance(node, ast.Call)
        ]

        assert "attach_console_if_needed" in calls
        assert calls.index("attach_console_if_needed") < calls.index("parse_args")

    @pytest.mark.parametrize("module", ["client/main.py", "videoserver/main.py"])
    def test_neither_keeps_its_own(self, module):
        source = (ROOT / module).read_text(encoding="utf-8")

        assert "def _attach_console_if_needed" not in source
        assert "class _NullWriter" not in source


class TestTheBundlesAreWindowed:
    """The reason any of this exists."""

    @pytest.mark.parametrize("spec", ["client.spec", "videoserver.spec"])
    def test_no_console_window(self, spec):
        source = (PACKAGING / spec).read_text(encoding="utf-8")
        setting = [
            line for line in source.splitlines()
            if line.strip().startswith("console=")
        ]

        assert setting, f"{spec} does not set console at all"
        assert all("console=False" in line for line in setting), (
            f"{spec} would open a console window behind the GUI"
        )


class TestTheEmbeddedChildHasNoConsoleEither:
    """Embedded mode spawns the video server, and on Windows a console
    subsystem child gets a console whether or not anything is in it."""

    def test_the_flag_is_passed(self):
        source = (ROOT / "server" / "videohost.py").read_text(encoding="utf-8")

        assert "CREATE_NO_WINDOW" in source
        assert "creationflags=creationflags" in source

    def test_it_is_looked_up_rather_than_named(self):
        """`subprocess.CREATE_NO_WINDOW` does not exist off Windows, which is
        where this server normally runs -- naming it directly would be an
        AttributeError at import on the reference Pi."""
        source = (ROOT / "server" / "videohost.py").read_text(encoding="utf-8")

        assert 'getattr(subprocess, "CREATE_NO_WINDOW", 0)' in source

    def test_every_stream_is_piped(self):
        """Which is why the child has nothing to show a console for: its output
        is re-logged by the parent."""
        source = (ROOT / "server" / "videohost.py").read_text(encoding="utf-8")
        spawn = source.split("create_subprocess_exec", 1)[1].split(")", 1)[0]

        for stream in ("stdin", "stdout", "stderr"):
            assert f"{stream}=asyncio.subprocess.PIPE" in spawn
