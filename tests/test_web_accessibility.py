"""Accessibility properties of the server web GUI.

Each of these was a real gap found in the Stage 8 audit, and each is the kind
that is invisible to anyone not using the thing it serves: the page looks and
behaves identically with them missing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
INDEX = STATIC / "index.html"
STYLE = STATIC / "style.css"
NAV = STATIC / "js" / "nav.js"


def test_the_banner_is_a_live_region() -> None:
    """The banner is the only place the page reports whether an action worked.

    Everything else on screen is state; this is the one element that says
    "saved" or "failed". Without a live region a screen-reader user presses a
    button and is told nothing at all -- not even that something happened.
    """
    html = INDEX.read_text(encoding="utf-8")
    banner = re.search(r"<div id=\"banner\"[^>]*>", html)
    assert banner, "no #banner element"
    tag = banner.group(0)
    assert 'aria-live="polite"' in tag, tag
    assert 'role="status"' in tag, tag
    assert 'aria-atomic="true"' in tag, tag


def test_every_view_is_a_named_landmark() -> None:
    """A `<section>` with no accessible name is not a landmark worth having.

    Each view is a top-level region of the page, so it needs a name for a
    reader listing landmarks to navigate by.
    """
    html = INDEX.read_text(encoding="utf-8")
    sections = re.findall(r"<section class=\"view\"[^>]*>", html)
    assert len(sections) >= 6, f"expected the six views, found {len(sections)}"
    unnamed = [s for s in sections if "aria-label" not in s and "aria-labelledby" not in s]
    assert not unnamed, "views with no accessible name:\n  " + "\n  ".join(unnamed)


def test_reduced_motion_covers_more_than_the_spinner() -> None:
    """The setting is a request about all motion.

    It used to disable one keyframe animation while six `transition` rules --
    rail items, buttons, toggles, cards -- carried on animating for someone who
    had asked the operating system for none.
    """
    css = STYLE.read_text(encoding="utf-8")
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)
    assert block, "no prefers-reduced-motion block"
    body = block.group(1)
    assert "transition-duration" in body, (
        "the block does not neutralise transitions, only animations")
    assert "*" in body, "the block is not applied broadly"


@pytest.mark.parametrize("key", ["ArrowDown", "ArrowUp", "Home", "End"])
def test_the_theme_menu_implements_the_keys_its_role_promises(key: str) -> None:
    """`role="menu"` is a promise, and an unkept one costs the user Tab.

    A reader switches to menu navigation on seeing the role and stops passing
    Tab through, so declaring it without arrow handling leaves a menu that can
    be opened and not moved around in -- strictly worse than no role.
    """
    assert key in NAV.read_text(encoding="utf-8"), f"{key} is not handled"


def test_escape_returns_focus_to_the_button() -> None:
    """Closing with the keyboard must not strand focus on nothing."""
    src = NAV.read_text(encoding="utf-8")
    assert "restoreFocus" in src
    assert "button.focus()" in src


def test_focus_rings_use_focus_visible() -> None:
    """`:focus` left a ring on the switch after every mouse click.

    `:focus-visible` still matches a text field clicked with a mouse, because
    keyboard input is expected there -- so nothing is lost by using it
    throughout, and the stuck ring goes away.
    """
    css = STYLE.read_text(encoding="utf-8")
    assert "input:focus-visible" in css
    assert re.search(r"(?<!-)\binput:focus\s*,", css) is None, (
        "a bare `input:focus` rule is back")
