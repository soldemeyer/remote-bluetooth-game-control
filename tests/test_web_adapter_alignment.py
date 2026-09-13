"""The buttons on the controller cards, and why they would not stay still.

Reported twice. First as the buttons moving "as people use their controllers",
then -- after a fix that reserved height for the message above them -- as still
moving. The second report is the interesting one: reserving one element's
height was necessary and nowhere near sufficient, because **five** things
between the card's top and its buttons change size independently.

Measured on the reference Pi and against mock adapters, at 1000-1700px:

    write-stats     0 -> 20px   appears on the first packet, so the buttons
                                drop as somebody starts to play
    manufacturer    20 -> 40px  "Realtek Semiconductor Corporation (93)" wraps
                                where "Cypress Semiconductor (305)" does not
    assignment      one line, or a line with an Unassign button in it
    region chips    0 -> 24px per wrapped row
    state text      wraps at a narrow card on the longer phrasings

Five `min-height`s that each have to be right at every card width is a fix that
is wrong somewhere. The row is pinned to the bottom of the card instead: the
grid already stretches every card in a row to one height, so the buttons land
on one line whatever sits above them.

That leaves one residual, and it is why this file exists rather than a comment.
Pinning holds the buttons still *within* a card, but the tallest card sets the
row -- so content arriving in **that** card still moves every button in the row.
Measured: 20px of write statistics appearing moved the buttons 4px, which was
what the auto margin had not already absorbed. Reserving that line as well took
it to zero:

    state       start  receiving  neutral  empty  noStats  withStats  longState
    before        986        986      986    986      982        986        986
    after         986        986      986    986      986        986        986

None of that is measurable here -- the suite has no rendering engine -- so what
is pinned is the structure the measurement depended on. Each assertion below
names the observation it stands in for.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
CSS = STATIC / "style.css"
ADAPTERS_JS = STATIC / "js" / "sections" / "adapters.js"


def declarations(selector: str) -> str:
    """The declaration block for one rule, by its exact selector text."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    marker = selector + " {"
    assert marker in css, f"no rule for {selector!r}"
    start = css.index(marker) + len(marker)
    return css[start : css.index("}", start)]


class _Tree(HTMLParser):
    """Enough of a DOM to ask what an element's children are."""

    VOID = {"input", "br", "img", "hr", "meta", "link"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "#root", "attrs": {}, "children": []}
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "children": []}
        self._stack[-1]["children"].append(node)
        if tag not in self.VOID:
            self._stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i]["tag"] == tag:
                del self._stack[i:]
                return

    def find(self, **attrs):
        def walk(node):
            if all(node["attrs"].get(k) == v for k, v in attrs.items()):
                yield node
            for child in node["children"]:
                yield from walk(child)

        return list(walk(self.root))


def card() -> _Tree:
    """The card markup `adapters.js` builds, parsed.

    Asserting on the template literal as *text* would pass on markup nested any
    which way, and the rule under test is entirely about nesting.
    """
    source = ADAPTERS_JS.read_text(encoding="utf-8")
    block = source.split("function adapterCardSkeleton", 1)[1]
    block = block.split("return `", 1)[1].split("`;", 1)[0]
    # The interpolations are not what is being checked and none of them lands
    # at either end of the body.
    tree = _Tree()
    tree.feed(re.sub(r"\$\{[^}]*\}", "", block))
    return tree


class TestTheActionRowIsPinnedToTheBottom:
    def test_the_body_grows_to_fill_the_card(self):
        """Without this the body is only as tall as its content, so there is no
        slack for an auto margin to take and the pin does nothing."""
        block = declarations('#adapters [data-field="body"]')
        assert "flex-direction: column" in block
        assert "flex: 1 1 auto" in block

    def test_the_last_row_takes_the_slack(self):
        block = declarations('#adapters [data-field="body"] > .card-row:last-child')
        assert "margin-top: auto" in block

    def test_the_buttons_really_are_that_last_row(self):
        """`:last-child`, so appending anything after the buttons silently
        unpins them -- the rule stops matching rather than matching something
        wrong, and nothing anywhere reports it."""
        bodies = card().find(**{"data-field": "body"})
        assert len(bodies) == 1
        last = bodies[0]["children"][-1]
        assert "card-row" in last["attrs"].get("class", ""), (
            "something has been added after the buttons; the pin no longer "
            "matches them"
        )
        fields = {c["attrs"].get("data-field") for c in last["children"]}
        assert {"power-button", "pair-button"} <= fields


class TestTheTwoLinesThatComeAndGoAreReserved:
    """The pin holds a card's own buttons still. The *row* is still set by
    whichever card is tallest, so a line that appears in that card moves every
    button beside it -- which is what the 4px residual was."""

    def test_the_message_under_the_preview_reserves_its_lines(self):
        block = declarations('.adapter-preview + [data-field="preview-hint"]')
        assert "min-height" in block

    def test_the_write_statistics_reserve_their_line(self):
        """0 -> 20px on the first packet. This is the one that moved the
        buttons as somebody started to play."""
        block = declarations('#adapters [data-field="write-stats"]')
        assert "min-height" in block

    def test_both_elements_are_in_the_card_under_those_names(self):
        """Three CSS rules address the card by `data-field`; a rename in the
        skeleton leaves them matching nothing, with no error anywhere."""
        tree = card()
        assert tree.find(**{"data-field": "preview-hint"})
        assert tree.find(**{"data-field": "write-stats"})
        preview = tree.find(**{"class": "adapter-preview", "data-field": "preview"})
        assert preview, "the hint's rule is an adjacent-sibling selector on this"
