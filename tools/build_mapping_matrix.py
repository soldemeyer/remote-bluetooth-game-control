"""Write the built-in default mappings out as one readable matrix.

    python -m tools.build_mapping_matrix

Produces ``docs/controller_mapping_matrix.md``: for every virtual controller,
which control on each built-in physical preset drives each of its outputs --

    physical preset -> physical input -> canonical input -> virtual output

The presets are a rule (see :mod:`client.gui.controller_presets`), so this is
the rule's output, generated rather than written, and committed so it can be
read and reviewed without running anything. ``tests/test_default_mappings.py``
regenerates it in memory and compares, so a change to the rule that forgets to
re-run this fails rather than leaving the document describing last month.

What a cell shows is what the pad *prints* on the control, from each family's
``labels``. Whether a particular pad actually has that control is decided when
the preset meets it -- SDL's database, a measurement, or the heuristic -- so a
cell naming a control an older pad lacks (an Xbox 360 has no Share) simply
stays unbound on that pad. The N64 Mod Kit column is resolved against its
measured table, which is why it names raw indices and can say "not on this
pad".
"""

from __future__ import annotations

from pathlib import Path

from client.gui.controller_layouts import LAYOUTS, Layout
from client.gui.controller_presets import (
    _BIT_TO_CONTROL,
    FAMILIES,
    PadFamily,
    build_layout_preset,
    _resolve_control,
)
from client.input.mapping import canonical_name
from common.state import Button

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "controller_mapping_matrix.md"

#: Where the evidence was thin or sources disagreed. Kept here, beside the
#: rule's output, so a reader of the matrix sees the doubt next to the answer.
AMBIGUITIES: tuple[tuple[str, str], ...] = (
    (
        "GameCube face buttons",
        "Positional here: A bottom, B left, X right, Y top -- the GameCube "
        "pad's own geometry around its big A. RetroArch's Dolphin core, "
        "standalone Dolphin and Nintendo Switch Online all map by *label* "
        "instead (GC A on the pad's A), and they disagree with each other on "
        "where that A is.",
    ),
    (
        "N64 B",
        "Left face button, with the right one as a second source -- "
        "RetroArch's Mupen64Plus core (A = RetroPad B, B = RetroPad Y). "
        "Nintendo Switch Online's N64 app maps by label instead: B on the "
        "bottom button, A on the right.",
    ),
    (
        "Genesis X / Y",
        "X from the left bumper, Y from the top face button: Genesis Plus GX "
        "(X = L1, Y = RetroPad X) and the 8BitDo M30 in X-input mode. The "
        "Genesis layout's own note says its output bits match 'how an 8BitDo "
        "dongle presents it', which is unverified; only the physical side "
        "changed here.",
    ),
    (
        "NES A / B",
        "A from the right face button, B from the bottom one (RetroArch's NES "
        "cores, Switch Online's NES app). The NES layout itself still sends "
        "NES A on our A bit.",
    ),
    (
        "PlayStation touchpad",
        "Its click drives a Switch's Capture -- the 8BitDo Wireless USB "
        "Adapter's convention. There is no logical touchpad output, so a PS5 "
        "target cannot receive one.",
    ),
    (
        "8BitDo Bluetooth 'Star'",
        "Reports as SDL's misc1 in some modes and not at all in others; which "
        "mode the pad booted in decides it.",
    ),
    (
        "8BitDo N64 Mod Kit",
        "Measured on one kit over USB in D-input mode on Windows. Raw indices "
        "are platform-specific: on Linux or over Bluetooth they may differ, "
        "and the preset then reports itself as approximate. Home, Star and "
        "Pair were not measured and are not bound. Its right and top face "
        "buttons on a modern target are unbound: an N64 pad has neither, and "
        "its C buttons are already the right stick there.",
    ),
    (
        "Switch Pro server profile",
        "Not changed here, and worth knowing: the server's Switch Pro profile "
        "sends our A bit as the Switch's A (right-hand) button, so on a real "
        "Switch the virtual Switch layout's positional labels come out "
        "mirrored. The virtual NES, N64 and GameCube A land on the Switch's A, "
        "which is what Switch Online's apps expect.",
    ),
)


def _header_cell(family: PadFamily) -> str:
    return family.name


def _physical(family: PadFamily, control: str) -> str:
    """What drives a control on this family, as a player would name it."""
    measured = family.measured
    if measured is None:
        return family.label(control)

    bindings = measured.bindings()
    source = _resolve_control(control, bindings["buttons"], bindings["axes"])
    if source is None:
        return "— (not on this pad)"
    return f"{family.label(control)} ({source.describe()})"


def _axis_physical(family: PadFamily, axis: str) -> str:
    measured = family.measured
    if measured is None:
        return family.label(axis)
    binding = measured.axes.get(axis)
    if binding is None:
        return "— (not on this pad)"
    return f"{family.label(axis)} ({binding.describe()})"


_TRIGGER_BIT = {
    "left_trigger": int(Button.LEFT_TRIGGER),
    "right_trigger": int(Button.RIGHT_TRIGGER),
}
_TRIGGER_AXIS = {bit: axis for axis, bit in _TRIGGER_BIT.items()}


def _trigger_cell(family: PadFamily, preset, axis: str) -> str:
    """An analog trigger's cell: its axis, or the button standing in for it.

    A pad whose trigger is a plain switch -- an N64's Z -- has no axis to give,
    and a per-pad rule may drive the trigger from a different control
    altogether. Either way the trigger arrives as a full press, and the cell
    says so rather than claiming there is nothing.
    """
    control = preset.buttons.get(_TRIGGER_BIT[axis], "")
    if control == axis:
        analog = _axis_physical(family, axis)
        if not analog.startswith("—"):
            return analog
    if not control:
        return "—"
    digital = _physical(family, control)
    if digital.startswith("—"):
        return digital
    marker = f" † `{canonical_name(control)}`" if control != axis else ""
    return f"{digital}, full press{marker}"


def _layout_section(layout: Layout) -> list[str]:
    presets = {family.key: build_layout_preset(family, layout) for family in FAMILIES}
    optional = layout.optional_bits()
    labels = dict(layout.bindable())

    header = ["Virtual output", "Canonical input"] + [
        _header_cell(family) for family in FAMILIES
    ]
    lines = [
        f"## {layout.name} (`{layout.key}`)",
        "",
        layout.note.replace("\n\n", " "),
        "",
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]

    # The rule every family starts from, so a family that differs can be
    # marked rather than silently disagreeing with the row's canonical input.
    baseline = build_layout_preset(FAMILIES[0], layout)

    for bit, label in layout.bindable():
        # An analog trigger is one control: its row is the axis, below, as
        # it is in the mapping screen.
        if bit in _TRIGGER_AXIS and layout.has_axis(_TRIGGER_AXIS[bit]):
            continue
        control = baseline.buttons.get(bit) or _BIT_TO_CONTROL.get(bit, "")
        if not control:
            continue
        name = label + (" *(optional)*" if bit in optional else "")
        canonical = canonical_name(control)
        alternate = baseline.alternates.get(bit)
        if alternate:
            canonical += f" (+ {canonical_name(alternate)})"

        cells = []
        for family in FAMILIES:
            chosen = presets[family.key].buttons.get(bit, "")
            cell = _physical(family, chosen) if chosen else "—"
            if chosen and chosen != control:
                cell += f" † `{canonical_name(chosen)}`"
            second = presets[family.key].alternates.get(bit)
            if second:
                other = _physical(family, second)
                if not other.startswith("—"):
                    cell += f" (+ {other})"
            cells.append(cell)
        lines.append(f"| {name} | {canonical} | " + " | ".join(cells) + " |")

    for axis in baseline.axes:
        axis_label = _axis_row_label(layout, axis, labels)
        cells = [
            _trigger_cell(family, presets[family.key], axis)
            if axis in _TRIGGER_BIT
            else _axis_physical(family, axis)
            for family in FAMILIES
        ]
        lines.append(
            f"| {axis_label} | {canonical_name(axis)} | " + " | ".join(cells) + " |"
        )

    lines += [
        "",
        "`†` a family-specific binding, with the canonical input it uses instead.",
        "",
    ]
    return lines


def _axis_row_label(layout: Layout, axis: str, labels: dict) -> str:
    trigger = {"left_trigger": Button.LEFT_TRIGGER, "right_trigger": Button.RIGHT_TRIGGER}
    if axis in trigger:
        return f"{labels.get(trigger[axis], axis)} (analog)"
    side = Button.LEFT_STICK if axis.startswith("left") else Button.RIGHT_STICK
    for control in layout.controls:
        if control.kind == "stick" and control.button == side:
            name = control.label if not control.clickable else (
                "Left stick" if side == Button.LEFT_STICK else "Right stick"
            )
            return f"{name} {axis[-1].upper()}"
    return axis


def document() -> str:
    lines = [
        "# Built-in controller mapping matrix",
        "",
        "<!-- GENERATED by tools/build_mapping_matrix.py from",
        "     client/gui/controller_presets.py and controller_layouts.py.",
        "     Do not edit; change the rule and re-run the tool. -->",
        "",
        "Every built-in physical preset against every virtual controller:",
        "",
        "    physical preset -> physical input -> canonical input -> virtual output",
        "",
        "Each table is one virtual controller. A row is one of its outputs; "
        "**Canonical input** is the positional control the rule asks for "
        "(`FACE_SOUTH` is the bottom face button on every pad, whatever it "
        "prints); each preset column is what that pad prints on it.",
        "",
        "**How a binding is chosen**, most specific last: identity by position; "
        "then a per-target rule (the N64's C buttons from the right stick); then "
        "a per-pad rule (an N64 mod kit has no right face button to give an "
        "SNES its A).",
        "",
        "A preset meets a real pad through SDL's controller database, which "
        "names controls by position -- the client turns off SDL2's label-based "
        "reporting for Nintendo pads so that holds for them too. A control a "
        "given pad lacks stays unbound. Rows marked *optional* are "
        "Switch-compatibility controls the original controller does not have; "
        "an alias such as `Start / +` is one output with two names, because "
        "on a Switch target our Start *is* Plus.",
        "",
    ]

    for layout in LAYOUTS:
        lines += _layout_section(layout)

    lines += ["## Where the knowledge came from", ""]
    for family in FAMILIES:
        lines.append(f"**{family.name}**")
        lines.append("")
        for source in family.sources:
            lines.append(f"- {source}")
        lines.append("")

    lines += ["## Ambiguities", ""]
    for title, text in AMBIGUITIES:
        lines.append(f"- **{title}.** {text}")
    lines.append("")

    return "\n".join(lines)


def build(path: Path = OUTPUT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document(), encoding="utf-8", newline="\n")
    return path


def main() -> int:
    path = build()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
