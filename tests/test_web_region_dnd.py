"""Dragging a region onto a controller, and seeing which one is live.

Two kinds of check, because the interaction has two kinds of failure.

The **vocabulary** tests pin duplicated tables against each other. The palette
markup and two JavaScript lookups spell out the same eight region names that
``common/screen_regions.py`` defines, and nothing at runtime compares them --
a name that drifts produces a chip labelled wrong, or highlighted under the
wrong layout, with no error anywhere.

The **behaviour** tests run the real chip renderer in Node. Which assignment
is shown as live is the whole point of the highlight, and it is a decision, so
it is worth testing rather than eyeballing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from common.screen_regions import REGIONS, REGIONS_FOR_LAYOUT, layout_of

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
ADAPTERS_JS = STATIC / "js" / "sections" / "adapters.js"


def js_object(name: str) -> dict[str, str]:
    """Parse a flat ``const NAME = { a: 'b', ... };`` out of adapters.js."""
    source = ADAPTERS_JS.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = \{{(.*?)\n\}};", source, re.S)
    assert match, f"{name} not found in adapters.js"
    return dict(re.findall(r"(\w+):\s*'([^']*)'", match.group(1)))


class TestTheVocabularyCannotDrift:
    """Three copies of the same eight names, and nothing compares them."""

    def test_the_javascript_labels_cover_every_region(self):
        assert set(js_object("REGION_LABELS")) == set(REGIONS)

    def test_the_javascript_layout_map_agrees_with_python(self):
        mapped = js_object("LAYOUT_OF")
        assert set(mapped) == set(REGIONS)
        for region, layout in mapped.items():
            assert layout == layout_of(region), region

    def test_the_palette_offers_every_region_exactly_once(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        palette = page[page.index('id="region-palette"'):page.index('id="region-armed-hint"')]
        found = re.findall(r'data-region="(\w+)"', palette)
        assert sorted(found) == sorted(REGIONS)
        assert len(found) == len(set(found)), "a region appears twice in the palette"

    def test_each_palette_group_holds_its_own_layout(self):
        """A region in the wrong little screen is a picture that lies about
        where the player will be looking."""
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        palette = page[page.index('id="region-palette"'):page.index('id="region-armed-hint"')]
        for block in palette.split('class="region-group"')[1:]:
            layout = re.search(r'data-layout="(\w+)"', block).group(1)
            for region in re.findall(r'data-region="(\w+)"', block):
                assert region in REGIONS_FOR_LAYOUT[layout], f"{region} in {layout}"

    def test_every_region_is_draggable(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        palette = page[page.index('id="region-palette"'):page.index('id="region-armed-hint"')]
        cells = re.findall(r"<button[^>]*data-region=\"\w+\"", palette)
        assert len(cells) == len(REGIONS)
        for cell in cells:
            assert 'draggable="true"' in cell

    def test_they_are_buttons_so_the_keyboard_can_reach_them(self):
        """A drag cannot be performed from the keyboard at all, and HTML5 drag
        events do not fire from touch. The click-to-arm path is the way in for
        both, and it needs a focusable, activatable element."""
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        palette = page[page.index('id="region-palette"'):page.index('id="region-armed-hint"')]
        assert palette.count("<button") == len(REGIONS)
        assert "<div" not in palette.split('class="region-screen')[1].split(">")[1][:200]


# -- the behaviour half, run in Node ---------------------------------------

pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; this check is advisory and skips cleanly",
)

HARNESS = textwrap.dedent(
    """
    // adapters.js imports dom.js, which registers window listeners at module
    // scope. Node has no such global; these are stub gaps, not faults.
    globalThis.addEventListener = () => {};
    globalThis.document = { querySelectorAll: () => [], addEventListener() {} };
    globalThis.window = globalThis;

    // Through the environment rather than argv: `node -e` shifts the
    // positional arguments, so an index here is a thing to get wrong once and
    // then spend a while wondering about.
    const mod = await import('file://' + process.env.RBGC_MODULE.replace(/\\\\/g, '/'));
    const cases = JSON.parse(process.env.RBGC_CASES);
    const out = cases.map(([addr, regions, live]) =>
      mod.regionChipsHtml(addr, regions, live));
    console.log(JSON.stringify(out));
    """
).strip()


def render(cases) -> list[str]:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS],
        capture_output=True, text=True, timeout=60,
        env={
            **os.environ,
            "RBGC_MODULE": str(ADAPTERS_JS),
            "RBGC_CASES": json.dumps(cases),
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytestmark_node
class TestTheChipsAndTheHighlight:
    ADDR = "00:00:00:00:00:01"

    def test_nothing_assigned_renders_nothing(self):
        assert render([[self.ADDR, [], "QUAD_4"]])[0].strip() == ""

    def test_one_chip_per_assignment(self):
        html = render([[self.ADDR, ["upper_left", "left", "upper"], "FULL"]])[0]
        # Counted on the chip's own class, spelled with its closing quote:
        # a bare "region-chip" also matches "region-chip-name" and the count
        # comes out double, which reads as a duplicated chip.
        assert html.count('class="region-chip"') == 3
        for label in ("Upper left", "Left", "Upper"):
            assert f">{label}<" in html

    def test_only_the_live_layouts_chip_is_highlighted(self):
        """The requirement the highlight exists for: a controller holds three
        assignments and exactly one of them is being sent."""
        html = render([[self.ADDR, ["upper_left", "left", "upper"], "QUAD_4"]])[0]
        assert 'class="region-chip live" data-region="upper_left"' in html
        assert 'class="region-chip" data-region="left"' in html
        assert 'class="region-chip" data-region="upper"' in html

    def test_the_highlight_moves_with_the_layout(self):
        regions = ["upper_left", "left", "upper"]
        for layout, expected in (
            ("QUAD_4", "upper_left"),
            ("VERTICAL_2", "left"),
            ("HORIZONTAL_2", "upper"),
        ):
            html = render([[self.ADDR, regions, layout]])[0]
            assert f'class="region-chip live" data-region="{expected}"' in html
            assert html.count("region-chip live") == 1

    def test_a_full_screen_game_highlights_nothing(self):
        """Correct rather than a gap: no split is on screen, so no assignment
        is in effect and every player sees the whole picture."""
        html = render([[self.ADDR, ["upper_left", "left"], "FULL"]])[0]
        assert "region-chip live" not in html

    def test_every_chip_has_a_remove_button_for_itself(self):
        html = render([[self.ADDR, ["upper_left", "lower_right"], "QUAD_4"]])[0]
        for region in ("upper_left", "lower_right"):
            assert (
                'data-action="region-remove"' in html
                and f'data-region="{region}"' in html
            )
        assert html.count('data-action="region-remove"') == 2
        assert html.count(f'data-addr="{self.ADDR}"') == 2

    def test_the_remove_button_is_labelled_for_a_screen_reader(self):
        html = render([[self.ADDR, ["lower_right"], "QUAD_4"]])[0]
        assert 'aria-label="Remove Lower right"' in html

    def test_an_unknown_region_does_not_break_the_row(self):
        """It cannot come from the palette, but it can come from a config file
        edited by hand -- and a thrown exception inside a render would take the
        whole card with it."""
        html = render([[self.ADDR, ["nonsense"], "QUAD_4"]])[0]
        assert "nonsense" in html
        assert "region-chip live" not in html


class TestMockModeStillShowsThem:
    """`--mock-bt` reports no hardware at all, so the adapter list is built
    from the router's channels by a fixed field literal in the renderer.

    That literal is the same shape as the trap `upsert_adapter` documents on
    the server: a field added to the real row and forgotten here is simply
    missing. `regions` was exactly that for one commit -- assignments saved
    correctly and no chip ever appeared -- on the one path anybody can run
    without Bluetooth hardware, which is where this gets tried first.
    """

    def test_the_fallback_row_carries_regions(self):
        source = ADAPTERS_JS.read_text(encoding="utf-8")
        fallback = source[source.index("channels.map((c) => ({"):]
        fallback = fallback[:fallback.index("}));")]
        assert "regions:" in fallback

    def test_the_channel_snapshot_provides_them(self):
        """The other half: the renderer can only forward what the router
        actually publishes."""
        from server.bt.profiles import create_profile
        from server.bt.sink import MockSink
        from server.router import OutputChannel

        channel = OutputChannel(
            bd_addr="00:00:00:00:00:01", hci_name="mock0",
            profile=create_profile("generic"), sink=MockSink(name="mock0"),
            regions=["upper_left"],
        )
        assert channel.snapshot()["regions"] == ["upper_left"]
