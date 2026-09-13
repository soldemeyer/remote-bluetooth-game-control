"""Run a piece of the web GUI's JavaScript under Node, with a stub DOM.

Several of the decisions in `server/web/static/` are pure functions wearing a
browser's clothes: which adapter sorts first, where a tooltip goes when there
is no room below it, whether a cleared field stays cleared. Asserting on the
*source text* of those is the trap this repo already records -- a grep-the-
source test once pinned a bug as a requirement, because it could not tell an
intention from a behaviour.

So they are executed instead. Node rather than a browser: it takes a second,
needs no display, and the decisions being checked do not touch layout.

The stub DOM is permissive on purpose. It exists so that modules touching the
document at import time can be loaded at all; anything missing from it is a gap
here rather than a fault in the module, and a test that needs more should build
what it needs in its own body.

Extracted from `test_web_operator_fixes.py`, which had the only copy. A second
copy is how two harnesses drift into disagreeing about what "a document" is.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "server" / "web" / "static"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; this check is advisory and skips cleanly",
)

# nav.js, dom.js and the ui/ modules all touch the document at module scope.
# These are stub gaps, not faults -- the modules under test do not use any of
# it unless a test says otherwise.
STUBS = """
globalThis.addEventListener = () => {};
const noop = () => {};
const element = {
  removeAttribute: noop, setAttribute: noop, addEventListener: noop,
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  querySelector: () => null, querySelectorAll: () => [],
  dataset: {}, style: {},
};
globalThis.document = {
  documentElement: element, body: element,
  getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], addEventListener: noop,
  createElement: () => element,
};
globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
globalThis.matchMedia = () => ({ matches: false, addEventListener: noop });
globalThis.window = globalThis;
const BASE = 'file://' + process.env.RBGC_STATIC.split('\\\\').join('/');
"""


def run_node(body: str, env: dict | None = None) -> str:
    """Run `body` as an ES module and return its last line of output."""
    source = STUBS + textwrap.dedent(body).strip()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        capture_output=True, text=True, timeout=60,
        # **UTF-8 explicitly.** `text=True` decodes with the locale codec,
        # which on Windows is cp1252 -- so any non-ASCII the page actually
        # shows comes back mangled and a test comparing it fails against
        # correct code. The GUI is full of en dashes and ellipses.
        encoding="utf-8", errors="replace",
        env={**os.environ, "RBGC_STATIC": str(STATIC), **(env or {})},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]
