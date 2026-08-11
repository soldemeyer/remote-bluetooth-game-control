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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from client.gui.controller_layouts import DEFAULT_LAYOUT, LAYOUTS
from client.gui.controller_preview import ControllerPreview
from client.input.mapping import (
    AXIS_PRESS_THRESHOLD,
    BINDABLE_BUTTONS,
    STICK_AXES,
    TRIGGER_AXES,
    AxisBinding,
    DeviceMapping,
    InputSource,
    KeyAxisBinding,
    SourceKind,
    default_joystick_mapping,
)
from common.state import ControllerState

log = logging.getLogger(__name__)

#: Preview refresh. Fast enough to feel instant, far below the input loop rate.
_TICK_MS = 16

#: How far a joystick axis must move from its baseline to count as "the player
#: moved this one". Generous, because a resting stick drifts.
_AXIS_CAPTURE_DELTA = 12000


class MappingDialog(QDialog):
    """Bind logical buttons to physical controls, with a live preview."""

    def __init__(
        self,
        backend,
        device,
        mapping: DeviceMapping | None,
        parent: QWidget | None = None,
        *,
        preview_layout: str = DEFAULT_LAYOUT,
    ) -> None:
        super().__init__(parent)
        self._backend = backend
        self._device = device
        self._is_keyboard = _is_keyboard(device)

        self._mapping = mapping or self._starting_mapping()

        #: Logical button currently being captured, or None.
        self._capturing: int | None = None
        #: Axis name currently being captured, or None.
        self._axis_capture: str | None = None
        #: Which half of a keyboard axis pair we are asking for.
        self._key_axis_stage = "negative"
        #: What every control read when capture began.
        self._baseline: dict | None = None
        self._baseline_keys: frozenset[int] = frozenset()

        self._state = ControllerState()
        self._rows: dict[int, QLabel] = {}

        self.setWindowTitle(f"Configure — {device.display_name()}")
        self.setMinimumSize(880, 620)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        self._build_ui(preview_layout)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(_TICK_MS)

        # Keys must reach us even when a button inside the dialog has focus.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # -- construction ------------------------------------------------------

    def _starting_mapping(self) -> DeviceMapping:
        if self._is_keyboard:
            from client.input.keyboard_backend import default_keyboard_mapping

            return default_keyboard_mapping()

        return default_joystick_mapping(
            self._device.guid,
            self._device.name,
            axes=self._device.axis_count,
            buttons=self._device.button_count,
            hats=self._device.hat_count,
        )

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
        chooser.addWidget(QLabel("Preview as"))
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

        right.addWidget(self._build_binding_list(), 1)

        buttons = QDialogButtonBox()
        reset = buttons.addButton("Reset to defaults", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(self._on_reset)
        clear = buttons.addButton("Clear all", QDialogButtonBox.ButtonRole.ActionRole)
        clear.clicked.connect(self._on_clear)
        buttons.addButton(QDialogButtonBox.StandardButton.Save)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

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

    def _build_binding_list(self) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setColumnStretch(1, 1)

        row = 0
        grid.addWidget(_section("Buttons"), row, 0, 1, 3)
        row += 1

        for bit, label in BINDABLE_BUTTONS:
            grid.addWidget(QLabel(label), row, 0)

            value = QLabel()
            value.setTextFormat(Qt.TextFormat.PlainText)
            self._rows[bit] = value
            grid.addWidget(value, row, 1)

            bind = QPushButton("Bind")
            bind.clicked.connect(lambda _=False, b=bit: self._start_capture(b))
            grid.addWidget(bind, row, 2)
            row += 1

        # Sticks and triggers. On a keyboard these are key pairs; on a gamepad
        # they are whole analog axes, so the two are captured differently.
        grid.addWidget(_section("Sticks and triggers"), row, 0, 1, 3)
        row += 1

        for name in STICK_AXES + TRIGGER_AXES:
            grid.addWidget(QLabel(_axis_label(name, self._is_keyboard)), row, 0)

            value = QLabel()
            self._rows[_axis_key(name)] = value
            grid.addWidget(value, row, 1)

            bind = QPushButton("Bind")
            bind.clicked.connect(lambda _=False, n=name: self._start_axis_capture(n))
            grid.addWidget(bind, row, 2)
            row += 1

        grid.setRowStretch(row, 1)
        area.setWidget(inner)
        self._refresh_rows()
        return area

    # -- capture -----------------------------------------------------------

    def _start_capture(self, button: int) -> None:
        self._capturing = button
        self._axis_capture = None
        self._baseline = None if self._is_keyboard else self._snapshot()
        self._baseline_keys = self._pressed_keys()
        self._preview.set_highlight(button)
        self._status.setText("Press the control to use, or Esc to clear this binding.")

    def _start_axis_capture(self, name: str) -> None:
        self._capturing = None
        self._axis_capture = name
        self._baseline = None if self._is_keyboard else self._snapshot()
        self._baseline_keys = self._pressed_keys()
        self._preview.set_highlight(0)

        if self._is_keyboard:
            self._key_axis_stage = "negative"
            self._status.setText(
                f"{_axis_label(name, True)}: press the key for the "
                f"<b>negative</b> direction (left / up)."
            )
        else:
            self._status.setText(
                f"{_axis_label(name, False)}: move the stick or trigger to use."
            )

    def _cancel_capture(self) -> None:
        self._capturing = None
        self._axis_capture = None
        self._preview.set_highlight(0)
        self._status.setText("Press Bind, then the control you want to use.")

    # -- polling -----------------------------------------------------------

    def _tick(self) -> None:
        self._backend.pump()

        if self._backend.poll(self._device.instance_id, self._state):
            self._preview.set_state(self._state)

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
                self._mapping.bind_button(
                    button, InputSource(SourceKind.KEY, next(iter(new)))
                )
                self._finish_capture()
            return

        source = self._first_changed_control()
        if source is not None:
            self._mapping.bind_button(button, source)
            self._finish_capture()

    def _first_changed_control(self) -> InputSource | None:
        """The first physical control that moved since capture began."""
        now = self._snapshot()
        if now is None or self._baseline is None:
            return None

        for index, pressed in enumerate(now.get("buttons", [])):
            was = self._baseline["buttons"][index] if index < len(self._baseline["buttons"]) else False
            if pressed and not was:
                return InputSource(SourceKind.BUTTON, index)

        for index, value in enumerate(now.get("hats", [])):
            was = self._baseline["hats"][index] if index < len(self._baseline["hats"]) else 0
            changed = value & ~was
            if changed:
                # Take a single direction, so a diagonal binds one axis of it.
                for mask in (0x01, 0x02, 0x04, 0x08):
                    if changed & mask:
                        return InputSource(SourceKind.HAT, index, mask)

        for index, value in enumerate(now.get("axes", [])):
            was = self._baseline["axes"][index] if index < len(self._baseline["axes"]) else 0
            if abs(value - was) > _AXIS_CAPTURE_DELTA and abs(value) > AXIS_PRESS_THRESHOLD:
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

            if self._key_axis_stage == "negative":
                self._mapping.key_axes[name] = KeyAxisBinding(
                    negative=key, positive=existing.positive
                )
                self._baseline_keys = self._pressed_keys()
                self._key_axis_stage = "positive"
                self._status.setText(
                    f"{_axis_label(name, True)}: now press the key for the "
                    f"<b>positive</b> direction (right / down)."
                )
                self._refresh_rows()
                return

            self._mapping.key_axes[name] = KeyAxisBinding(
                negative=self._mapping.key_axes[name].negative, positive=key
            )
            self._finish_capture()
            return

        now = self._snapshot()
        if now is None or self._baseline is None:
            return

        for index, value in enumerate(now.get("axes", [])):
            was = self._baseline["axes"][index] if index < len(self._baseline["axes"]) else 0
            if abs(value - was) > _AXIS_CAPTURE_DELTA:
                # An axis that reads strongly negative when pushed "positive"
                # is wired backwards; record that rather than making the player
                # discover their stick is inverted mid-game.
                self._mapping.bind_axis(name, AxisBinding(index, invert=value < was))
                self._finish_capture()
                return

    def _finish_capture(self) -> None:
        self._cancel_capture()
        self._refresh_rows()
        self._apply_live()

    def _apply_live(self) -> None:
        """Push the mapping to the backend so the preview reflects it at once."""
        setter = getattr(self._backend, "set_mapping", None)
        if setter is not None:
            setter(self._device.guid, self._mapping)

    # -- rendering ---------------------------------------------------------

    def _refresh_rows(self) -> None:
        for bit, _ in BINDABLE_BUTTONS:
            source = self._mapping.buttons.get(bit)
            label = self._rows.get(bit)
            if label is None:
                continue
            label.setText(_describe_source(source, self._is_keyboard))
            label.setEnabled(source is not None)

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
                text = binding.describe() if binding else "—"
            label.setText(text)
            label.setEnabled(text != "—")

    # -- events ------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        key = int(event.key())

        if key == Qt.Key.Key_Escape:
            if self._capturing is not None:
                # Escape during capture clears the binding rather than closing
                # the dialog -- losing a half-finished configuration to a
                # reflexive Esc would be infuriating.
                self._mapping.bind_button(self._capturing, None)
                self._finish_capture()
                return
            if self._axis_capture:
                self._cancel_capture()
                return

        if self._is_keyboard:
            self._feed_key(key, True)
            event.accept()
            return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._is_keyboard:
            self._feed_key(int(event.key()), False)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def _feed_key(self, key: int, down: bool) -> None:
        for backend in getattr(self._backend, "backends", [self._backend]):
            setter = getattr(backend, "set_key", None)
            if setter is not None:
                setter(key, down)

    def _on_layout_changed(self) -> None:
        self._preview.set_layout_key(self._layout_combo.currentData())
        self._note.setText(self._preview.note)

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

    @property
    def mapping(self) -> DeviceMapping:
        return self._mapping

    @property
    def preview_layout(self) -> str:
        return self._layout_combo.currentData()


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


def _axis_label(name: str, keyboard: bool) -> str:
    pretty = {
        "left_x": "Left stick X",
        "left_y": "Left stick Y",
        "right_x": "Right stick X",
        "right_y": "Right stick Y",
        "left_trigger": "Left trigger",
        "right_trigger": "Right trigger",
    }[name]
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
