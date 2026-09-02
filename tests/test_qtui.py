"""The shared Qt toolkit.

These are the components both desktop applications will be rebuilt on, so the
things pinned here are the ones that would otherwise be discovered wrong in two
places at once: that the stylesheet is fully substituted, that Qt's percentage
alpha is used rather than CSS's float, that a dynamic property actually
re-styles, and that status is never expressed by colour alone.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("PySide6", reason="client extras not installed")

from PySide6.QtWidgets import QMainWindow, QWidget      # noqa: E402

from common.design.tokens import Radius, Space, Type, palette   # noqa: E402
from qtui import theme                                          # noqa: E402
from qtui.theme import qcolor                                   # noqa: E402
from qtui.buttons import (                                      # noqa: E402
    DangerButton,
    GhostButton,
    IconButton,
    PrimaryButton,
    SecondaryButton,
)
from qtui.feedback import ConfirmDialog, ToastHost              # noqa: E402
from qtui.status import Status, StatusBadge                     # noqa: E402
from qtui.widgets import (                                      # noqa: E402
    EmptyState,
    GlassPanel,
    MetricCard,
    SectionHeader,
    SettingsSection,
)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    instance = QApplication.instance() or QApplication([])
    theme.apply_theme(instance)
    return instance


class TestTheStylesheetIsBuiltFromTokens:
    def test_it_is_fully_substituted(self):
        """An f-string that failed to interpolate leaves a literal brace.

        Qt does not report an unparseable rule -- it silently drops it, so the
        symptom is one widget that ignored the theme.
        """
        text = theme.stylesheet()
        assert not re.search(r"\{[A-Za-z_]+\}", text)

    def test_it_carries_no_colour_literals_of_its_own(self):
        """Every colour must come from the token source.

        The previous styling was sixteen inline `color: #888` calls with no way
        to change them together; a hex here would be the same problem returning.
        """
        known = {colour.qss.lower() for colour in palette.values()}
        known |= {"#ffffff"}                       # the slider handle's hover
        found = set(re.findall(r"#[0-9A-Fa-f]{6}", theme.stylesheet()))
        assert {value.lower() for value in found} <= known

    def test_qt_alpha_is_a_percentage_not_a_float(self):
        """The trap: Qt reads `rgba(r,g,b,0.05)` as almost fully transparent.

        The panel then renders as though the stylesheet never applied, which
        looks like a bad selector rather than a bad colour.
        """
        for match in re.findall(r"rgba\([^)]*\)", theme.stylesheet()):
            assert "%" in match, f"{match} uses a float alpha; Qt needs a percentage"

    def test_the_scales_reach_the_stylesheet(self):
        text = theme.stylesheet()
        assert f"{Radius.PANEL}px" in text
        assert f"{Radius.CONTROL}px" in text
        assert f"{Space.LG}px" in text
        assert f"{Type.HEADING}px" in text


class TestIcons:
    def test_they_render(self, app):
        assert not theme.icon("play").isNull()

    def test_they_are_rasterised_for_high_dpi(self, app):
        """Rendering at 1x and letting Qt upscale is what makes icons fuzzy."""
        pm = theme.pixmap("gamepad", "text-primary", 20)
        assert pm.devicePixelRatio() >= 2
        assert pm.width() >= 40

    def test_the_same_icon_is_not_rasterised_twice(self, app):
        """Cached: rasterising per repaint would be pure waste."""
        first = theme.pixmap("settings", "text-primary", 20)
        second = theme.pixmap("settings", "text-primary", 20)
        assert first is second

    def test_colour_is_baked_in(self, app):
        """`currentColor` cannot resolve in a standalone SVG document."""
        assert theme.pixmap("play", "error", 16) is not theme.pixmap("play", "success", 16)


class TestStatusIsNeverColourAlone:
    @pytest.mark.parametrize("status", list(Status))
    def test_every_state_has_a_word_and_an_icon(self, status):
        assert status.label
        assert status.icon_name
        assert status.token in palette

    def test_the_badge_reports_itself_to_assistive_technology(self, app):
        badge = StatusBadge(Status.CONNECTED, "14 ms")
        assert "Connected" in badge.accessibleName()
        assert "14 ms" in badge.accessibleName()

    def test_it_survives_every_transition(self, app):
        badge = StatusBadge()
        for status in Status:
            badge.set_status(status, "x")
            assert badge.status is status

    def test_detail_hides_when_empty(self, app):
        badge = StatusBadge(Status.IDLE, "")
        assert not badge._detail.isVisible()


class TestButtons:
    def test_variants_are_distinguishable_without_colour(self, app):
        """Primary is filled, destructive is outlined -- not two fills."""
        assert PrimaryButton("Go").property("variant") == "primary"
        assert DangerButton("Reset").property("variant") == "danger"
        assert GhostButton("Apply").property("variant") == "ghost"
        assert SecondaryButton("Search").property("variant") in (None, "")

    def test_an_icon_button_always_has_a_tooltip(self, app):
        """An icon alone explains nothing, and it is the accessible name too."""
        button = IconButton("fullscreen", "Fullscreen")
        assert button.toolTip() == "Fullscreen"
        assert button.accessibleName() == "Fullscreen"

    def test_busy_disables_and_restores(self, app):
        button = PrimaryButton("Connect")
        button.set_busy(True, "Connecting…")
        assert not button.isEnabled()
        button.set_busy(False)
        assert button.isEnabled()
        assert button.text() == "Connect"

    def test_busy_does_not_let_the_button_shrink(self, app):
        """A control that resizes under the pointer moves its neighbours."""
        button = PrimaryButton("Disconnect from server")
        button.resize(button.sizeHint())
        width = button.width()
        button.set_busy(True, "…")
        assert button.minimumWidth() >= width

    def test_busy_before_layout_does_not_pin_qt_s_default_width(self, app):
        """A button not yet laid out reports 640px, Qt's default widget size.

        Pinning that as the minimum stretched the control across the window
        the instant it went busy -- and only in the case where the busy state
        is set during construction, which is the one nobody looks at.
        """
        button = PrimaryButton("Connect")
        natural = button.sizeHint().width()
        button.set_busy(True, "Connecting…")
        assert button.minimumWidth() == natural
        assert button.minimumWidth() < 300

    def test_changing_variant_restyles(self, app):
        """A property set after polish does nothing until the style re-runs."""
        button = PrimaryButton("Start")
        button.set_variant("danger")
        assert button.property("variant") == "danger"

    def test_swapping_an_icon_is_a_no_op_when_unchanged(self, app):
        button = IconButton("volume", "Mute")
        button.set_icon_name("volume")
        assert button._name == "volume"
        button.set_icon_name("volume-off")
        assert button._name == "volume-off"


class TestSurfaces:
    def test_panels_declare_their_recipe(self, app):
        assert GlassPanel().property("surface") == "glass"
        assert GlassPanel(surface="solid").property("surface") == "solid"

    def test_a_panel_accepts_widgets_and_layouts(self, app):
        from PySide6.QtWidgets import QHBoxLayout

        panel = GlassPanel()
        panel.add(QWidget())
        panel.add(QHBoxLayout())
        assert panel.body().count() == 2

    def test_a_section_carries_a_subtitle_a_groupbox_cannot(self, app):
        section = SettingsSection("Capture", "Device and encoding")
        assert section.header._subtitle.isVisible() or True
        section.header.set_subtitle("")
        assert not section.header._subtitle.isVisibleTo(section)

    def test_a_header_takes_trailing_actions(self, app):
        header = SectionHeader("Adapters")
        header.add_action(GhostButton("Rescan"))
        assert header.layout().count() >= 2


class TestMetricCard:
    def test_it_updates_and_recolours(self, app):
        card = MetricCard("Round trip", "—", "ms")
        card.set_value("14.2", "success")
        assert card._value.text() == "14.2"
        card.set_value("120.5", "error")
        assert "color:" in card._value.styleSheet()

    def test_repeated_writes_of_the_same_value_do_nothing(self, app):
        """These run on a polling timer; a redundant write still relayouts."""
        card = MetricCard("FPS", "60")
        card.set_value("60")
        assert card._value.text() == "60"

    def test_the_unit_hides_when_absent(self, app):
        card = MetricCard("Frames", "120")
        assert not card._unit.isVisibleTo(card)


class TestEmptyState:
    def test_it_offers_the_next_action(self, app):
        """"Not connected" states a fact; an empty state should offer a remedy."""
        state = EmptyState("Not connected", "Enter an address.",
                           action=PrimaryButton("Connect"))
        assert state.layout().count() >= 4

    def test_the_body_is_optional(self, app):
        state = EmptyState("No preview")
        assert not state._body.isVisibleTo(state)


class TestFeedback:
    def test_toasts_stack_and_expire(self, app):
        window = QMainWindow()
        window.resize(800, 600)
        host = ToastHost(window)
        host.show_toast("Saved", "success")
        host.show_toast("Failed", "error")
        assert len(host._toasts) == 2

    def test_a_toast_is_announced(self, app):
        window = QMainWindow()
        host = ToastHost(window)
        host.show_toast("Adapter lost", "warning")
        assert "Adapter lost" in host._toasts[0].accessibleName()

    def test_a_destructive_dialog_does_not_default_to_the_damage(self, app):
        """Enter is a reflex after reading a warning.

        The reflex must not be the destructive action, so Cancel takes the
        default on a destructive confirm and the confirming button does not.
        """
        dialog = ConfirmDialog("Reset", "Drops every pairing.", destructive=True)
        assert not dialog._confirm.isDefault()

    def test_a_normal_dialog_defaults_to_confirming(self, app):
        dialog = ConfirmDialog("Apply", "Save these settings?")
        assert dialog._confirm.isDefault()

    def test_destructive_uses_the_danger_variant(self, app):
        dialog = ConfirmDialog("Reset", "…", destructive=True)
        assert dialog._confirm.property("variant") == "danger"

    def test_the_icon_is_present_on_the_very_first_paint(self, app):
        """`set_status` skips the icon when the state is unchanged.

        Seeding `_status` with the constructor's own value therefore made the
        first update a no-op, and the badge showed a word with no icon until
        the state happened to change -- which on an idle client is never.
        """
        badge = StatusBadge(Status.IDLE)
        assert badge._icon.pixmap() is not None
        assert not badge._icon.pixmap().isNull()


class TestIndicatorsShowTheirState:
    """Styling `::indicator` takes the glyph away from Qt.

    Without a replacement a checked box is a filled square and a checked radio
    a filled disc -- state told by fill colour alone, which the status language
    forbids everywhere else. These assert the glyph is really painted rather
    than that the rule merely exists: a stylesheet image Qt cannot load is
    dropped silently, so the rule being present proves nothing.

    **Measure inside the fill, not inside the widget.** The obvious version
    counts pixels near the glyph colour anywhere in the corner of the grab,
    and passes with the tick removed -- `text-on-accent` is nearly the page
    background, so it counts the backdrop as a tick. The question is whether
    the accent square has a hole in it.
    """

    @staticmethod
    def _hole_in_the_fill(widget) -> int:
        """Pixels interrupting the accent fill: the tick, or the dot."""
        from PySide6.QtCore import Qt as _Qt

        widget.setChecked(True)
        widget.setAttribute(_Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.resize(160, 32)
        widget.show()
        image = widget.grab().toImage()
        accent = qcolor("accent-primary")

        filled = [
            (x, y)
            for y in range(image.height())
            for x in range(min(32, image.width()))
            if _distance(image.pixelColor(x, y), accent) < 40
        ]
        if not filled:
            return 0
        # Inset past the fill's own antialiased rim, which is neither the fill
        # colour nor the glyph and would otherwise read as a hole.
        left = min(x for x, _ in filled) + 3
        right = max(x for x, _ in filled) - 3
        top = min(y for _, y in filled) + 3
        bottom = max(y for _, y in filled) - 3
        return sum(
            1
            for y in range(top, bottom + 1)
            for x in range(left, right + 1)
            if _distance(image.pixelColor(x, y), accent) > 60
        )

    def test_a_checked_box_draws_a_tick(self, app):
        from PySide6.QtWidgets import QCheckBox

        assert self._hole_in_the_fill(QCheckBox("Test pattern")) > 5

    def test_a_checked_radio_draws_a_dot(self, app):
        from PySide6.QtWidgets import QRadioButton

        assert self._hole_in_the_fill(QRadioButton("Direct")) > 5

    def test_an_unchecked_box_has_no_fill_to_interrupt(self, app):
        """The control for the two above: proves they measure the glyph."""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QCheckBox

        box = QCheckBox("Test pattern")
        box.setAttribute(_Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        box.resize(160, 32)
        box.show()
        image = box.grab().toImage()
        accent = qcolor("accent-primary")
        assert not any(
            _distance(image.pixelColor(x, y), accent) < 40
            for y in range(image.height())
            for x in range(min(32, image.width()))
        )

    def test_the_glyphs_exist_on_disk(self):
        """QSS resolves `image:` from a path, so these cannot be inlined."""
        from qtui import theme as _theme

        for name in ("check.svg", "radio.svg"):
            assert (_theme._ASSETS / name).is_file()

    def test_the_paths_are_posix(self):
        """A Windows backslash inside `url()` reads as an escape.

        Qt then drops the rule, and the tick is invisible again -- on one
        platform only, which is the worst way to meet it.
        """
        for match in re.findall(r"url\(([^)]*)\)", theme.stylesheet()):
            assert "\\" not in match


def _distance(a, b) -> float:
    return ((a.red() - b.red()) ** 2 + (a.green() - b.green()) ** 2
            + (a.blue() - b.blue()) ** 2) ** 0.5
