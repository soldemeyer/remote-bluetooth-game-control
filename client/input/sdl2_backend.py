"""SDL2 GameController input backend.

SDL2 is the pragmatic default: one API covering Xbox, PlayStation, Switch and
generic pads on both Windows and Linux, with a built-in mapping database so
buttons land in the right logical positions without per-device configuration.

We use the GameController API rather than the raw Joystick API precisely for
that mapping -- raw joystick axis numbering differs per vendor and would push
that mess onto the user.

The video subsystem is never initialized. SDL can service controller events
headlessly, which matters because the client must run without a window in
``--headless`` mode.
"""

from __future__ import annotations

import ctypes
import logging
import os

from client.input.base import DeviceInfo, InputBackend, InputBackendError
from common.state import Button, ControllerState, clamp_axis, scale_sdl_trigger

log = logging.getLogger(__name__)

try:
    import sdl2
except ImportError as _exc:  # pragma: no cover - exercised only without SDL2 installed
    sdl2 = None
    _IMPORT_ERROR = _exc
else:
    _IMPORT_ERROR = None


#: SDL button constant -> our logical Button. SDL has already normalized
#: physical layout differences, so this is a straight positional mapping.
_BUTTON_MAP: dict[int, int] = {}

#: SDL axis constant -> ControllerState attribute name.
_STICK_AXES: tuple[tuple[int, str], ...] = ()


def _build_maps() -> None:
    """Populate the constant maps once SDL2 is known to be importable."""
    global _BUTTON_MAP, _STICK_AXES

    _BUTTON_MAP = {
        sdl2.SDL_CONTROLLER_BUTTON_A: Button.A,
        sdl2.SDL_CONTROLLER_BUTTON_B: Button.B,
        sdl2.SDL_CONTROLLER_BUTTON_X: Button.X,
        sdl2.SDL_CONTROLLER_BUTTON_Y: Button.Y,
        sdl2.SDL_CONTROLLER_BUTTON_LEFTSHOULDER: Button.LEFT_BUMPER,
        sdl2.SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: Button.RIGHT_BUMPER,
        sdl2.SDL_CONTROLLER_BUTTON_BACK: Button.BACK,
        sdl2.SDL_CONTROLLER_BUTTON_START: Button.START,
        sdl2.SDL_CONTROLLER_BUTTON_GUIDE: Button.GUIDE,
        sdl2.SDL_CONTROLLER_BUTTON_LEFTSTICK: Button.LEFT_STICK,
        sdl2.SDL_CONTROLLER_BUTTON_RIGHTSTICK: Button.RIGHT_STICK,
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_UP: Button.DPAD_UP,
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_DOWN: Button.DPAD_DOWN,
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_LEFT: Button.DPAD_LEFT,
        sdl2.SDL_CONTROLLER_BUTTON_DPAD_RIGHT: Button.DPAD_RIGHT,
    }

    # The capture/share button only exists in newer SDL2 releases.
    if hasattr(sdl2, "SDL_CONTROLLER_BUTTON_MISC1"):
        _BUTTON_MAP[sdl2.SDL_CONTROLLER_BUTTON_MISC1] = Button.CAPTURE

    _STICK_AXES = (
        (sdl2.SDL_CONTROLLER_AXIS_LEFTX, "left_x"),
        (sdl2.SDL_CONTROLLER_AXIS_LEFTY, "left_y"),
        (sdl2.SDL_CONTROLLER_AXIS_RIGHTX, "right_x"),
        (sdl2.SDL_CONTROLLER_AXIS_RIGHTY, "right_y"),
    )


class SDL2Backend(InputBackend):
    """Gamepad input via SDL2's GameController API."""

    def __init__(self, *, allow_background: bool = True) -> None:
        if sdl2 is None:
            raise InputBackendError(
                "PySDL2 is not installed. Install the client extras: "
                'pip install -e ".[client]"'
            ) from _IMPORT_ERROR

        self._opened = False
        self._handles: dict[int, object] = {}
        self._allow_background = allow_background

    def open(self) -> None:
        if self._opened:
            return

        # Keep receiving input when the window is not focused -- the player is
        # looking at the game, not at our GUI.
        if self._allow_background:
            os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")

        # No video subsystem: we must work headless.
        if sdl2.SDL_Init(sdl2.SDL_INIT_GAMECONTROLLER | sdl2.SDL_INIT_JOYSTICK) != 0:
            raise InputBackendError(f"SDL_Init failed: {_sdl_error()}")

        _build_maps()

        # We poll explicitly; letting SDL also auto-update would duplicate work.
        sdl2.SDL_SetHint(sdl2.SDL_HINT_JOYSTICK_ALLOW_BACKGROUND_EVENTS, b"1")
        sdl2.SDL_GameControllerEventState(sdl2.SDL_ENABLE)

        self._opened = True
        log.info("SDL2 input backend initialized (%s)", _sdl_version())

    def close(self) -> None:
        if not self._opened:
            return
        for instance_id in list(self._handles):
            self.release(instance_id)
        sdl2.SDL_Quit()
        self._opened = False
        log.info("SDL2 input backend closed")

    def list_devices(self) -> list[DeviceInfo]:
        self._require_open()
        devices: list[DeviceInfo] = []

        for index in range(sdl2.SDL_NumJoysticks()):
            if not sdl2.SDL_IsGameController(index):
                # Wheels, flight sticks and similar have no GameController
                # mapping. Skipping them keeps the GUI list meaningful.
                continue

            joystick = sdl2.SDL_JoystickOpen(index)
            if not joystick:
                continue
            try:
                instance_id = sdl2.SDL_JoystickInstanceID(joystick)
                name_ptr = sdl2.SDL_GameControllerNameForIndex(index)
                name = name_ptr.decode("utf-8", "replace") if name_ptr else f"Controller {index}"
                devices.append(
                    DeviceInfo(
                        instance_id=int(instance_id),
                        name=name,
                        guid=_guid_string(sdl2.SDL_JoystickGetDeviceGUID(index)),
                    )
                )
            finally:
                # Only close if we are not actively polling it; closing an open
                # handle would drop the controller mid-game.
                if int(sdl2.SDL_JoystickInstanceID(joystick)) not in self._handles:
                    sdl2.SDL_JoystickClose(joystick)

        return devices

    def acquire(self, instance_id: int) -> DeviceInfo:
        self._require_open()
        if instance_id in self._handles:
            return self._device_info(instance_id)

        for index in range(sdl2.SDL_NumJoysticks()):
            if not sdl2.SDL_IsGameController(index):
                continue
            joystick = sdl2.SDL_JoystickOpen(index)
            if not joystick:
                continue
            found = int(sdl2.SDL_JoystickInstanceID(joystick)) == instance_id
            sdl2.SDL_JoystickClose(joystick)
            if not found:
                continue

            handle = sdl2.SDL_GameControllerOpen(index)
            if not handle:
                raise InputBackendError(
                    f"Could not open controller {instance_id}: {_sdl_error()}"
                )
            self._handles[instance_id] = handle
            info = self._device_info(instance_id)
            log.info("Acquired controller %s (%s)", instance_id, info.name)
            return info

        raise InputBackendError(f"Controller {instance_id} is not connected")

    def release(self, instance_id: int) -> None:
        handle = self._handles.pop(instance_id, None)
        if handle is not None:
            sdl2.SDL_GameControllerClose(handle)
            log.info("Released controller %s", instance_id)

    def pump(self) -> None:
        """Drain SDL's event queue and refresh controller state.

        ``SDL_GameControllerUpdate`` does the actual device read;
        ``SDL_PumpEvents`` keeps the queue from growing unbounded, which on
        Windows eventually stalls the message loop.
        """
        self._require_open()
        sdl2.SDL_PumpEvents()
        sdl2.SDL_GameControllerUpdate()

        # Drop events we don't consume. Connect/disconnect is detected via
        # SDL_GameControllerGetAttached in poll(), which is simpler and cannot
        # miss a transition that happened while we were not looking.
        event = sdl2.SDL_Event()
        while sdl2.SDL_PollEvent(ctypes.byref(event)) != 0:
            pass

    def poll(self, instance_id: int, out: ControllerState) -> bool:
        """Sample one controller. Allocation-free by design -- runs at up to 1 kHz."""
        handle = self._handles.get(instance_id)
        if handle is None:
            return False

        if not sdl2.SDL_GameControllerGetAttached(handle):
            return False

        buttons = 0
        for sdl_button, logical in _BUTTON_MAP.items():
            if sdl2.SDL_GameControllerGetButton(handle, sdl_button):
                buttons |= logical

        out.left_x = clamp_axis(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_LEFTX)
        )
        out.left_y = clamp_axis(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_LEFTY)
        )
        out.right_x = clamp_axis(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_RIGHTX)
        )
        out.right_y = clamp_axis(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_RIGHTY)
        )

        out.left_trigger = scale_sdl_trigger(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_TRIGGERLEFT)
        )
        out.right_trigger = scale_sdl_trigger(
            sdl2.SDL_GameControllerGetAxis(handle, sdl2.SDL_CONTROLLER_AXIS_TRIGGERRIGHT)
        )

        out.buttons = buttons
        out.apply_trigger_buttons()
        return True

    def rumble(self, instance_id: int, low: float, high: float, duration_ms: int) -> None:
        handle = self._handles.get(instance_id)
        if handle is None or not hasattr(sdl2, "SDL_GameControllerRumble"):
            return
        sdl2.SDL_GameControllerRumble(
            handle, int(low * 0xFFFF), int(high * 0xFFFF), duration_ms
        )

    # -- internals ---------------------------------------------------------

    def _require_open(self) -> None:
        if not self._opened:
            raise InputBackendError("Backend is not open; call open() first")

    def _device_info(self, instance_id: int) -> DeviceInfo:
        handle = self._handles[instance_id]
        name_ptr = sdl2.SDL_GameControllerName(handle)
        joystick = sdl2.SDL_GameControllerGetJoystick(handle)
        return DeviceInfo(
            instance_id=instance_id,
            name=name_ptr.decode("utf-8", "replace") if name_ptr else "Controller",
            guid=_guid_string(sdl2.SDL_JoystickGetGUID(joystick)),
            is_connected=bool(sdl2.SDL_GameControllerGetAttached(handle)),
        )


def _sdl_error() -> str:
    err = sdl2.SDL_GetError()
    return err.decode("utf-8", "replace") if err else "unknown error"


def _sdl_version() -> str:
    version = sdl2.SDL_version()
    sdl2.SDL_GetVersion(ctypes.byref(version))
    return f"SDL {version.major}.{version.minor}.{version.patch}"


def _guid_string(guid) -> str:
    """Render an SDL_JoystickGUID as hex.

    The GUID identifies the hardware model and survives replug, so config can
    remember which physical pad belongs to which player slot.
    """
    buf = ctypes.create_string_buffer(33)
    sdl2.SDL_JoystickGetGUIDString(guid, buf, 33)
    return buf.value.decode("ascii", "replace")


def is_available() -> bool:
    """True if SDL2 can be imported. Lets the GUI show a useful error."""
    return sdl2 is not None
