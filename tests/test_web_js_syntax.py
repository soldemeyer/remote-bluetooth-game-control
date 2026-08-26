"""The shipped JavaScript must actually parse.

This exists because a single broken string literal in app.js took the whole web
GUI down -- not the one feature being edited, the *entire* file, because a
syntax error stops the parser dead. The operator could not get past the login
screen, and the only clue was a console message naming a line number.

Every other test in this suite asserts on app.js as **text**, and none of them
can catch that: a file full of syntax errors still contains all the right
substrings. `test_web_disconnect.py` was passing happily while the GUI was
unusable.

Why there is no hand-rolled fallback
------------------------------------
A quote-balance heuristic was tried and removed. Real JavaScript defeats it
immediately -- template literals span lines and hold unbalanced apostrophes
(``machine's``), and regex literals contain bare quotes (``/[&<>"']/g``) --
so it reported valid code as broken. A check that fails on correct code is
worse than no check: it trains people to ignore it. Writing a JS lexer to do
better is not worth it when a real parser is one dependency away.

So this defers to `node --check` and skips where node is absent, which is
honest about what it can and cannot promise.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))


def test_there_is_javascript_to_check():
    """Guard against the glob silently matching nothing after a move."""
    assert SCRIPTS, "no .js under server/web/static -- has it moved?"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_it_parses(script: Path):
    result = subprocess.run(
        ["node", "--check", str(script)],
        capture_output=True,
        text=True,
        check=False,
        # Never let a helper inherit our stdin; see _run() in server/bt/adapter.py
        # for the hang this habit came from.
        input="",
    )
    assert result.returncode == 0, (
        f"{script.name} does not parse, so the whole file is dead in the "
        f"browser:\n{result.stderr.strip()}"
    )


#: Everything the browser actually loads from us.
ASSETS = SCRIPTS + sorted(STATIC.glob("*.html"))


@pytest.mark.parametrize("asset", ASSETS, ids=lambda p: p.name)
def test_no_inline_style_attributes(asset: Path):
    """The CSP sets ``style-src 'self'``, which blocks inline style attributes.

    Including ones written into a JavaScript template literal, which is how
    this got shipped: a ``style="float:right"`` on the Unassign button. The
    browser blocks the style, so the button renders in the wrong place, and
    logs a violation *per render* -- the adapter cards re-render on every
    status push, so an operator watching the console sees the count climb
    without bound and reasonably reads it as a fault in whatever they just did.

    Nothing fails. The page works, mostly, slightly wrong, forever.
    """
    text = asset.read_text(encoding="utf-8")
    assert 'style="' not in text, (
        f"{asset.name} carries an inline style attribute, which the "
        f"Content-Security-Policy blocks -- put it in style.css instead"
    )
    assert "setAttribute('style'" not in text
    assert 'setAttribute("style"' not in text
