"""The shared design tokens, and the generated stylesheet that mirrors them.

The product has three front ends and one palette. Before this existed the
palette was five colour literals duplicated across `client/gui/app.py`,
`client/gui/latency_plot.py` and `server/web/static/style.css`, agreeing with
each other by luck. The point of these tests is that they cannot drift apart
again silently: the CSS is generated from the Python source, and a stale
commit fails here rather than showing up as a browser that is a slightly
different blue from the desktop app.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from common.design import icons
from common.design.tokens import Color, Motion, Radius, Space, Type, palette, resolved_glass
from tools.build_design_tokens import OUTPUT, stylesheet


class TestTheGeneratedStylesheetIsNotStale:
    def test_it_matches_the_token_source(self):
        """Regenerate and compare -- the same guard the presets and art have."""
        assert OUTPUT.exists(), "run: python -m tools.build_design_tokens"
        committed = OUTPUT.read_text(encoding="utf-8")
        assert committed == stylesheet(), (
            "server/web/static/tokens.css is out of date; "
            "run `python -m tools.build_design_tokens`"
        )

    def test_it_is_committed_with_unix_line_endings(self):
        """A Windows checkout must not commit CRLF into a file Linux reads.

        The release builder documents this trap costing a whole build: `sed`
        capturing a trailing `\\r` ended up inside a generated C header.
        """
        assert b"\r\n" not in OUTPUT.read_bytes()

    def test_every_colour_reaches_the_web(self):
        text = stylesheet()
        for name in palette:
            assert f"--{name}:" in text, f"{name} is missing from tokens.css"

    def test_the_scales_reach_the_web(self):
        text = stylesheet()
        for value in Space.ALL:
            assert f"--space-{value}:" in text
        for name in ("radius-control", "radius-card", "radius-panel", "radius-dialog"):
            assert f"--{name}:" in text
        for name in ("motion-fast", "motion-normal", "motion-slow", "motion-ease"):
            assert f"--{name}:" in text

    def test_braces_balance(self):
        text = stylesheet()
        assert text.count("{") == text.count("}")


class TestColourRendersCorrectlyForBothToolkits:
    """CSS wants a 0..1 float alpha; Qt's stylesheet wants a percentage.

    Handing Qt a float is not an error -- it is read as an almost completely
    transparent colour, so the panel renders as though the style never
    applied. That is exactly the kind of silent difference this module exists
    to prevent, so both forms are pinned.
    """

    def test_opaque_colours_are_hex_in_both(self):
        colour = Color.hex("4C8DFF")
        assert colour.css == "#4C8DFF"
        assert colour.qss == "#4C8DFF"

    def test_css_alpha_is_a_float(self):
        assert Color.hex("FFFFFF", 0.05).css == "rgba(255, 255, 255, 0.05)"

    def test_qt_alpha_is_a_percentage(self):
        assert Color.hex("FFFFFF", 0.05).qss == "rgba(255, 255, 255, 5%)"

    def test_hex_parsing_rejects_nonsense(self):
        with pytest.raises(ValueError):
            Color.hex("#abc")

    def test_alpha_returns_the_same_hue(self):
        base = palette["accent-primary"]
        faded = base.alpha(0.2)
        assert (faded.r, faded.g, faded.b) == (base.r, base.g, base.b)
        assert faded.a == 0.2


class TestGlassCanBeFlattened:
    """Qt cannot blur what is behind a widget, so glass there is a composite."""

    def test_it_resolves_to_an_opaque_colour(self):
        flat = resolved_glass()
        assert flat.a == 1.0
        assert flat.css.startswith("#")

    def test_it_sits_between_the_backdrop_and_the_overlay(self):
        base = palette["background-base"]
        flat = resolved_glass()
        # 5% white over a dark base must be lighter than the base, and nowhere
        # near white.
        assert flat.r > base.r and flat.g > base.g and flat.b > base.b
        assert flat.r < 60

    def test_an_opaque_colour_composites_to_itself(self):
        solid = palette["surface-solid"]
        assert solid.over(palette["background-base"]) == solid


class TestTheIconFamilyIsCoherent:
    def test_every_icon_is_valid_svg(self):
        """A malformed path makes QSvgRenderer report only `isValid() == False`.

        CLAUDE.md records that costing time three separate times on the
        controller artwork, which is why those are parsed in a test too.
        """
        for name in icons.icon_names():
            ET.fromstring(icons.render_svg(name, color="#FFFFFF"))

    def test_the_sprite_parses(self):
        ET.fromstring(icons.sprite())

    def test_every_icon_has_geometry(self):
        for name, path in icons.ICONS.items():
            assert path.strip(), f"{name} has an empty path"
            assert path.lstrip().startswith("M"), f"{name} does not start with a move"

    def test_they_share_one_grid_and_one_weight(self):
        for name in icons.icon_names():
            document = icons.render_svg(name)
            assert f'viewBox="0 0 {icons.VIEWBOX} {icons.VIEWBOX}"' in document
            assert f'stroke-width="{icons.STROKE_WIDTH}"' in document, name

    def test_an_unknown_icon_names_the_alternatives(self):
        with pytest.raises(KeyError, match="unknown icon"):
            icons.render_svg("definitely-not-an-icon")

    def test_qt_can_be_given_a_real_colour(self):
        """`currentColor` has nothing to inherit from in a standalone document."""
        assert 'stroke="#4C8DFF"' in icons.render_svg("play", color="#4C8DFF")

    @pytest.mark.parametrize(
        "name",
        [
            # The brief names these actions specifically; each must exist
            # before a screen tries to use one and silently falls back to text.
            "link", "link-off", "play", "stop", "video", "volume", "mic",
            "gamepad", "bluetooth", "settings", "fullscreen", "activity",
            "wifi", "server", "monitor", "copy", "refresh", "edit", "trash",
        ],
    )
    def test_the_actions_the_product_needs_are_present(self, name):
        assert name in icons.ICONS


class TestTheScalesAreUsableAsScales:
    def test_spacing_increases(self):
        assert list(Space.ALL) == sorted(Space.ALL)
        assert len(set(Space.ALL)) == len(Space.ALL)

    def test_radii_increase_with_the_size_of_the_surface(self):
        assert Radius.CONTROL < Radius.CARD < Radius.PANEL < Radius.DIALOG

    def test_type_scale_decreases(self):
        steps = [Type.DISPLAY, Type.TITLE, Type.HEADING, Type.BODY, Type.LABEL, Type.META]
        assert steps == sorted(steps, reverse=True)

    def test_motion_stays_short(self):
        """A control that animates longer than ~150 ms feels laggy, not smooth."""
        assert Motion.INSTANT < Motion.FAST <= 150
        assert Motion.SLOW <= 250


class TestTheQtIndicatorGlyphs:
    """`qtui/assets/*.svg` is generated and committed, like the controller art.

    Regenerate with `python -m tools.build_design_tokens` after changing the
    palette or the icon set.
    """

    def test_they_are_not_stale(self):
        from tools.build_design_tokens import QT_ASSETS, qt_indicator_assets

        for name, expected in qt_indicator_assets().items():
            actual = (QT_ASSETS / name).read_text(encoding="utf-8")
            assert actual == expected, (
                f"{name} is stale; run python -m tools.build_design_tokens"
            )

    def test_each_glyph_takes_the_colour_of_what_it_sits_on(self):
        """Colour is baked in, so it has to match the surface underneath.

        The tick and the dot are drawn on the accent fill; the chevrons sit on
        an ordinary control surface. Getting this wrong is invisible in code
        review and shows up as a glyph that vanishes into its background.
        """
        from tools.build_design_tokens import qt_indicator_assets

        expected = {
            "check.svg": "text-on-accent",
            "radio.svg": "text-on-accent",
            "chevron-down.svg": "text-secondary",
            "chevron-up.svg": "text-secondary",
        }
        documents = qt_indicator_assets()
        assert set(documents) == set(expected), "a glyph was added without a colour"
        for name, token in expected.items():
            assert palette[token].css in documents[name]

    def test_the_tick_is_the_icon_set_s_own_checkmark(self):
        """One drawing, so the tick in a checkbox matches the one in a toast."""
        from tools.build_design_tokens import qt_indicator_assets

        assert icons.ICONS["check"] in qt_indicator_assets()["check.svg"]
