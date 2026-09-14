"""Info icons: where the tip goes, and that every one of them says something.

The page used to carry a paragraph under every control, which is honest and is
also why the Server view was two screens tall -- a card stopped reading as a set
of controls. Moving the prose onto an icon trades that for two new ways to be
wrong, and both are silent:

  * a trigger with no text, which is an icon that does nothing when hovered;
  * a tip drawn off the bottom or the side of the window, which is text nobody
    can read and, worse, one that appears to be missing entirely.

`chooseTooltipPosition` is the only decision in the module and is pure, so it
is executed rather than described. The rest is DOM plumbing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
INDEX = STATIC / "index.html"
TOOLTIP = STATIC / "js" / "ui" / "tooltip.js"


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

BODY = """
    const mod = await import(BASE + '/js/ui/tooltip.js');
    const cases = JSON.parse(process.env.RBGC_CASES);
    console.log(JSON.stringify(cases.map(
      (c) => mod.chooseTooltipPosition(c.rect, c.size, c.viewport))));
"""


def place(cases: list[dict]) -> list[dict]:
    return json.loads(run_node(BODY, {"RBGC_CASES": json.dumps(cases)}))


def rect(left, top, width=18, height=18):
    return {
        "left": left, "top": top, "width": width, "height": height,
        "right": left + width, "bottom": top + height,
    }


VIEWPORT = {"width": 1280, "height": 800}
SIZE = {"width": 320, "height": 120}


@needs_node
class TestItGoesSomewhereReadable:
    def test_it_sits_below_the_trigger_when_there_is_room(self):
        (out,) = place([{"rect": rect(600, 300), "size": SIZE, "viewport": VIEWPORT}])
        assert out["flipped"] is False
        assert out["top"] > 300 + 18

    def test_it_flips_above_near_the_bottom(self):
        """A tip drawn below a trigger near the bottom edge is off screen, and
        an info icon that appears to do nothing is worse than the paragraph it
        replaced."""
        (out,) = place([{"rect": rect(600, 760), "size": SIZE, "viewport": VIEWPORT}])
        assert out["flipped"] is True
        assert out["top"] < 760

    def test_it_stays_below_when_there_is_room_for_neither(self):
        """A trigger in a viewport shorter than the tip. Flipping would put it
        off the *top* instead, which is no better -- below at least starts at
        something the reader can scroll to."""
        (out,) = place([{
            "rect": rect(600, 40),
            "size": {"width": 320, "height": 600},
            "viewport": {"width": 1280, "height": 300},
        }])
        assert out["flipped"] is False

    def test_it_is_centred_on_the_trigger_in_the_middle_of_the_page(self):
        (out,) = place([{"rect": rect(600, 300), "size": SIZE, "viewport": VIEWPORT}])
        assert out["left"] == 600 + 9 - 160

    def test_it_is_clamped_at_the_left_edge(self):
        (out,) = place([{"rect": rect(4, 300), "size": SIZE, "viewport": VIEWPORT}])
        assert out["left"] >= 0

    def test_it_is_clamped_at_the_right_edge(self):
        """Where the last adapter card's icons sit, which is exactly where a
        tip that cannot be clamped goes off screen."""
        (out,) = place([{"rect": rect(1270, 300), "size": SIZE, "viewport": VIEWPORT}])
        assert out["left"] + SIZE["width"] <= VIEWPORT["width"]

    def test_a_viewport_narrower_than_the_tip_still_starts_on_screen(self):
        (out,) = place([{
            "rect": rect(100, 300),
            "size": {"width": 400, "height": 120},
            "viewport": {"width": 320, "height": 800},
        }])
        assert out["left"] >= 0


# ---------------------------------------------------------------------------
# Every trigger has something to say
# ---------------------------------------------------------------------------


class TestEveryIconSaysSomething:
    def triggers(self) -> list[str]:
        html = INDEX.read_text(encoding="utf-8")
        return re.findall(r'<button class="info"[^>]*?data-info="([^"]*)"', html, re.S)

    def test_there_are_some(self):
        assert len(self.triggers()) >= 10, (
            "the prose was removed and not replaced by anything"
        )

    def test_none_is_empty(self):
        assert all(text.strip() for text in self.triggers())

    def test_every_info_button_carries_text(self):
        """An `.info` with no `data-info` is an icon that does nothing when
        hovered -- and the module refuses to show an empty tip, so there is no
        error to notice either."""
        html = INDEX.read_text(encoding="utf-8")
        buttons = re.findall(r'<button class="info"[^>]*>', html, re.S)
        assert buttons
        silent = [b for b in buttons if "data-info=" not in b]
        assert not silent, "info icons with nothing behind them:\n  " + "\n  ".join(silent)

    def test_every_info_button_is_named_for_a_reader(self):
        """The icon is a mask with no text of its own, so without a label it
        is announced as "button" and nothing more."""
        html = INDEX.read_text(encoding="utf-8")
        buttons = re.findall(r'<button class="info"[^>]*>', html, re.S)
        unnamed = [b for b in buttons if "aria-label=" not in b]
        assert not unnamed, "unnamed info icons:\n  " + "\n  ".join(unnamed)

    def test_they_are_buttons_not_spans(self):
        """Touch has no hover and a keyboard cannot hover at all. A real button
        is focusable and clickable; a span with a title is neither."""
        html = INDEX.read_text(encoding="utf-8")
        assert '<span class="info"' not in html


class TestTheTipIsReachableWithoutAPointer:
    def test_focus_opens_it(self):
        assert "focusin" in TOOLTIP.read_text(encoding="utf-8")

    def test_escape_closes_it(self):
        assert "'Escape'" in TOOLTIP.read_text(encoding="utf-8")

    def test_it_is_announced_rather_than_only_drawn(self):
        assert "aria-describedby" in TOOLTIP.read_text(encoding="utf-8")

    def test_a_touch_does_not_open_and_immediately_close_it(self):
        """A tap fires pointerover *and* click. Without telling them apart the
        icon flashes and does nothing -- on the one platform this affordance
        exists for."""
        assert "pointerType" in TOOLTIP.read_text(encoding="utf-8")

    def test_it_lives_at_body_level(self):
        """Not inside the card it describes. A `.card` carries a backdrop blur,
        which makes it a stacking context painted atomically in document order,
        so a popup nested in one is covered by the next card along."""
        html = INDEX.read_text(encoding="utf-8")
        after_app = html.split("</div>\n\n<!-- The one tooltip", 1)
        assert len(after_app) == 2, "the tip moved inside the app container"
        assert 'id="info-tip"' in after_app[1]
