"""Named colour themes.

A theme is **only** a set of overrides on top of the shared palette: the
backdrop, the orbs floating in it, and the accent. Everything structural --
glass alpha, border alpha, the status colours, the spacing and radius scales --
is deliberately *not* themeable. Those carry meaning (green is healthy, amber
is a warning) or carry the material, and letting a theme move them would mean
five designs to check rather than one design in five colourways.

Switching is a runtime operation: `set_theme` rewrites the shared `palette`
dict **in place**, so the many modules that did `from ...tokens import palette`
keep working against the object they already hold. Rebinding a new dict would
leave every one of them on the old colours, which is the kind of bug that shows
up as "half the window changed".
"""

from __future__ import annotations

from common.design.tokens import Color, palette

__all__ = ["THEMES", "active_theme", "set_theme", "theme_names"]

#: The default. Warm amber on near-black -- the pairing asked for, and the one
#: that stays out of the way of a video picture, which is what this window is
#: mostly showing.
DEFAULT_THEME = "amber"


def _theme(
    *,
    base: str,
    raised: str,
    sunken: str,
    grad: tuple[str, str, str],
    orbs: tuple[str, str, str, str],
    accent: str,
    accent_hover: str,
    gradient: tuple[str, str],
    on_accent: str,
    text: tuple[str, str, str],
    solid: str,
) -> dict[str, Color]:
    """One theme, written as hex so the tables below stay readable."""
    return {
        "background-base": Color.hex(base),
        "background-raised": Color.hex(raised),
        "background-sunken": Color.hex(sunken),
        "backdrop-1": Color.hex(grad[0]),
        "backdrop-2": Color.hex(grad[1]),
        "backdrop-3": Color.hex(grad[2]),
        "orb-magenta": Color.hex(orbs[0]),
        "orb-violet": Color.hex(orbs[1]),
        "orb-blue": Color.hex(orbs[2]),
        "orb-rose": Color.hex(orbs[3]),
        "accent-primary": Color.hex(accent),
        "accent-primary-hover": Color.hex(accent_hover),
        "accent-primary-muted": Color.hex(accent, 0.22),
        "accent-gradient-start": Color.hex(gradient[0]),
        "accent-gradient-end": Color.hex(gradient[1]),
        "accent-focus": Color.hex(gradient[0]),
        "border-active": Color.hex(accent, 0.55),
        "text-on-accent": Color.hex(on_accent),
        "info": Color.hex(accent),
        "info-muted": Color.hex(accent, 0.18),
        # Text is tinted to the theme too. Left neutral it stays whatever the
        # last theme's hue was -- the amber scheme shipped with lavender
        # column headers, which reads as a control that failed to update.
        "text-primary": Color.hex(text[0]),
        "text-secondary": Color.hex(text[1]),
        "text-muted": Color.hex(text[2]),
        # Popups and dense wells. These are *opaque* -- a popup is a separate
        # top-level window with nothing behind it to blend with -- so they have
        # to be tinted per theme or every dropdown keeps the previous scheme.
        "surface-solid": Color.hex(raised),
        "surface-solid-raised": Color.hex(solid),
        # The web's frosted card fill: the same tint, translucent, so the
        # backdrop-filter behind it has something to show. Per theme for the
        # same reason `surface-solid` is -- a card that kept the previous
        # scheme's hue reads as a control that failed to update.
        "surface-panel": Color.hex(raised, 0.72),
    }


#: The orb keys keep their original names across every theme even though the
#: hues no longer match them. Renaming them per theme would mean the backdrop
#: could not simply look up four fixed keys, and "orb-1..4" says less about
#: what the four are for -- they are a warm one, a mid one, a cool one and an
#: accent, in that order, everywhere.
THEMES: dict[str, dict[str, Color]] = {
    "amber": _theme(
        base="14100C", raised="1E1811", sunken="0B0907",
        grad=("120E0A", "2A1D10", "1A1410"),
        orbs=("F59E0B", "EA580C", "B45309", "FBBF24"),
        accent="F59E0B", accent_hover="FBBF24",
        gradient=("FB923C", "F59E0B"), on_accent="1A1206",
        text=("F5EFE6", "D6C9B6", "A2947F"),
        solid="2A2119",
    ),
    "violet": _theme(
        base="1A0B2E", raised="241040", sunken="120720",
        grad=("120720", "2E0F52", "3B1C8C"),
        orbs=("C026D3", "7C3AED", "2563EB", "EC4899"),
        accent="4C8DFF", accent_hover="6BA1FF",
        gradient=("A855F7", "4C8DFF"), on_accent="0B1220",
        text=("E6E9EF", "C9C6E0", "9A93B8"),
        solid="362356",
    ),
    "blue": _theme(
        base="081020", raised="0E1A30", sunken="050A14",
        grad=("050A14", "0F2547", "10315E"),
        orbs=("2563EB", "0EA5E9", "1D4ED8", "38BDF8"),
        accent="4C8DFF", accent_hover="6BA1FF",
        gradient=("38BDF8", "4C8DFF"), on_accent="04101F",
        text=("E6EDF7", "B8C7DE", "8496AF"),
        solid="17263F",
    ),
    "green": _theme(
        base="071410", raised="0C2119", sunken="040D0A",
        grad=("040D0A", "0D2A20", "10402F"),
        orbs=("10B981", "059669", "0D9488", "34D399"),
        accent="3ECF8E", accent_hover="6EE7B7",
        gradient=("34D399", "3ECF8E"), on_accent="052015",
        text=("E4F2EB", "B4D2C4", "83A395"),
        solid="133024",
    ),
    "grey": _theme(
        base="121418", raised="1B1E24", sunken="0A0B0E",
        grad=("0A0B0E", "1B1F26", "262B33"),
        orbs=("64748B", "475569", "334155", "94A3B8"),
        accent="94A3B8", accent_hover="CBD5E1",
        gradient=("CBD5E1", "94A3B8"), on_accent="10131A",
        text=("E8EAEE", "BFC5CF", "8B929E"),
        solid="272C34",
    ),
}

#: Shown in the picker. Kept beside the definitions so a new theme cannot be
#: added without a name someone can read.
LABELS = {
    "amber": "Amber (default)",
    "violet": "Violet",
    "blue": "Blue",
    "green": "Green",
    "grey": "Slate",
}

#: A snapshot of the palette as imported, before any theme is applied. Every
#: `set_theme` starts from this rather than from whatever the last theme left
#: behind -- otherwise a theme that overrides fewer keys than its predecessor
#: inherits the difference, and the fifth switch looks nothing like the first.
_ORIGINAL: dict[str, Color] = dict(palette)

_active = DEFAULT_THEME


def theme_names() -> list[str]:
    return list(THEMES)


def active_theme() -> str:
    return _active


def set_theme(name: str) -> str:
    """Apply a theme to the shared palette, in place. Returns the name used.

    An unknown name falls back to the default rather than raising: a config
    file naming a theme that a later version removed should cost the user a
    colour scheme, not their client.
    """
    global _active
    if name not in THEMES:
        name = DEFAULT_THEME
    palette.clear()
    palette.update(_ORIGINAL)
    palette.update(THEMES[name])
    _active = name
    return name
