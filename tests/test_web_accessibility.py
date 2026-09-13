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
    assert sections, "no views at all -- has the markup moved?"
    unnamed = [s for s in sections if "aria-label" not in s and "aria-labelledby" not in s]
    assert not unnamed, "views with no accessible name:\n  " + "\n  ".join(unnamed)


def test_the_rail_and_the_views_agree() -> None:
    """Every tab must lead somewhere, and every view must be reachable.

    This replaces a count of the sections, which was a magic number that went
    stale the moment six tabs became four. The invariant underneath it is the
    one that actually matters, and it is the blank-page bug: `showView` finds
    its section by `data-view`, so a rail item naming one that does not exist
    is a tab that appears to do nothing, and a section no rail item names is a
    view nobody can reach.
    """
    html = INDEX.read_text(encoding="utf-8")
    rail = html.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
    tabs = set(re.findall(r'class="rail-item" data-view="([^"]+)"', rail))
    views = set(re.findall(r'<section class="view" data-view="([^"]+)"', html))

    assert tabs, "the rail has no items"
    assert tabs == views, (
        f"tabs with no view: {sorted(tabs - views)}; "
        f"views with no tab: {sorted(views - tabs)}"
    )


def test_a_retired_view_name_still_lands_somewhere() -> None:
    """The last view is remembered in localStorage, across an upgrade.

    Six tabs became four, so every existing install holds a name that no
    longer exists. `showView` used to return silently on a miss, which leaves
    *no* section active -- a blank page with a working rail and nothing to say
    why, on the first load after deploying, for everybody. Each retired name
    needs an alias, and the alias has to name a view that is really there.
    """
    nav = NAV.read_text(encoding="utf-8")
    html = INDEX.read_text(encoding="utf-8")
    views = set(re.findall(r'<section class="view" data-view="([^"]+)"', html))

    block = re.search(r"VIEW_ALIASES = \{(.*?)\}", nav, re.S)
    assert block, "no alias map; a stored view name from before the rename is fatal"
    aliases = dict(re.findall(r"(\w+)\s*:\s*'([^']+)'", block.group(1)))

    for retired in ("overview", "adapters", "clients"):
        assert retired in aliases, f"{retired} was a tab and has no alias"
    for old, new in aliases.items():
        assert new in views, f"{old} is aliased to {new}, which is not a view"


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
