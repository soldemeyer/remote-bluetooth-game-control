"""What stays still on a controller card, and what is allowed to move.

Reported three times, each time as the same sentence: things move while
somebody is playing. Three different causes, and the first two fixes were each
correct and insufficient.

    1  the hint under the preview changes length as a stick moves
    2  five more things above the buttons change size independently
    3  the block itself still began wherever the head happened to end

The operator's requirement, and what the rules below exist to hold: the
**assignment row, the controller preview, the split-screen view and the
buttons** sit on the same line across every card, and do not move as the text
and artwork around them change.

The mechanism is one idea plus its preconditions. The grid already stretches
every card in a row to a common height, so the whole lower block is anchored to
the card's bottom edge -- and then nothing above it can reach it, however the
manufacturer wraps or the state sentence changes. For that to align the block's
*contents* as well, everything inside it has to be the same height on every
card, so each thing that comes and goes is reserved: the assigned box against
the empty one, the hint's two lines, the write statistics.

Measured on the reference Pi, offsets from the card's top, two adapters side by
side at 1280px -- one whose manufacturer string wraps and one whose does not:

                    assignment  preview  split-screen  buttons
    before, hci3           150      192           359      469
    before, hci0           170      207           373      469
    after,  both           170      212           379      479

and through every state a card passes while somebody plays -- the state
sentence, the hint, the write statistics arriving, a controller being assigned
and unassigned, regions added and removed -- all four anchors hold one position
at 1400px and 1000px.

Two things still move the block, both deliberate: a pairing window opening and
a HID error appearing each add a line to the head, and the card grows. Those
are exceptional, deliberate and transient, and reserving space for them would
cost every card a permanent blank where a fault message will almost never go.

None of this is measurable here -- the suite has no rendering engine -- so what
is pinned is the structure the measurement rested on, with the numbers in each
test. Two of them are behavioural rather than CSS greps, and they are the ones
that matter: the auto margin only bottom-anchors the block if the assignment
row is genuinely first, and `:last-child` stops matching the buttons entirely
if anything is appended after them.
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

    def test_the_first_element_takes_the_slack(self):
        """**On the assignment row, not on the buttons.**

        Pinning only the last row held the buttons still and left everything
        above them floating, because the block still began wherever the head
        happened to end. Measured on the reference Pi at 1280px:

            hci3 (Cypress)   assignment 150  preview 192  split 359
            hci0 (Realtek)   assignment 170  preview 207  split 373

        -- 20px, entirely because "Realtek Semiconductor Corporation (93)"
        wraps where "Cypress Semiconductor (305)" does not.
        """
        block = declarations('#adapters [data-field="body"] > [data-field="assignment"]')
        assert "margin-top: auto" in block

    def test_the_buttons_do_not_take_it_as_well(self):
        """Two auto margins split the slack between them instead of one taking
        it all, which would put the block back in the middle of the card."""
        block = declarations('#adapters [data-field="body"] > .card-row:last-child')
        assert "margin-top: auto" not in block

    def test_the_assignment_row_is_the_first_element_of_the_block(self):
        """The counterpart of the `:last-child` check below: the auto margin
        bottom-anchors everything after it, so anything inserted *before* the
        assignment row is silently left out of the alignment."""
        bodies = card().find(**{"data-field": "body"})
        first = bodies[0]["children"][0]
        assert first["attrs"].get("data-field") == "assignment"

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

    def test_the_assigned_box_reserves_the_button(self):
        """The assigned box carries an Unassign button and the empty one a line
        of text -- 40px against 36. Bottom-anchoring makes that 4px move the
        preview and the split-screen view on every card without a
        controller."""
        block = declarations('#adapters [data-field="assignment"] .assigned-to')
        assert "min-height" in block

    def test_the_hint_reserves_whole_lines_at_a_pinned_size(self):
        """`2.8em` reserved 33.6px where a line is 18 -- more than one and less
        than two, so the message held still until it wrapped and then moved
        2.4px. Measured on the Pi at 1280px: hint 36 against 34, assignment 167
        against 170.

        The font is pinned because the two hint states carry different classes
        and `.small` resolves to 0.85em (12.75px) where `muted small` is 12 --
        so `em` meant something different in each and the same reserve came out
        36 against 38.25.
        """
        block = declarations('.adapter-preview + [data-field="preview-hint"]')
        assert "font-size: 12px" in block
        assert "line-height: 1.5" in block
        # 3em at 12px is 36px, which is two lines at that line height.
        assert "min-height: 3em" in block

    def test_the_write_statistics_are_held_to_one_line(self):
        """Reserving the line stops the element *appearing* from moving
        anything. It does not stop the line wrapping -- the text is three
        measurements and a counter, so it grows with the latency and with how
        long somebody has been playing, and a card narrow enough to wrap it
        pushed the split-screen view up by a line."""
        selector = (
            '#adapters [data-field="write-stats"],' + chr(10)
            + '#adapters [data-field="write-stats"] > div'
        )
        block = declarations(selector)
        assert "white-space: nowrap" in block
        assert "text-overflow: ellipsis" in block

    def test_the_whole_reading_is_still_reachable(self):
        """An ellipsis is only acceptable because nothing is lost: the full
        text is on the element's title."""
        source = ADAPTERS_JS.read_text(encoding="utf-8")
        block = source.split("const write = channel.write_ms", 1)[1]
        block = block.split(chr(10) + "}", 1)[0]
        assert "stats.title = write" in block

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
