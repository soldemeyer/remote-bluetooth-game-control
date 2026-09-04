"""The glass material: a blur is inert unless the fill above it is translucent.

This exists because of a bug that was invisible in review. `.card` was written
as *two* blocks -- one asking for `backdrop-filter`, one setting
`background: var(--panel)` -- and the second silently won the fill. An opaque
background leaves the filter nothing to sample, so every card outside the
Overview rendered flat while the CSS read as though it were glass.

Nothing about that is detectable from either block alone, which is why the
check here is on the *pair*: any rule that asks for a blur must also have a
translucent fill.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from common.design.themes import THEMES, theme_names
from common.design.tokens import palette

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
STYLE = STATIC / "style.css"
TOKENS = STATIC / "tokens.css"


def _rules(css: str) -> list[tuple[str, str]]:
    """Every `selector { body }` pair, flat. Good enough: this file nests only
    inside media queries, whose contents are themselves ordinary rules.

    Comments are stripped first. Without that a selector carries whatever
    comment precedes it, so an exact match against `.card` never fires -- and
    the test fails on a file that is correct, which is the worst kind.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return re.findall(r"([^{}]+)\{([^{}]*)\}", css)


def _declared(body: str, prop: str) -> str | None:
    found = None
    for line in body.split(";"):
        name, _, value = line.partition(":")
        if name.strip() == prop:
            found = value.strip()  # last wins, as in the cascade
    return found


#: Fills that let the backdrop through. A blur above any of these is doing
#: something; a blur above anything else is not.
TRANSLUCENT = ("--surface-panel", "--surface-glass", "--glass-scrim",
               "rgba(", "transparent", "hsla(")


def test_every_blur_has_a_translucent_fill() -> None:
    """A `backdrop-filter` under an opaque background renders nothing."""
    css = STYLE.read_text(encoding="utf-8")
    offenders = []
    for selector, body in _rules(css):
        if not _declared(body, "backdrop-filter"):
            continue
        fill = _declared(body, "background") or _declared(body, "background-color")
        if fill is None:
            # Inherited or set by another rule -- can't judge it here, and the
            # selectors that do this are covered by the explicit list below.
            continue
        if not any(token in fill for token in TRANSLUCENT):
            offenders.append(f"{selector.strip()} -> background: {fill}")
    assert not offenders, (
        "these rules ask for a backdrop blur under an opaque fill, so the blur "
        "renders nothing:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("selector", ["header", ".card", ".stat", ".empty", ".summary"])
def test_the_frosted_surfaces_are_frosted(selector: str) -> None:
    """The surfaces the operator sees as glass actually carry both halves.

    Named individually rather than counted, because the failure this guards is
    one surface quietly losing the property while the others keep it -- which
    is exactly what happened to everything outside the Overview.
    """
    css = STYLE.read_text(encoding="utf-8")
    blurred = False
    translucent = False
    for sel, body in _rules(css):
        if selector not in [s.strip() for s in sel.split(",")]:
            continue
        if _declared(body, "backdrop-filter"):
            blurred = True
        fill = _declared(body, "background") or _declared(body, "background-color")
        if fill and any(token in fill for token in TRANSLUCENT):
            translucent = True
    assert blurred, f"{selector} has no backdrop-filter"
    assert translucent, f"{selector} has no translucent fill, so its blur is inert"


def test_surface_panel_is_translucent_in_every_theme() -> None:
    """The web card fill must let the backdrop through."""
    assert palette["surface-panel"].a < 1.0
    for name in theme_names():
        assert THEMES[name]["surface-panel"].a < 1.0, name


def test_surface_solid_stays_opaque_in_every_theme() -> None:
    """Qt popups are separate top-level windows with nothing behind them.

    A translucent `surface-solid` shows the desktop through a dropdown, so the
    two tokens must not be conflated however similar they look.
    """
    assert palette["surface-solid"].a == 1.0
    for name in theme_names():
        assert THEMES[name]["surface-solid"].a == 1.0, name
        assert THEMES[name]["surface-solid-raised"].a == 1.0, name


def test_tokens_css_carries_surface_panel_for_every_theme() -> None:
    """The generated stylesheet is what the browser actually reads."""
    css = TOKENS.read_text(encoding="utf-8")
    # One for `:root` plus one per non-default theme block.
    assert css.count("--surface-panel:") == len(theme_names())


# ---------------------------------------------------------------------------
# The Overview's Bluetooth card read from the wrong list
# ---------------------------------------------------------------------------

OVERVIEW = STATIC / "js" / "sections" / "overview.js"


def test_overview_reads_adapter_state_from_hardware() -> None:
    """`status.adapters` is the router's channels, not the adapter state.

    Those entries carry neither `enabled` nor `phase` (see `Channel.snapshot`
    in `server/router.py`), so the old `adapters.filter(a => a.enabled)` matched
    nothing and the card reported "0/0 - none enabled" on a server with four
    adapters linked to a console. It could never have reported anything else,
    which is worse than a stale number: the display was confidently wrong and
    disagreed with the Bluetooth section about the same hardware.
    """
    src = OVERVIEW.read_text(encoding="utf-8")
    assert "status.hardware" in src, (
        "the Overview must read adapter state from `status.hardware`, the same "
        "list the Bluetooth section counts"
    )
    assert "adapters.filter((a) => a.enabled)" not in src, (
        "`status.adapters` entries have no `enabled` field, so this filter is "
        "always empty"
    )


def test_channel_snapshot_still_lacks_the_fields_the_overview_needs() -> None:
    """Pins *why* the fix is shaped the way it is.

    If `Channel.snapshot` ever grows `enabled`/`phase`, reading `hardware`
    stops being necessary and this test should be revisited deliberately --
    rather than someone "simplifying" the Overview back to the broken form
    because the two lists look interchangeable.
    """
    router = (Path(__file__).resolve().parent.parent / "server" / "router.py")
    body = router.read_text(encoding="utf-8").split("def snapshot", 1)[1]
    body = body.split("class Router", 1)[0]
    assert '"bd_addr"' in body, "not the snapshot body -- the split anchors moved"
    assert '"enabled"' not in body
    assert '"phase"' not in body


# ---------------------------------------------------------------------------
# backdrop-filter creates a stacking context, and that trapped the theme menu
# ---------------------------------------------------------------------------


def test_a_blurred_container_of_a_popup_is_raised() -> None:
    """The header holds the theme menu, and a blur changed how it paints.

    `backdrop-filter` makes an element a stacking context, and a
    *non-positioned* one paints atomically at z-index 0 in document order. So
    giving the header a blur put every later `.card` -- also blurred, also a
    stacking context -- on top of it, and the menu's own `z-index: 20` could no
    longer help: that z-index became scoped inside the header.

    The symptom is not visual only. Measured with `elementFromPoint`, the
    bottom menu item returned `.card`, so the click landed on the card.

    Anything that gains a blur and contains an overflowing popup needs the same
    pair, which is why this asserts the pair rather than the property.
    """
    css = STYLE.read_text(encoding="utf-8")
    header = [body for sel, body in _rules(css)
              if "header" in [s.strip() for s in sel.split(",")]]
    assert header, "no bare `header` rule found"
    blurred = [b for b in header if _declared(b, "backdrop-filter")]
    if not blurred:
        return  # no blur, no stacking context, nothing to raise
    positioned = any(_declared(b, "position") in {"relative", "sticky", "fixed"}
                     for b in header)
    layered = any((_declared(b, "z-index") or "auto") != "auto" for b in header)
    assert positioned and layered, (
        "the header has a backdrop-filter, which makes it a stacking context "
        "painted in document order -- so it needs `position` and a `z-index` "
        "or the cards below it paint over the theme menu"
    )


def test_the_menu_sits_above_its_own_header_content() -> None:
    """Within the header's context the menu still has to win."""
    css = STYLE.read_text(encoding="utf-8")
    menu = [body for sel, body in _rules(css)
            if ".menu" in [s.strip() for s in sel.split(",")]]
    assert menu, "no `.menu` rule found"
    z = next((_declared(b, "z-index") for b in menu if _declared(b, "z-index")), None)
    assert z is not None and int(z) > 0
