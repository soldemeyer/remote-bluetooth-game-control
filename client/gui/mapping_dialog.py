"""Controller and keyboard mapping screen.

One dialog serves three needs that are really the same need:

  * make a gamepad SDL has no layout for usable at all;
  * remap a recognised gamepad's buttons;
  * bind the keyboard as a controller.

All three are "point a logical button at a physical control", so all three use
the same press-to-bind flow and the same live preview. The preview shows the
state we are actually about to transmit, which is the only way to confirm a
binding without walking to the console.

Capture works by diffing against a baseline taken the moment Bind is pressed,
rather than by waiting for an event. A gamepad axis is never truly at rest, and
several controls may already be held, so "first control that *changes*" is far
more reliable than "first control that is down".
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.gui.controller_layouts import LAYOUTS, get_layout
from client.gui.controller_preview import ControllerPreview
from client.input.mapping import (
    AXIS_PRESS_THRESHOLD,
    STICK_AXES,
    TRIGGER_AXES,
    AxisBinding,
    DeviceMapping,
    InputSource,
    KeyAxisBinding,
    SourceKind,
    button_label,
    default_joystick_mapping,
)
from common.state import Button, ControllerState

log = logging.getLogger(__name__)

#: Preview refresh. Fast enough to feel instant, far below the input loop rate.
_TICK_MS = 16

#: How far a joystick axis must move from its baseline to count as "the player
#: moved this one" -- about 80% of full travel.
#:
#: Deliberately near the extent rather than merely past the noise floor. At the
#: old third-of-travel setting, nudging a stick while reaching for a face
#: button, or a diagonal on the way to a cardinal direction, was enough to bind
#: the wrong control -- and the binding looked deliberate afterwards. Asking for
#: a decisive push costs the player nothing and removes the whole class of
#: accidental captures.
_AXIS_CAPTURE_DELTA = 26000

#: A stick must fall back inside this of centre before the opposite direction
#: is accepted. Releasing a fully deflected stick is a large movement in the
#: other direction, and without this it would be read as the next push.
_AXIS_REARM_LEVEL = 8000

#: How long any control must be held before its binding is taken.
#:
#: A control brushed on the way past reads identically to one pressed on
#: purpose for a single frame, so thresholds alone cannot tell them apart.
#: Requiring the input to *persist* can, and it costs a deliberate press
#: nothing. Applies to buttons, hats and axis halves alike; the preview draws
#: a filling ring during the wait so the pause reads as intended rather than
#: as an unresponsive control. Keyboard keys stay instant -- typing is already
#: deliberate, and half a second per key would make binding a chore.
_HOLD_TO_BIND_S = 0.6


class MappingDialog(QDialog):
    """Bind logical buttons to physical controls, with a live preview."""

    def __init__(
        self,
        backend,
        device,
        configuration,
        parent: QWidget | None = None,
        *,
        store=None,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._device = device
        self._is_keyboard = _is_keyboard(device)

        #: Where "Save as..." puts the copy. Without it the dialog can only
        #: hand its result back on accept, which is not enough: "Save as..."
        #: has to create the copy and *keep editing it*, so the new
        #: configuration must exist while the dialog is still open.
        self._store = store

        # Edited in place on Save, discarded on Cancel: the caller owns the
        # object and re-reads it only if the dialog was accepted.
        self._configuration = configuration
        self._mapping = configuration.mapping
        if self._mapping.is_empty():
            self._mapping = self._starting_mapping()
            self._configuration.mapping = self._mapping

        #: Guided binding: the queue of (layout key, target) still to ask for,
        #: where a target is a logical Button bit or an axis name. None when the
        #: player is binding individual controls by hand.
        self._wizard: list[tuple[str, object]] | None = None
        self._wizard_index = 0
        #: Targets the wizard has left unbound, for the summary at the end.
        self._wizard_skipped = 0

        #: True once "Save as..." has put a copy in the store. The caller needs
        #: to know even if the dialog is then cancelled -- the copy exists, and
        #: dropping it because the last click was Cancel would lose work the
        #: player was told had been saved.
        self._saved_as = False

        #: Logical button currently being captured, or None.
        self._capturing: int | None = None
        #: True when the capture is filling the *second* source for that
        #: button rather than replacing the first.
        self._capturing_alt = False
        #: Axis name currently being captured, or None.
        self._axis_capture: str | None = None
        #: Which half of an axis we are asking for: "negative" (left / up) then
        #: "positive" (right / down). Used for sticks and for keyboard pairs
        #: alike; triggers have one direction and never leave "negative".
        self._axis_stage = "negative"
        #: (axis index, sign) recorded from the first half, so the second can
        #: confirm the same axis and reveal whether it is wired backwards.
        self._axis_pending: tuple[int, int] | None = None
        #: False while waiting for a deflected stick to return to centre.
        self._axis_rearmed = True
        #: (identity, started_at) for the input being held towards a binding,
        #: or None. The identity is whatever distinguishes the input -- an
        #: (axis, sign) pair, or the InputSource for a button -- so changing
        #: inputs mid-hold restarts the clock. Reset the moment it is released.
        self._input_hold: tuple[object, float] | None = None
        #: What every control read when capture began.
        self._baseline: dict | None = None
        self._baseline_keys: frozenset[int] = frozenset()

        self._state = ControllerState()
        #: Always-neutral, shown instead of live input during the walk-through.
        self._neutral = ControllerState()
        self._rows: dict[int, QLabel] = {}
        self._bind_buttons: list[QPushButton] = []

        self.setWindowTitle(f"Configure — {device.display_name()}")
        self.setMinimumSize(880, 620)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        self._build_ui(self._configuration.layout)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_TICK_MS)

        # Keys must reach us even when a button inside the dialog has focus.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- construction ------------------------------------------------------

    def _starting_mapping(self) -> DeviceMapping:
        """A first guess at the bindings, trimmed to the type being edited.

        The generic defaults cover a full modern pad, so seeding an NES
        configuration with them bound fifteen controls to a type that has
        eight -- and the extra bits were really sent to the console, since a
        binding the list does not show still travels. Trimming keeps the
        configuration honest about what it is.
        """
        if self._is_keyboard:
            from client.input.keyboard_backend import default_keyboard_mapping

            mapping = default_keyboard_mapping()
        else:
            mapping = default_joystick_mapping(
                self._device.guid,
                self._device.name,
                axes=self._device.axis_count,
                buttons=self._device.button_count,
                hats=self._device.hat_count,
            )

        return _trim_to_layout(mapping, self._configuration.layout)

    def _build_ui(self, preview_layout: str) -> None:
        root = QVBoxLayout(self)

        header = QLabel(self._header_text())
        header.setWordWrap(True)
        header.setStyleSheet("padding: 4px 2px;")
        root.addWidget(header)

        columns = QHBoxLayout()
        root.addLayout(columns, 1)

        # -- left: preview --
        left = QVBoxLayout()
        columns.addLayout(left, 3)

        chooser = QHBoxLayout()
        chooser.addWidget(QLabel("Editing"))
        # Read-only: renaming is what "Save as..." is for. A free-text name
        # beside a Save button made it ambiguous whether typing a new name
        # renamed this configuration or created another.
        self._name_label = QLabel()
        self._name_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        chooser.addWidget(self._name_label, 2)

        chooser.addWidget(QLabel("Controller type"))
        self._layout_combo = QComboBox()
        for layout in LAYOUTS:
            self._layout_combo.addItem(layout.name, layout.key)
        index = self._layout_combo.findData(preview_layout)
        if index >= 0:
            self._layout_combo.setCurrentIndex(index)
        self._layout_combo.currentIndexChanged.connect(self._on_layout_changed)
        chooser.addWidget(self._layout_combo, 1)
        left.addLayout(chooser)

        self._preview = ControllerPreview()
        self._preview.set_layout_key(preview_layout)
        left.addWidget(self._preview, 1)

        self._note = QLabel(self._preview.note)
        self._note.setWordWrap(True)
        font = QFont(self._note.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        self._note.setFont(font)
        left.addWidget(self._note)

        self._status = QLabel("Press Bind, then the control you want to use.")
        self._status.setWordWrap(True)
        left.addWidget(self._status)

        # -- right: bindings --
        right = QVBoxLayout()
        columns.addLayout(right, 2)

        right.addWidget(self._build_wizard_bar())
        right.addWidget(self._build_binding_list(), 1)

        buttons = QDialogButtonBox()
        self._button_box = buttons
        reset = buttons.addButton("Reset to defaults", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._on_reset)
        clear = buttons.addButton("Clear all", QDialogButtonBox.ButtonRole.ActionRole)
        clear.clicked.connect(self._on_clear)

        self._save_button = buttons.addButton(QDialogButtonBox.StandardButton.Save)
        self._save_as_button = buttons.addButton(
            "Save as…", QDialogButtonBox.ButtonRole.ApplyRole
        )
        self._save_as_button.clicked.connect(self._on_save_as)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_identity()

    def _header_text(self) -> str:
        if self._is_keyboard:
            return (
                "<b>Keyboard</b> — bindings only register while this window has focus. "
                "Gamepads keep working in the background; the keyboard cannot, because "
                "reading it globally would mean installing a system-wide key hook."
            )
        note = "" if self._device.is_mapped else (
            " This pad has no built-in layout, so the bindings below started as a "
            "<i>guess</i>. Check each one against the preview."
        )
        return (
            f"<b>{self._device.display_name()}</b> — "
            f"{self._device.axis_count} axes, {self._device.button_count} buttons, "
            f"{self._device.hat_count} hat(s).{note}"
        )

    def _build_wizard_bar(self) -> QWidget:
        """Guided binding: start buttons, and the running step display.

        Both live in one widget that swaps between two rows, because they are
        the same thing in two states -- offering the walk-through, and being in
        the middle of it.
        """
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 6)

        # -- idle: the two entry points --
        self._wizard_start_row = QWidget()
        start = QHBoxLayout(self._wizard_start_row)
        start.setContentsMargins(0, 0, 0, 0)

        self._wizard_one = QPushButton("Bind this type…")
        self._wizard_one.setToolTip(
            "Step through every control of the controller type shown, asking "
            "for each one in turn."
        )
        self._wizard_one.clicked.connect(lambda: self._start_wizard(False))
        start.addWidget(self._wizard_one)

        self._wizard_all = QPushButton("Bind all types…")
        self._wizard_all.setToolTip(
            "The same walk-through, for every controller type in this "
            "configuration — Xbox through to Genesis."
        )
        self._wizard_all.clicked.connect(lambda: self._start_wizard(True))
        start.addWidget(self._wizard_all)
        start.addStretch(1)

        outer.addWidget(self._wizard_start_row)

        # -- running: what to press, and how far through --
        self._wizard_row = QWidget()
        running = QVBoxLayout(self._wizard_row)
        running.setContentsMargins(0, 0, 0, 0)

        self._wizard_label = QLabel()
        self._wizard_label.setWordWrap(True)
        running.addWidget(self._wizard_label)

        self._wizard_progress = QProgressBar()
        self._wizard_progress.setTextVisible(False)
        self._wizard_progress.setFixedHeight(6)
        running.addWidget(self._wizard_progress)

        controls = QHBoxLayout()
        back = QPushButton("Back")
        back.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        back.clicked.connect(self._wizard_back)
        controls.addWidget(back)

        skip = QPushButton("Skip")
        skip.setToolTip(
            "This pad has no such control: clear it and move on. Esc does the "
            "same."
        )
        skip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        skip.clicked.connect(self._wizard_skip)
        controls.addWidget(skip)

        stop = QPushButton("Stop")
        stop.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        stop.clicked.connect(self._stop_wizard)
        controls.addWidget(stop)
        controls.addStretch(1)
        running.addLayout(controls)

        outer.addWidget(self._wizard_row)
        self._wizard_row.setVisible(False)

        return holder

    # -- guided binding ----------------------------------------------------

    def _wizard_targets(self, layout_key: str) -> list[tuple[str, object]]:
        """Every control one type needs, in the order the list shows them.

        Trigger *bits* are excluded where the layout has the matching axis: the
        axis step already captures that control, analog or digital. Leaving the
        bit in -- as an earlier version did -- asked for LT and RT twice, and
        the second answer just overwrote the first. A trigger with no analog
        travel has no axis step, so its bit is asked for here like any button.

        A stick axis contributes **two steps**, one per direction. They were
        one step with an internal two-half flow, which made Skip and Esc drop
        the axis's *other* direction as well -- skipping "Left" silently
        skipped "Right", and the walk-through appeared to never ask for it.
        A step the player can see is a step the player can skip alone.
        """
        layout = get_layout(layout_key)
        targets: list[tuple[str, object]] = [
            (layout_key, bit)
            for bit, _label in layout.bindable()
            if not (
                bit in _TRIGGER_BIT.values()
                and layout.has_axis(_TRIGGER_AXIS[bit])
            )
        ]
        for name in STICK_AXES + TRIGGER_AXES:
            if not layout.has_axis(name):
                continue
            if _axis_directions(name) is None:
                targets.append((layout_key, name))
            else:
                targets.append((layout_key, ("half", name, "negative")))
                targets.append((layout_key, ("half", name, "positive")))
        return targets

    def _start_wizard(self, every_type: bool) -> None:
        keys = (
            [layout.key for layout in LAYOUTS]
            if every_type
            else [self._layout_combo.currentData()]
        )

        queue: list[tuple[str, object]] = []
        for key in keys:
            queue.extend(self._wizard_targets(key))

        if not queue:
            return

        self._wizard = queue
        self._wizard_index = 0
        self._wizard_skipped = 0

        self._wizard_start_row.setVisible(False)
        self._wizard_row.setVisible(True)
        self._wizard_progress.setRange(0, len(queue))
        self._set_manual_controls_enabled(False)

        self._wizard_step()

    def _wizard_step(self) -> None:
        """Ask for the control at the current index."""
        if self._wizard is None:
            return
        if self._wizard_index >= len(self._wizard):
            self._finish_wizard()
            return

        layout_key, target = self._wizard[self._wizard_index]

        # Switching type swaps which mapping is being edited, so it must happen
        # before capture starts -- otherwise the binding lands on the previous
        # type's mapping.
        if layout_key != self._layout_combo.currentData():
            index = self._layout_combo.findData(layout_key)
            if index >= 0:
                self._layout_combo.setCurrentIndex(index)

        self._wizard_progress.setValue(self._wizard_index)

        # Start capture first: the label reads the capture state to decide
        # which half of a stick it is asking for, so labelling beforehand
        # showed the previous step's direction for one frame.
        if isinstance(target, tuple):
            self._start_axis_capture(target[1], stage=target[2])
        elif isinstance(target, str):
            self._start_axis_capture(target)
        else:
            self._start_capture(target)
        self._update_wizard_label(layout_key, target)

    def _refresh_wizard_label(self) -> None:
        """Re-state the step, now that the axis has moved on to its other half."""
        if self._wizard is None or self._wizard_index >= len(self._wizard):
            return
        layout_key, target = self._wizard[self._wizard_index]
        self._update_wizard_label(layout_key, target)

    def _update_wizard_label(self, layout_key: str, target) -> None:
        layout = get_layout(layout_key)
        total = len(self._wizard or ())
        prefix = f"Step {self._wizard_index + 1} of {total} — <b>{layout.name}</b>: "

        if isinstance(target, tuple):
            # One stick direction per step. Name the stick and the direction;
            # the axis letter is ours, not the player's.
            _kind, name, stage = target
            directions = _axis_directions(name) or ("", "")
            direction = directions[0 if stage == "negative" else 1]
            self._wizard_label.setText(
                prefix + f"push <b>{_stick_name(name)}</b> — <b>{direction}</b>"
            )
            return

        if isinstance(target, str):
            what = _axis_label(target, self._is_keyboard, layout)
        else:
            what = dict(layout.bindable()).get(target, button_label(target))

        self._wizard_label.setText(prefix + f"press <b>{what}</b>")

    def _wizard_skip(self) -> None:
        """Move on, clearing whatever this control had.

        Skipping means "this pad has no such control". Leaving the old binding
        in place would keep a value the starting guess invented, which is the
        very thing the player is skipping past -- and Esc, the shortcut for
        this, has always cleared.
        """
        if self._wizard is None:
            return

        _layout_key, target = self._wizard[self._wizard_index]
        if isinstance(target, tuple):
            # Skipping the first direction clears the axis; skipping the
            # second keeps what the first bound -- one good push is a working
            # binding, and the second step only existed to verify it.
            if target[2] == "negative":
                self._mapping.bind_axis(target[1], None)
                self._mapping.key_axes.pop(target[1], None)
        elif isinstance(target, str):
            self._mapping.bind_axis(target, None)
            self._mapping.key_axes.pop(target, None)
        else:
            self._mapping.bind_button(target, None)

        self._wizard_skipped += 1
        self._wizard_index += 1
        self._cancel_capture()
        self._refresh_rows()
        self._apply_live()
        self._wizard_step()

    def _wizard_back(self) -> None:
        if self._wizard is None:
            return
        self._wizard_index = max(0, self._wizard_index - 1)
        self._cancel_capture()
        self._wizard_step()

    def _wizard_next(self) -> None:
        """Called once a binding lands, to move on."""
        if self._wizard is None:
            return
        self._wizard_index += 1
        self._wizard_step()

    def _stop_wizard(self) -> None:
        self._end_wizard()
        self._status.setText(
            "Guided binding stopped. Anything already bound has been kept."
        )

    def _finish_wizard(self) -> None:
        total = len(self._wizard or ())
        skipped = self._wizard_skipped
        self._end_wizard()

        done = total - skipped
        message = f"Guided binding finished — {done} of {total} bound."
        if skipped:
            message += (
                f" {skipped} skipped; bind those individually if you need them."
            )
        message += " Review the list, then Save."
        self._status.setText(message)

    def _end_wizard(self) -> None:
        self._wizard = None
        self._cancel_capture()
        self._wizard_row.setVisible(False)
        self._wizard_start_row.setVisible(True)
        self._set_manual_controls_enabled(True)

    def _set_manual_controls_enabled(self, enabled: bool) -> None:
        """Individual Bind buttons and the type selector fight the wizard.

        The wizard drives both -- it switches type between sections and owns
        what is being captured -- so leaving them live would let a click land
        the next press on a control nobody asked for.
        """
        self._layout_combo.setEnabled(enabled)
        for button in self._bind_buttons:
            button.setEnabled(enabled)

    def _build_binding_list(self) -> QWidget:
        self._binding_area = QScrollArea()
        self._binding_area.setWidgetResizable(True)
        self._populate_bindings()
        return self._binding_area

    def _populate_bindings(self) -> None:
        """Build the binding rows for the current controller type.

        The button set is a property of the *type*: an NES pad has A, B, Select,
        Start and a D-pad, so that is all it offers. Listing bumpers and stick
        clicks for a controller with none of them was noise, and implied
        bindings that could never be pressed.
        """
        layout = get_layout(self._configuration.layout)

        self._rows = {}
        self._bind_buttons = []

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setColumnStretch(1, 1)

        row = 0
        grid.addWidget(_section(f"{layout.name} buttons"), row, 0, 1, 3)
        row += 1

        for bit, label in layout.bindable():
            # An *analog* trigger is a single physical control that used to
            # appear twice -- once here and again under "Sticks and triggers".
            # It belongs with the axes, where it can carry travel. A digital
            # one (the N64's Z) has no travel and stays here, as a button.
            if bit in _TRIGGER_BIT.values() and layout.has_axis(_TRIGGER_AXIS[bit]):
                continue

            grid.addWidget(QLabel(label), row, 0)

            value = QLabel()
            value.setTextFormat(Qt.TextFormat.PlainText)
            self._rows[bit] = value
            grid.addWidget(value, row, 1)

            bind = QPushButton("Bind")
            bind.clicked.connect(lambda _=False, b=bit: self._start_capture(b))
            # Focus must not stay on a Bind button: Qt activates a focused
            # button on Space and Enter, so binding Space would re-arm the
            # button instead of recording the key.
            bind.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            grid.addWidget(bind, row, 2)
            self._bind_buttons.append(bind)

            add = QPushButton("+")
            add.setToolTip(
                "Bind a second control to this button, so either one works."
            )
            add.setFixedWidth(28)
            add.setProperty("compact", True)
            add.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            add.clicked.connect(
                lambda _=False, b=bit: self._start_capture(b, alt=True)
            )
            grid.addWidget(add, row, 3)
            self._bind_buttons.append(add)

            clear = QPushButton("×")
            clear.setToolTip("Clear this binding.")
            clear.setFixedWidth(28)
            clear.setProperty("compact", True)
            clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            clear.clicked.connect(lambda _=False, b=bit: self._clear_binding(b))
            grid.addWidget(clear, row, 4)
            self._bind_buttons.append(clear)
            row += 1

        # Sticks and triggers. On a keyboard these are key pairs; on a gamepad
        # they are whole analog axes, so the two are captured differently.
        # Only offered for types that actually have them.
        axes = [n for n in STICK_AXES + TRIGGER_AXES if layout.has_axis(n)]
        if axes:
            grid.addWidget(_section("Sticks and triggers"), row, 0, 1, 3)
            row += 1

        for name in axes:
            grid.addWidget(
                QLabel(_axis_label(name, self._is_keyboard, layout)), row, 0
            )

            value = QLabel()
            self._rows[_axis_key(name)] = value
            grid.addWidget(value, row, 1)

            bind = QPushButton("Bind")
            bind.clicked.connect(lambda _=False, n=name: self._start_axis_capture(n))
            bind.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            grid.addWidget(bind, row, 2)
            self._bind_buttons.append(bind)

            clear = QPushButton("×")
            clear.setToolTip("Clear this binding.")
            clear.setFixedWidth(28)
            clear.setProperty("compact", True)
            clear.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            clear.clicked.connect(lambda _=False, n=name: self._clear_axis(n))
            # Column 4, leaving 3 empty: an axis has no second binding, and
            # lining the clears up matters more than closing the gap.
            grid.addWidget(clear, row, 4)
            self._bind_buttons.append(clear)
            row += 1

        grid.setRowStretch(row, 1)
        self._binding_area.setWidget(inner)

        # These are brand-new widgets, so they come back enabled. The wizard
        # switches type mid-run, which lands here -- without this the Bind
        # buttons quietly become live again halfway through.
        if self._wizard is not None:
            self._set_manual_controls_enabled(False)
        self._refresh_rows()

    # -- capture -----------------------------------------------------------

    def _arm_capture(self) -> None:
        """Take over the keyboard for the duration of a binding.

        Qt activates a focused button on Space and Enter, and a dialog's default
        button answers Enter from anywhere. Without this, binding Space re-armed
        the Bind button and binding Enter dismissed the dialog -- which made
        keyboard binding almost unusable. While capture is armed the only thing
        a keypress can do is complete the binding.
        """
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        for button in self._bind_buttons:
            button.setEnabled(False)
        self._set_default_button(False)

    def _disarm_capture(self) -> None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

        for button in self._bind_buttons:
            button.setEnabled(True)
        self._set_default_button(True)

    def _set_default_button(self, enabled: bool) -> None:
        box = getattr(self, "_button_box", None)
        if box is None:
            return
        save = box.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setDefault(enabled)
            save.setAutoDefault(enabled)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt override
        """Consume every key while a binding is being captured."""
        from PySide6.QtCore import QEvent

        if self._capturing is None and not self._axis_capture:
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.KeyPress:
            key = int(event.key())
            if key == Qt.Key.Key_Escape:
                if self._wizard is not None:
                    # Mid-walk-through, Esc means "I do not have this control"
                    # rather than "stop". Skip already clears it.
                    self._wizard_skip()
                elif self._capturing is not None:
                    self._mapping.bind_button(self._capturing, None)
                    self._finish_capture()
                else:
                    self._cancel_capture()
                return True
            if self._is_keyboard and not event.isAutoRepeat():
                self._feed_key(key, True)
            return True

        if event.type() == QEvent.Type.KeyRelease:
            if self._is_keyboard and not event.isAutoRepeat():
                self._feed_key(int(event.key()), False)
            return True

        return super().eventFilter(obj, event)

    def _start_capture(self, button: int, *, alt: bool = False) -> None:
        self._capturing = button
        self._capturing_alt = alt
        self._axis_capture = None
        self._baseline = None if self._is_keyboard else self._snapshot()
        self._baseline_keys = self._pressed_keys()
        # In the walk-through the target is drawn as pressed, so the picture
        # shows what is being set rather than what is being pushed.
        self._update_preview_highlight()
        self._arm_capture()
        self._status.setText(
            "Press the control to *add* as a second binding, or Esc to cancel."
            if alt
            else "Press the control to use, or Esc to clear this binding."
        )

    def _start_axis_capture(self, name: str, stage: str = "negative") -> None:
        """Listen for one axis. ``stage`` picks which direction is wanted.

        The wizard asks for each direction as its own step, so it starts the
        positive half directly. ``_axis_pending`` is then recovered from the
        binding the negative step made, letting the same verification run --
        and if that step was skipped, the positive half stands alone and infers
        the wiring from its own push.
        """
        self._capturing = None
        self._axis_capture = name
        self._baseline_keys = self._pressed_keys()
        self._axis_stage = stage
        self._axis_pending = None
        self._axis_rearmed = True
        self._baseline = None if self._is_keyboard else self._snapshot()

        if stage == "positive":
            binding = self._mapping.axes.get(name)
            if binding is not None:
                # Negative reads negative on a normal axis; positive if the
                # binding was recorded inverted.
                self._axis_pending = (
                    binding.index, 1 if binding.invert else -1
                )
                # The stick may still be deflected from the previous step's
                # push; insist it comes back to centre first, exactly as the
                # one-step flow did between halves.
                self._axis_rearmed = False
        # Ring the stick or trigger this axis belongs to. Highlighting nothing
        # left the player reading the status line to work out which stick was
        # being asked for, with the picture sitting right there.
        self._update_preview_highlight()
        self._arm_capture()
        self._prompt_for_axis()

    def _prompt_for_axis(self) -> None:
        """Ask for the current half of the axis being captured."""
        name = self._axis_capture
        if not name:
            return

        directions = _axis_directions(name)

        if directions is None:
            label = _axis_label(
                name, self._is_keyboard, get_layout(self._configuration.layout)
            )
            verb = "press the key for" if self._is_keyboard else "press"
            self._status.setText(f"{label}: {verb} the trigger.")
        else:
            which = directions[0 if self._axis_stage == "negative" else 1]
            verb = "press the key for" if self._is_keyboard else "push"
            self._status.setText(
                f"{_stick_name(name)}: {verb} <b>{which}</b>."
                + ("" if self._is_keyboard else " Then let it return to centre.")
            )

        # The stage moves on mid-capture in the manual two-half flow, so the
        # arrow has to follow it, not just the text.
        self._update_preview_highlight()
        self._refresh_wizard_label()

    def _axis_direction_word(self) -> str:
        """"Left" / "Right" / "Up" / "Down", or "" for a trigger."""
        directions = _axis_directions(self._axis_capture or "")
        if directions is None:
            return ""
        return directions[0 if self._axis_stage == "negative" else 1]

    def _cancel_capture(self) -> None:
        self._capturing = None
        self._capturing_alt = False
        self._axis_capture = None
        self._input_hold = None
        self._disarm_capture()
        self._preview.set_highlight(0)
        self._status.setText("Press Bind, then the control you want to use.")

    # -- polling -----------------------------------------------------------

    def _tick(self) -> None:
        self._backend.pump()

        if self._backend.poll(self._device.instance_id, self._state):
            # During the walk-through the picture shows *the control being
            # set*, not what the player's press currently drives. Mid-rebind
            # those are different controls, and lighting the old one tells the
            # player their press went somewhere it did not.
            self._preview.set_state(
                self._neutral if self._wizard is not None else self._state
            )

        if self._capturing is not None:
            self._try_capture_button()
        elif self._axis_capture:
            self._try_capture_axis()

    def _snapshot(self) -> dict | None:
        getter = getattr(self._backend, "raw_snapshot", None)
        return getter(self._device.instance_id) if getter else None

    def _pressed_keys(self) -> frozenset[int]:
        for backend in getattr(self._backend, "backends", [self._backend]):
            keys = getattr(backend, "pressed_keys", None)
            if keys is not None:
                return keys
        return frozenset()

    def _try_capture_button(self) -> None:
        button = self._capturing
        if button is None:
            return

        if self._is_keyboard:
            new = self._pressed_keys() - self._baseline_keys
            if new:
                self._bind_captured(
                    button, InputSource(SourceKind.KEY, next(iter(new)))
                )
            return

        source = self._first_changed_control()
        if source is None:
            self._release_hold()
            return

        # Held to confirm, exactly like an axis: a button clipped while
        # reaching past it looks identical to a deliberate press for one
        # frame, and only persistence tells them apart.
        if not self._hold_input(source):
            return

        self._bind_captured(button, source)

    def _first_changed_control(self) -> InputSource | None:
        """The first physical control that moved since capture began."""
        now = self._snapshot()
        if now is None or self._baseline is None:
            return None

        for index, pressed in enumerate(now.get("buttons", [])):
            was = (
                self._baseline["buttons"][index]
                if index < len(self._baseline["buttons"])
                else False
            )
            if pressed and not was:
                return InputSource(SourceKind.BUTTON, index)

        for index, value in enumerate(now.get("hats", [])):
            was = (
                self._baseline["hats"][index]
                if index < len(self._baseline["hats"])
                else 0
            )
            changed = value & ~was
            if changed:
                # Take a single direction, so a diagonal binds one axis of it.
                for mask in (0x01, 0x02, 0x04, 0x08):
                    if changed & mask:
                        return InputSource(SourceKind.HAT, index, mask)

        for index, value in enumerate(now.get("axes", [])):
            was = (
                self._baseline["axes"][index]
                if index < len(self._baseline["axes"])
                else 0
            )
            if (
                abs(value - was) > _AXIS_CAPTURE_DELTA
                and abs(value) > AXIS_PRESS_THRESHOLD
            ):
                return InputSource(SourceKind.AXIS, index, 1 if value > 0 else -1)

        return None

    def _try_capture_axis(self) -> None:
        name = self._axis_capture

        if self._is_keyboard:
            new = self._pressed_keys() - self._baseline_keys
            if not new:
                return
            key = next(iter(new))
            existing = self._mapping.key_axes.get(name, KeyAxisBinding())

            if _axis_directions(name) is None:
                # A trigger has one direction, so one key is the whole binding.
                self._mapping.key_axes[name] = KeyAxisBinding(
                    negative=existing.negative, positive=key
                )
                self._finish_capture()
                return

            if self._axis_stage == "negative":
                self._mapping.key_axes[name] = KeyAxisBinding(
                    negative=key, positive=existing.positive
                )
                if self._wizard is not None:
                    # Each direction is its own wizard step.
                    self._finish_capture()
                    return
                self._baseline_keys = self._pressed_keys()
                self._axis_stage = "positive"
                self._prompt_for_axis()
                self._refresh_rows()
                return

            # .get, not [name]: with per-direction steps the negative half may
            # have been skipped, and there is then no entry to read back.
            current = self._mapping.key_axes.get(name, KeyAxisBinding())
            self._mapping.key_axes[name] = KeyAxisBinding(
                negative=current.negative, positive=key
            )
            self._finish_capture()
            return

        now = self._snapshot()
        if now is None:
            return

        if _axis_directions(name) is None:
            self._capture_trigger(name, now)
            return

        if self._baseline is None:
            return
        self._capture_stick_half(name, now)

    def _capture_trigger(self, name: str, now: dict) -> None:
        """A trigger is one direction, and may not be analog at all.

        Unlike a stick this stays baseline-relative, because a raw joystick may
        rest a trigger at full negative rather than at zero -- so "is it
        pulled?" cannot be read from the value alone.
        """
        moved = self._changed_axis(now, require_deflection=True)
        if moved is not None:
            index, value, was = moved
            if not self._hold_axis(index, value - was):
                return
            self._mapping.bind_axis(name, AxisBinding(index, invert=value < was))
            # Drop any digital binding: with analog travel the bit is derived
            # from the value, and two sources for one control conflict.
            self._mapping.buttons.pop(_TRIGGER_BIT[name], None)
            self._finish_capture()
            return

        # No analog travel. A retro pad's Z is a plain button, so bind the bit
        # instead -- CompiledMapping flags the missing axis and the poll path
        # synthesizes a full-scale value so apply_trigger_buttons keeps it.
        source = self._first_changed_control()
        if source is None or source.kind is SourceKind.AXIS:
            self._release_hold()
            return

        # Held to confirm like every other control. This path was instant,
        # which is why a digital trigger -- the common case on a retro pad --
        # bound the moment it was brushed.
        if not self._hold_input(source):
            return

        self._mapping.bind_axis(name, None)
        self._mapping.buttons[_TRIGGER_BIT[name]] = source
        self._finish_capture()

    def _capture_stick_half(self, name: str, now: dict) -> None:
        """Ask for each direction separately, and learn the wiring from it.

        Two halves rather than one push, because one push cannot distinguish a
        stick that is wired backwards from a player who pushed the other way --
        and because "push Left" is something a person can act on, where "move
        the axis you want" is not.
        """
        # Between halves the stick must come back to centre. Releasing a fully
        # deflected stick is itself a large movement, and would otherwise read
        # as the opposite direction being pushed.
        if not self._axis_rearmed:
            if self._axis_pending is None:
                self._axis_rearmed = True
            else:
                index = self._axis_pending[0]
                axes = now.get("axes", [])
                if index < len(axes) and abs(axes[index]) < _AXIS_REARM_LEVEL:
                    self._axis_rearmed = True
                    self._baseline = now
            return

        moved = self._deflected_axis(now)
        if moved is None:
            self._release_hold()
            return
        index, value = moved

        if not self._hold_axis(index, value):
            return

        if self._axis_stage == "negative":
            # Our convention is negative = left / up. If pushing that way read
            # positive, the axis is wired backwards; one decisive push settles
            # both the index and the wiring.
            if self._wizard is not None:
                # Each direction is its own wizard step: bind from this half
                # and advance. The positive step verifies against it.
                self._mapping.bind_axis(name, AxisBinding(index, invert=value > 0))
                self._finish_capture()
                return

            self._axis_pending = (index, 1 if value > 0 else -1)
            self._axis_stage = "positive"
            self._axis_rearmed = False
            self._prompt_for_axis()
            return

        if self._axis_pending is None:
            # The negative half was skipped, so this push stands alone:
            # positive = right / down, and reading negative means inverted.
            self._mapping.bind_axis(name, AxisBinding(index, invert=value < 0))
            self._finish_capture()
            return

        pending_index, pending_sign = self._axis_pending
        if index != pending_index:
            # Two different axes for one stick direction pair means one of the
            # two pushes was misread; binding either would not work.
            if self._wizard is not None:
                self._status.setText(
                    "That moved a different axis than the first push — "
                    "asking for both directions again."
                )
                self._wizard_back()
                return

            self._axis_stage = "negative"
            self._axis_pending = None
            self._axis_rearmed = True
            self._baseline = now
            self._prompt_for_axis()
            self._status.setText(
                self._status.text() + "  (That moved a different axis — starting again.)"
            )
            return

        self._mapping.bind_axis(name, AxisBinding(index, invert=pending_sign > 0))
        self._finish_capture()

    def _hold_input(self, identity: object) -> bool:
        """Track a sustained input. True once it has been held long enough.

        ``identity`` is whatever distinguishes the input -- (axis, sign) for a
        deflection, the InputSource for a button or hat. Restarting whenever it
        changes means a control swept through on the way somewhere else never
        accumulates enough time to be taken.
        """
        now = time.monotonic()

        if self._input_hold is None or self._input_hold[0] != identity:
            self._input_hold = (identity, now)

        elapsed = now - self._input_hold[1]
        if elapsed >= _HOLD_TO_BIND_S:
            self._release_hold()
            return True

        self._show_hold_progress(elapsed / _HOLD_TO_BIND_S)
        return False

    def _hold_axis(self, index: int, value: int) -> bool:
        return self._hold_input((index, 1 if value > 0 else -1))

    def _clear_binding(self, button: int) -> None:
        """Unbind a logical button, both sources."""
        self._cancel_capture()
        self._mapping.bind_button(button, None)
        self._refresh_rows()
        self._apply_live()
        self._status.setText(f"Cleared {button_label(button)}.")

    def _clear_axis(self, name: str) -> None:
        self._cancel_capture()
        self._mapping.bind_axis(name, None)
        self._mapping.key_axes.pop(name, None)
        # A trigger with no analog travel is bound as a button, so clearing the
        # row has to take that with it or the control keeps firing.
        if name in _TRIGGER_BIT:
            self._mapping.buttons.pop(_TRIGGER_BIT[name], None)
            self._mapping.buttons_alt.pop(_TRIGGER_BIT[name], None)
        self._refresh_rows()
        self._apply_live()
        self._status.setText(
            f"Cleared {_axis_label(name, self._is_keyboard, get_layout(self._configuration.layout))}."
        )

    def _bind_captured(self, button: int, source: InputSource) -> None:
        """Store a captured control as the primary or the second source."""
        if self._capturing_alt:
            self._mapping.bind_button_alt(button, source)
        else:
            self._mapping.bind_button(button, source)
        self._finish_capture()

    def _release_hold(self) -> None:
        if self._input_hold is not None:
            self._input_hold = None
            self._show_hold_progress(0.0)

    def _show_hold_progress(self, fraction: float) -> None:
        self._update_preview_highlight(fraction)

    def _update_preview_highlight(self, progress: float = 0.0) -> None:
        """Point the preview at whatever is being captured.

        One place, because the highlight has three parts that must agree --
        which control, whether to draw it pressed, and which way to push it --
        and updating progress from a separate call used to reset the others.
        """
        if self._capturing is not None:
            bit, direction = self._capturing, None
        else:
            name = self._axis_capture or ""
            bit = _axis_highlight_bit(name)
            direction = _axis_direction_vector(name, self._axis_stage)

        self._preview.set_highlight(
            bit,
            lit=self._wizard is not None,
            progress=progress,
            direction=direction,
        )

    def _deflected_axis(self, now: dict) -> tuple[int, int] | None:
        """The first axis pushed near its extent, as (index, value).

        Absolute, not measured against a baseline. A stick self-centres, so
        "is it pushed?" is answerable from the reading alone -- and going via a
        baseline made it possible to get stuck: if the player was already
        holding the stick when the settle delay ended, the deflection became
        the resting state and no further push could register.

        Triggers still go through the baseline, because a raw joystick may rest
        one at full negative rather than at zero.
        """
        for index, value in enumerate(now.get("axes", [])):
            if abs(value) > _AXIS_CAPTURE_DELTA:
                return index, value
        return None

    def _changed_axis(
        self, now: dict, *, require_deflection: bool = False
    ) -> tuple[int, int, int] | None:
        """The first axis that has moved far enough, as (index, value, was).

        ``require_deflection`` also insists the axis has ended up *away from
        centre*, not merely moved. A stick released from full travel produces a
        large change while landing on nothing, and without this a trigger
        prompt read that release as a pull -- walking the N64 wizard bound Z to
        the left stick's Y axis, because the stick was still deflected from the
        previous step. A trigger that rests at full negative still works: at
        rest it has no change from the baseline, and pulling it satisfies both
        conditions at once.
        """
        if self._baseline is None:
            return None
        base = self._baseline.get("axes", [])
        for index, value in enumerate(now.get("axes", [])):
            was = base[index] if index < len(base) else 0
            if abs(value - was) <= _AXIS_CAPTURE_DELTA:
                continue
            if require_deflection and abs(value) <= AXIS_PRESS_THRESHOLD:
                continue
            return index, value, was
        return None

    def _finish_capture(self) -> None:
        self._cancel_capture()
        self._refresh_rows()
        self._apply_live()
        # A binding landing is what advances the walk-through: the player never
        # has to reach for the mouse between controls.
        self._wizard_next()

    def _apply_live(self) -> None:
        """Push the mapping to the backend so the preview reflects it at once."""
        setter = getattr(self._backend, "set_mapping", None)
        if setter is not None:
            setter(self._device.guid, self._mapping)

    # -- rendering ---------------------------------------------------------

    def _refresh_rows(self) -> None:
        for key, label in self._rows.items():
            if isinstance(key, str):
                continue
            sources = self._mapping.sources_for(key)
            if sources:
                text = " + ".join(
                    _describe_source(s, self._is_keyboard) for s in sources
                )
            else:
                text = "—"
            label.setText(text)
            label.setEnabled(bool(sources))

        for name in STICK_AXES + TRIGGER_AXES:
            label = self._rows.get(_axis_key(name))
            if label is None:
                continue
            if self._is_keyboard:
                binding = self._mapping.key_axes.get(name)
                text = (
                    f"{_key_name(binding.negative)} / {_key_name(binding.positive)}"
                    if binding else "—"
                )
            else:
                binding = self._mapping.axes.get(name)
                if binding is not None:
                    text = binding.describe()
                else:
                    # A trigger with no analog travel is bound as a button.
                    source = self._mapping.buttons.get(_TRIGGER_BIT.get(name, 0))
                    text = source.describe() if source else "—"
            label.setText(text)
            label.setEnabled(text != "—")

    # -- events ------------------------------------------------------------

    def _feed_key(self, key: int, down: bool) -> None:
        for backend in getattr(self._backend, "backends", [self._backend]):
            setter = getattr(backend, "set_key", None)
            if setter is not None:
                setter(key, down)

    def _on_layout_changed(self) -> None:
        key = self._layout_combo.currentData()
        if not key:
            return

        self._cancel_capture()

        # Each type keeps its own bindings, so switching type switches mapping.
        self._configuration.layout = key
        self._mapping = self._configuration.mapping_for(key)

        self._preview.set_layout_key(key)
        self._note.setText(self._preview.note)
        self._populate_bindings()
        self._apply_live()

    def _on_reset(self) -> None:
        self._mapping = self._starting_mapping()
        self._finish_capture()

    def _on_clear(self) -> None:
        self._mapping = DeviceMapping(guid=self._device.guid, name=self._device.name)
        self._finish_capture()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._timer.stop()
        super().closeEvent(event)

    # -- result ------------------------------------------------------------

    def _refresh_identity(self) -> None:
        """Show which configuration is being edited, and what may be done to it.

        A built-in has no Save: it is regenerated from a rule on every launch,
        so writing to it would be undone at the next start and would take the
        player's edits with it. "Save as..." is the only way out, and disabling
        Save with a reason is clearer than hiding it.
        """
        builtin = getattr(self._configuration, "builtin", False)
        self._name_label.setText(
            f"<b>{self._configuration.name}</b>"
            + (" <i>(built-in)</i>" if builtin else "")
        )

        self._save_button.setEnabled(not builtin)
        self._save_button.setToolTip(
            "Built-in configurations cannot be overwritten — use Save as… to "
            "keep your changes under a new name."
            if builtin
            else "Update this configuration."
        )
        self._save_as_button.setToolTip("Save these bindings as a new configuration.")

    def _commit(self) -> None:
        """Write the editor's state back into the configuration being edited."""
        self._configuration.layout = self._layout_combo.currentData()
        self._configuration.mapping = self._mapping
        self._configuration.device_guid = self._device.guid
        self._configuration.device_name = self._device.name

    def _on_save(self) -> None:
        if getattr(self._configuration, "builtin", False):
            # Reachable via the dialog's default button even while Save is
            # disabled, so refuse here too rather than silently corrupting it.
            self._status.setText(
                "This is a built-in configuration — use Save as… to keep your changes."
            )
            return

        self._commit()
        if self._store is not None:
            self._store.upsert(self._configuration)
        self.accept()

    def _on_save_as(self) -> None:
        """Copy under a new name and carry on editing the copy."""
        name = self._ask_for_name()
        if name is None:
            return

        self._commit()
        copy = self._configuration.copy_as(name)

        if self._store is not None:
            self._store.upsert(copy)

        # Continue in the copy: the mappings were deep-copied, so the editor
        # has to point at the new objects or further edits would land on the
        # configuration we just left.
        self._configuration = copy
        self._mapping = copy.mapping_for(self._layout_combo.currentData())
        self._saved_as = True

        self._refresh_identity()
        self._populate_bindings()
        self._apply_live()
        self._status.setText(f"Now editing '{name}'. Save to keep further changes.")

    def _ask_for_name(self) -> str | None:
        """Prompt for a name, refusing ones that would destroy something."""
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        suggestion = self._configuration.name
        if self._store is not None:
            suggestion = self._store.unique_name(suggestion)

        while True:
            name, ok = QInputDialog.getText(
                self, "Save configuration as", "Name", text=suggestion
            )
            if not ok:
                return None

            name = name.strip()
            if not name:
                continue

            if self._store is None:
                return name

            existing = self._store.get(name)
            if existing is None:
                return name

            if getattr(existing, "builtin", False):
                QMessageBox.warning(
                    self,
                    "Name in use",
                    f"'{name}' is a built-in configuration and cannot be "
                    f"replaced. Choose a different name.",
                )
                suggestion = self._store.unique_name(name)
                continue

            answer = QMessageBox.question(
                self,
                "Replace configuration?",
                f"'{name}' already exists. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                return name
            suggestion = self._store.unique_name(name)

    @property
    def configuration(self):
        return self._configuration

    @property
    def created_copy(self) -> bool:
        """Whether "Save as..." stored a new configuration during this session."""
        return self._saved_as

    @property
    def mapping(self) -> DeviceMapping:
        return self._mapping

    @property
    def preview_layout(self) -> str:
        return self._layout_combo.currentData()


def _trim_to_layout(mapping: DeviceMapping, layout_key: str) -> DeviceMapping:
    """Drop bindings for controls the target system does not have.

    A binding the list does not show is not inert: it still reaches the console.
    An SNES configuration reporting stick clicks and bumpers is wrong about
    itself, and the player has no way to see why.
    """
    layout = get_layout(layout_key)
    allowed = {bit for bit, _label in layout.bindable()}

    mapping.buttons = {
        bit: source for bit, source in mapping.buttons.items() if bit in allowed
    }
    mapping.axes = {
        name: binding
        for name, binding in mapping.axes.items()
        if layout.has_axis(name)
    }
    mapping.key_axes = {
        name: binding
        for name, binding in mapping.key_axes.items()
        if layout.has_axis(name)
    }
    return mapping


#: Which control on the artwork an axis belongs to, so binding it can ring the
#: right thing. Sticks and triggers are drawn as single elements, and both of a
#: stick's axes live in the same one.
_AXIS_ELEMENT_BIT: dict[str, int] = {
    "left_x": Button.LEFT_STICK,
    "left_y": Button.LEFT_STICK,
    "right_x": Button.RIGHT_STICK,
    "right_y": Button.RIGHT_STICK,
    "left_trigger": Button.LEFT_TRIGGER,
    "right_trigger": Button.RIGHT_TRIGGER,
}


def _axis_highlight_bit(name: str) -> int:
    return _AXIS_ELEMENT_BIT.get(name, 0)


#: Which way to push, as a unit (dx, dy) in screen terms -- y grows downward,
#: so "up" is -1. None for a trigger, which has no direction to point at.
_AXIS_VECTORS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "left_x": ((-1, 0), (1, 0)),
    "right_x": ((-1, 0), (1, 0)),
    "left_y": ((0, -1), (0, 1)),
    "right_y": ((0, -1), (0, 1)),
}


def _axis_direction_vector(name: str, stage: str) -> tuple[int, int] | None:
    pair = _AXIS_VECTORS.get(name)
    if pair is None:
        return None
    return pair[0 if stage == "negative" else 1]


def _is_keyboard(device) -> bool:
    from client.input.keyboard_backend import KEYBOARD_GUID

    return device.guid == KEYBOARD_GUID


def _section(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont(label.font())
    font.setBold(True)
    label.setFont(font)
    return label


def _axis_key(name: str) -> str:
    return f"axis:{name}"


#: The two halves of a stick axis, named as a player experiences them.
#:
#: Nobody pushes a stick "in the positive X direction" -- they push it right.
#: Each half is asked for separately, which is also how the inversion is
#: learned: if pushing *left* reads positive, the axis is wired backwards.
_AXIS_DIRECTIONS: dict[str, tuple[str, str]] = {
    "left_x": ("Left", "Right"),
    "right_x": ("Left", "Right"),
    "left_y": ("Up", "Down"),
    "right_y": ("Up", "Down"),
}

#: Which logical bit each trigger axis drives, for labelling and for digital
#: triggers that have no analog travel to bind.
_TRIGGER_BIT: dict[str, int] = {
    "left_trigger": Button.LEFT_TRIGGER,
    "right_trigger": Button.RIGHT_TRIGGER,
}

#: The reverse, for asking "does this layout expose that trigger as an axis?"
_TRIGGER_AXIS: dict[int, str] = {bit: name for name, bit in _TRIGGER_BIT.items()}


def _axis_directions(name: str) -> tuple[str, str] | None:
    """The two prompts for a stick axis, or None for a trigger."""
    return _AXIS_DIRECTIONS.get(name)


def _stick_name(name: str) -> str:
    """"Left analog stick" / "Right analog stick" -- never the axis letter.

    A prompt names the stick and the direction: "Left stick X, push Down" asks
    the player to translate an axis letter into a direction they can act on,
    and reads as though X and Y were separate controls.
    """
    return "Right analog stick" if name.startswith("right") else "Left analog stick"


def _axis_label(name: str, keyboard: bool, layout=None) -> str:
    pretty = {
        "left_x": "Left stick X",
        "left_y": "Left stick Y",
        "right_x": "Right stick X",
        "right_y": "Right stick Y",
        "left_trigger": "Left trigger",
        "right_trigger": "Right trigger",
    }[name]

    # A trigger is one control, so it appears once -- here, not also in the
    # button list. Use the name this system prints on it: Z on an N64, ZL on a
    # Switch, L2 on a PlayStation.
    if layout is not None and name in _TRIGGER_BIT:
        own = dict(layout.bindable()).get(_TRIGGER_BIT[name])
        if own:
            pretty = own

    return f"{pretty} (2 keys)" if keyboard else pretty


def _describe_source(source: InputSource | None, keyboard: bool) -> str:
    if source is None:
        return "—"
    if source.kind is SourceKind.KEY:
        return _key_name(source.index)
    return source.describe()


def _key_name(code: int) -> str:
    """Human name for a Qt key code."""
    if not code:
        return "—"
    from PySide6.QtGui import QKeySequence

    text = QKeySequence(code).toString()
    return text or f"Key {code}"
