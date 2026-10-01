"""Write the built-in controller presets out as readable JSON.

The presets themselves are a *rule* -- see :mod:`client.gui.controller_presets`
-- because 8 pad families times 9 controller types is 72 mappings, and a rule
that derives them from the layout metadata cannot drift out of step with the
artwork the way 72 hand-written tables would.

What this tool produces is the rule's output, committed so it can be read,
diffed and shared. Two uses:

* **Review.** "Which physical control drives C-left on an Xbox pad targeting the
  N64?" is answerable by reading a file rather than by running the client.
  ``docs/controller_mapping_matrix.md`` (``python -m tools.build_mapping_matrix``)
  lays the same data out as one table per controller type.
* **Sharing.** The files carry the same envelope as the client's own export, so
  a player can import one and edit from it.

They are *not* loaded at runtime -- the client applies the rule directly, so
there is no packaged data file to lose and nothing to keep in sync. The test
suite regenerates them in memory and compares, so a stale commit is caught.

Usage::

    python -m tools.build_controller_presets
"""

from __future__ import annotations

import json
from pathlib import Path

from client.gui.controller_layouts import LAYOUTS_BY_KEY, get_layout
from client.gui.controller_presets import FAMILIES, PadFamily, build_preset
from client.input.mapping import button_label, canonical_name

#: Where the generated files land, beside the controller artwork.
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "client" / "gui" / "assets" / "presets"


def preset_document(family: PadFamily) -> dict:
    """One family's bindings for every controller type, as plain data."""
    layouts = []
    for entry in build_preset(family):
        layout = get_layout(entry.layout)
        optional = layout.optional_bits()
        buttons = []
        for bit, control in sorted(entry.buttons.items()):
            row = {
                "button": int(bit),
                "name": button_label(bit),
                "label": _layout_label(entry.layout, bit),
                "control": control,
                "canonical": canonical_name(control),
                "physical": family.label(control),
            }
            alternate = entry.alternates.get(bit)
            if alternate:
                row["alternate"] = alternate
            if bit in optional:
                row["optional"] = True
            buttons.append(row)
        layouts.append(
            {
                "layout": entry.layout,
                "layout_name": layout.name,
                "buttons": buttons,
                "axes": list(entry.axes),
            }
        )

    document = {
        "version": 2,
        "kind": "rbgc-controller-preset",
        "family": family.key,
        "name": family.name,
        "note": family.note,
        "sources": list(family.sources),
    }
    if family.measured is not None:
        document["measured"] = family.measured.provenance
    document["layouts"] = layouts
    return document


def _layout_label(layout_key: str, bit: int) -> str:
    """What this system calls the button -- "B" on an SNES for our ``A``."""
    layout = LAYOUTS_BY_KEY.get(layout_key)
    if layout is None:
        return ""
    return dict(layout.bindable()).get(bit, "")


def build(output_dir: Path = OUTPUT_DIR) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for family in FAMILIES:
        path = output_dir / f"{family.key}.json"
        path.write_text(
            json.dumps(preset_document(family), indent=2) + "\n", encoding="utf-8"
        )
        written.append(path)
    return written


def main() -> int:
    for path in build():
        document = json.loads(path.read_text(encoding="utf-8"))
        bindings = sum(len(entry["buttons"]) for entry in document["layouts"])
        print(f"{path.name:28} {len(document['layouts'])} types, {bindings} bindings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
