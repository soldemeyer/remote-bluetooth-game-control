"""The web GUI's controller artwork, and the table that gives it meaning.

Two generated things have to agree with a third that is not generated at all:
the SVGs, the element-to-button table in `pad_layouts.js`, and
`client/gui/controller_layouts.py`, which is where both come from.

The failure this guards is quiet in a specific way. A wrong entry in the table
does not raise: the preview lights a control the player did not press, on an
adapter card, which reads as the *server* routing input to the wrong place --
and that is a subsystem with a long history of hard-won diagnoses to wade
through before anybody suspects a table in the browser.

The other half is staleness. Both outputs are committed so nothing at runtime
needs a build step, which means an edit to `controller_layouts.py` with no
regeneration ships a table describing the previous version. This regenerates
into memory and compares, the same way `test_design_tokens.py` does.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from client.config import DEFAULT_LAYOUT as CONFIG_DEFAULT_LAYOUT
from client.gui.controller_layouts import DEFAULT_LAYOUT, KIND_STICK, LAYOUTS
from common.state import Button
from tests.webjs import needs_node, run_node
from tools import build_web_controller_art as builder

ART_DIR = Path(__file__).resolve().parent.parent / "server" / "web" / "static" / "controllers"
TABLE = Path(__file__).resolve().parent.parent / "server" / "web" / "static" / "js" / "sections" / "pad_layouts.js"


def parsed_table() -> dict:
    source = TABLE.read_text(encoding="utf-8")
    body = source.split("PAD_LAYOUTS = ", 1)[1].rstrip().rstrip(";")
    return json.loads(body)


# ---------------------------------------------------------------------------
# Staleness
# ---------------------------------------------------------------------------


class TestTheCommittedOutputIsCurrent:
    def test_the_table_matches_the_generator(self):
        assert TABLE.read_text(encoding="utf-8") == builder.table_source(), (
            "pad_layouts.js is stale -- run "
            "`python -m tools.build_web_controller_art`"
        )

    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.key)
    def test_each_svg_matches_the_generator(self, layout):
        from tools.build_controller_art import SPECS, render

        spec = next(s for s in SPECS if s.key == layout.key)
        committed = (ART_DIR / f"{layout.key}.svg").read_text(encoding="utf-8")
        assert committed == render(spec), (
            f"{layout.key}.svg is stale -- run "
            f"`python -m tools.build_web_controller_art`"
        )

    def test_the_ghost_exists_and_has_no_controls(self):
        """The placeholder for an adapter with nothing assigned.

        Its own shape rather than a real pad drawn faintly, because a dimmed
        Xbox claims a controller type nobody chose -- the confidently-wrong
        display this GUI keeps having to unpick. No `c_` groups at all is what
        makes that impossible to get wrong.
        """
        from xml.etree import ElementTree

        tree = ElementTree.parse(ART_DIR / "ghost.svg")
        # Parsed rather than grepped: the generator's own boilerplate comment
        # names `id="c_name"`, so a text search matches the explanation of the
        # convention rather than any use of it.
        controls = [
            node.get("id") for node in tree.iter()
            if (node.get("id") or "").startswith("c_")
        ]
        assert not controls, f"the ghost carries controls that could light: {controls}"

    def test_the_hand_copy_is_gone(self):
        """`logical.svg` was byte-identical to `xbox.svg` with nothing saying
        so. Two copies of one picture is the drift this whole generator
        exists to remove."""
        assert not (ART_DIR / "logical.svg").exists()


# ---------------------------------------------------------------------------
# Art and hit boxes cannot drift apart
# ---------------------------------------------------------------------------


class TestEveryControlInTheMappingIsInThePicture:
    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.key)
    def test_the_svg_carries_every_element_the_table_names(self, layout):
        svg = (ART_DIR / layout.svg).read_text(encoding="utf-8")
        spec = parsed_table()[layout.key]
        named = list(spec["buttons"]) + list(spec["triggers"]) + spec["sticks"]
        assert named, f"{layout.key} maps nothing at all"
        missing = [e for e in named if f'id="{e}"' not in svg]
        assert not missing, (
            f"{layout.key}.svg has no group for {missing} -- the preview would "
            f"silently never light them"
        )

    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.key)
    def test_every_bindable_control_reaches_the_table(self, layout):
        spec = parsed_table()[layout.key]
        known = set(spec["buttons"]) | set(spec["triggers"]) | set(spec["sticks"])
        for control in layout.controls:
            if not control.button:
                continue                        # decoration, never lights
            assert control.element in known, (
                f"{layout.key}: {control.element} is bindable in Python and "
                f"absent from the browser's table"
            )


# ---------------------------------------------------------------------------
# The bits themselves
# ---------------------------------------------------------------------------


class TestTheBitsAreThePythonBits:
    @pytest.mark.parametrize("layout", LAYOUTS, ids=lambda layout: layout.key)
    def test_each_element_carries_its_own_button(self, layout):
        spec = parsed_table()[layout.key]
        mapped = {**spec["buttons"], **spec["triggers"]}
        for control in layout.controls:
            if not control.button or control.kind == KIND_STICK:
                continue
            assert mapped.get(control.element) == int(control.button), (
                f"{layout.key}: {control.element} should be "
                f"{int(control.button)}, table says {mapped.get(control.element)}"
            )

    def test_the_n64_c_buttons_ride_the_bits_they_borrow(self):
        """The case a flat table gets wrong.

        An N64 has no right stick, no Back and no Guide, so the C cluster takes
        those bits. A single map shared across families would have to pick one
        meaning for bit 10, and whichever it picked would be wrong somewhere.
        """
        n64 = parsed_table()["n64"]["buttons"]
        assert n64["c_cdown"] == int(Button.RIGHT_STICK)
        assert n64["c_cright"] == int(Button.BACK)
        assert n64["c_cleft"] == int(Button.GUIDE)
        assert n64["c_cup"] == int(Button.CAPTURE)

    def test_a_digital_trigger_is_a_button_not_a_trigger(self):
        """The N64's Z is a switch. Filed under `triggers` it would be drawn
        from an analog value the pad can never report, so it would light only
        when the *bit* happened to be set -- which is the same thing, reached
        by a path that looks like partial travel and is not."""
        n64 = parsed_table()["n64"]
        assert "c_lt" in n64["buttons"]
        assert "c_lt" not in n64["triggers"]

        xbox = parsed_table()["xbox"]
        assert "c_lt" in xbox["triggers"], "a real analog trigger lost its travel"


# ---------------------------------------------------------------------------
# Vocabularies that must agree
# ---------------------------------------------------------------------------


class TestTheVocabulariesAgree:
    def test_every_family_the_client_can_send_has_art(self):
        assert set(parsed_table()) == {layout.key for layout in LAYOUTS}

    def test_the_default_agrees_across_all_three_places(self):
        """`client/config.py` spells it out rather than importing it, so that
        it stays free of the GUI layer. That is a copy, and a copy needs a
        pin."""
        table = TABLE.read_text(encoding="utf-8")
        declared = re.search(r"DEFAULT_LAYOUT = \"([^\"]+)\"", table)
        assert declared, "the browser's table declares no default"
        assert declared.group(1) == DEFAULT_LAYOUT
        assert CONFIG_DEFAULT_LAYOUT == DEFAULT_LAYOUT

    def test_the_default_is_a_family_that_exists(self):
        assert DEFAULT_LAYOUT in parsed_table()


# ---------------------------------------------------------------------------
# The fallback, which is the one runtime decision in pad.js
# ---------------------------------------------------------------------------


@needs_node
class TestAnUnknownFamilyFallsBack:
    """A client one version ahead names a family this server has never heard
    of. Blanking the card there would read as a controller that is not
    connected -- the opposite of what has happened, and the reading that sends
    somebody to check cables."""

    BODY = """
        const mod = await import(BASE + '/js/sections/pad.js');
        const table = await import(BASE + '/js/sections/pad_layouts.js');
        console.log(JSON.stringify({
          unknown: mod.resolveFamily('gamecube'),
          empty: mod.resolveFamily(''),
          missing: mod.resolveFamily(undefined),
          known: mod.resolveFamily('n64'),
          fallback: table.DEFAULT_LAYOUT,
          inherited: mod.resolveFamily('constructor'),
        }));
    """

    def result(self):
        return json.loads(run_node(self.BODY))

    def test_an_unknown_name_becomes_the_default(self):
        out = self.result()
        assert out["unknown"] == out["fallback"]

    def test_so_do_the_empty_and_missing_cases(self):
        out = self.result()
        assert out["empty"] == out["fallback"]
        assert out["missing"] == out["fallback"]

    def test_a_known_name_is_left_alone(self):
        assert self.result()["known"] == "n64"

    def test_an_inherited_property_is_not_a_family(self):
        """`'constructor' in PAD_LAYOUTS` is true for every plain object, so a
        membership test written the obvious way would accept it and then ask
        for `PAD_LAYOUTS.constructor.svg`."""
        out = self.result()
        assert out["inherited"] == out["fallback"]


# ---------------------------------------------------------------------------
# The XML trap that has cost time three times
# ---------------------------------------------------------------------------


def test_every_served_svg_parses():
    """A double hyphen inside an XML comment makes the whole file invalid,
    with no error beyond the renderer refusing it."""
    from xml.etree import ElementTree

    files = sorted(ART_DIR.glob("*.svg"))
    assert files, "no artwork is being served"
    for path in files:
        ElementTree.parse(path)
