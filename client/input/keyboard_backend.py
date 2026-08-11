"""Keyboard as a controller source.

Presents the keyboard as a single virtual gamepad so everything downstream --
the input loop, the transport, the server, the console -- treats it exactly like
any other pad. Nothing outside this module knows the difference.

**Focus is required, and that is deliberate.** This backend does not read the
keyboard itself; the GUI feeds it key events via :meth:`set_key`, which means it
only sees keys while the client window has focus. Reading the keyboard globally
would need a system-wide hook (``SetWindowsHookEx``/``RawInput``), and a
background process silently recording every keystroke is indistinguishable from
a keylogger -- it would be flagged by antivirus and would deserve to be. Gamepads
do not have this limitation: SDL sets ``SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS``,
so a real pad keeps working when the window is not focused.

In practice this is fine for the intended use: the player watches the console,
and the client window sits focused on the PC whose keyboard they are using.
"""

from __future__ import annotations

import logging

from client.input.base import DeviceInfo, InputBackend, InputBackendError
from client.input.mapping import (
    DEFAULT_KEYBOARD_AXES,
    DEFAULT_KEYBOARD_BINDINGS,
    DeviceMapping,
    InputSource,
    KeyAxisBinding,
    SourceKind,
)
from common.state import ControllerState, clamp_axis

log = logging.getLogger(__name__)

#: Stable identity for the virtual keyboard device, so a saved mapping and a
#: saved player-slot assignment both find it again across restarts.
KEYBOARD_GUID = "rbgc-keyboard"
KEYBOARD_INSTANCE_ID = -1000
KEYBOARD_NAME = "Keyboard"

#: Full deflection for a held direction key.
_AXIS_FULL = 32767


class KeyboardBackend(InputBackend):
    """One virtual gamepad driven by key events pushed in from the GUI."""

    def __init__(self, mapping: DeviceMapping | None = None) -> None:
        self._opened = False
        self._pressed: set[int] = set()
        self._acquired = False
        self._mapping = mapping or DeviceMapping(guid=KEYBOARD_GUID, name=KEYBOARD_NAME)

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        self._opened = True
        log.info("Keyboard input backend ready (window focus required)")

    def close(self) -> None:
        self._opened = False
        self._pressed.clear()
        self._acquired = False

    # -- event feed --------------------------------------------------------

    def set_key(self, key: int, down: bool) -> None:
        """Record a key transition. Called from the GUI thread."""
        if down:
            self._pressed.add(int(key))
        else:
            self._pressed.discard(int(key))

    def clear_keys(self) -> None:
        """Release everything.

        Called when the window loses focus: without it a key held at the moment
        focus was lost would stay latched down forever, because the release
        event goes to whichever window took focus.
        """
        self._pressed.clear()

    @property
    def pressed_keys(self) -> frozenset[int]:
        """Currently held keys. Used by the mapping screen's press-to-bind."""
        return frozenset(self._pressed)

    # -- mapping -----------------------------------------------------------

    def set_mapping(self, guid: str, mapping: DeviceMapping | None) -> None:
        if guid != KEYBOARD_GUID:
            return
        self._mapping = mapping or DeviceMapping(guid=KEYBOARD_GUID, name=KEYBOARD_NAME)

    @property
    def mapping(self) -> DeviceMapping:
        return self._mapping

    # -- InputBackend ------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        self._require_open()
        return [
            DeviceInfo(
                instance_id=KEYBOARD_INSTANCE_ID,
                name=KEYBOARD_NAME,
                guid=KEYBOARD_GUID,
                is_mapped=not self._mapping.is_empty(),
                button_count=0,
                axis_count=0,
                hat_count=0,
            )
        ]

    def acquire(self, instance_id: int) -> DeviceInfo:
        self._require_open()
        if instance_id != KEYBOARD_INSTANCE_ID:
            raise InputBackendError(f"Keyboard backend has no device {instance_id}")
        self._acquired = True
        return self.list_devices()[0]

    def release(self, instance_id: int) -> None:
        if instance_id == KEYBOARD_INSTANCE_ID:
            self._acquired = False
            self._pressed.clear()

    def pump(self) -> None:
        """No-op: events are pushed in by the GUI rather than polled for."""

    def poll(self, instance_id: int, out: ControllerState) -> bool:
        if instance_id != KEYBOARD_INSTANCE_ID or not self._acquired:
            return False

        pressed = self._pressed
        mapping = self._mapping

        buttons = 0
        for bit, source in mapping.buttons.items():
            if source.kind is SourceKind.KEY and source.index in pressed:
                buttons |= bit

        out.left_x = out.left_y = out.right_x = out.right_y = 0
        out.left_trigger = out.right_trigger = 0

        for name, binding in mapping.key_axes.items():
            value = 0
            if binding.negative and binding.negative in pressed:
                value -= _AXIS_FULL
            if binding.positive and binding.positive in pressed:
                value += _AXIS_FULL
            if name in ("left_trigger", "right_trigger"):
                setattr(out, name, 255 if value > 0 else 0)
            else:
                setattr(out, name, clamp_axis(value))

        out.buttons = buttons
        out.apply_trigger_buttons()
        return True

    def _require_open(self) -> None:
        if not self._opened:
            raise InputBackendError("Backend is not open; call open() first")


def default_keyboard_mapping() -> DeviceMapping:
    """The out-of-the-box keyboard layout.

    Qt is imported here rather than at module scope so the rest of the input
    layer stays importable headless -- ``--headless`` never uses this backend.
    """
    from PySide6.QtGui import QKeySequence

    def code(name: str) -> int:
        sequence = QKeySequence(name)
        return int(sequence[0].key()) if sequence.count() else 0

    mapping = DeviceMapping(guid=KEYBOARD_GUID, name=KEYBOARD_NAME)

    for bit, key_name in DEFAULT_KEYBOARD_BINDINGS:
        key = code(key_name)
        if key:
            mapping.buttons[bit] = InputSource(SourceKind.KEY, key)

    pairs: dict[str, dict[str, int]] = {}
    for axis, key_name, direction in DEFAULT_KEYBOARD_AXES:
        key = code(key_name)
        if not key:
            continue
        slot = pairs.setdefault(axis, {})
        slot["negative" if direction < 0 else "positive"] = key

    for axis, slot in pairs.items():
        mapping.key_axes[axis] = KeyAxisBinding(
            negative=slot.get("negative", 0), positive=slot.get("positive", 0)
        )

    return mapping
