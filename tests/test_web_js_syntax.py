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

import re
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


def test_every_input_type_the_page_uses_is_styled():
    """An input type missing from the stylesheet renders browser-default.

    `number` was, so the port fields were white boxes -- invisible against the
    old flat panel and glaring against a coloured backdrop. A new input type
    added to the markup should fail here rather than in a screenshot.
    """
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    used = set(re.findall(r'<input[^>]*type="([a-z]+)"', html))
    # Checkboxes and radios are drawn by their own rules, not the field rule.
    used -= {"checkbox", "radio", "submit", "hidden"}
    styled = set(re.findall(r'input\[type="([a-z]+)"\]', css))
    assert used <= styled, f"unstyled input types: {sorted(used - styled)}"


def test_grid_containers_cancel_the_adjacent_card_margin():
    """`.card + .card` must not fire inside a container that has its own gap.

    In a grid it applies to every item except the first, so Controller 1 sat
    18px above Controllers 2, 3 and 4 while appearing taller. Measured in a
    browser: tops [733, 751, 751] before, [733, 733, 733] after.

    Structural rather than behavioural, because the behavioural check needs a
    rendering engine and the suite has none. The numbers above came from
    QtWebEngine and are recorded here so the next person does not have to
    rediscover what "aligned" meant.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    reset = re.search(r"((?:\.\w+ \.card \+ \.card,?\s*)+)\{\s*margin-top: 0;", css)
    assert reset, "no rule cancelling the adjacent-card margin"
    for container in (".cards", ".split", ".stack"):
        assert f"{container} .card + .card" in reset.group(1), container


def test_every_module_the_entry_imports_is_public():
    """`app.js` is an ES module, so its import graph is fetched before login.

    A module missing from `PUBLIC_PATHS` is refused, and the *whole* script
    fails to run -- the sign-in form stops working entirely, not just the
    section that module serves. That is a worse failure than the unstyled
    login screen a missing stylesheet causes, and it fails the same way:
    silently, at the fail-closed allow-list.
    """
    graph = set()
    for path in [STATIC / "app.js", *(STATIC / "js").rglob("*.js")]:
        for spec in re.findall(r"from\s+'([^']+\.js)'", path.read_text(encoding="utf-8")):
            resolved = (path.parent / spec).resolve()
            graph.add("/" + resolved.relative_to(STATIC.resolve()).as_posix())

    source = (STATIC.parent / "app.py").read_text(encoding="utf-8")
    block = re.search(r"PUBLIC_PATHS = frozenset\((.*?)\n\)", source, re.S)
    assert block
    public = set(re.findall(r'"(/[^"]*)"', block.group(1)))
    assert graph <= public, f"imported but not public: {sorted(graph - public)}"


def test_no_module_imports_the_entry_point():
    """A section importing `app.js` would be a cycle through the entry.

    The entry owns the socket and the delegated handlers; a module that
    imported it would be half-initialised at load, and the failure shows up as
    a handler that silently never fires.
    """
    for path in (STATIC / "js").rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "app.js'" not in text, f"{path.name} imports the entry point"
