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
from client.input.mapping import AXIS_PRESS_THRESHOLD, SourceKind
from common.state import Button, ControllerState, clamp_axis, scale_sdl_trigger

log = logging.getLogger(__name__)

# Hoisted to module level: poll() compares against these per control per tick,
# and an enum attribute lookup in that loop is measurable at 1 kHz.
_KIND_BUTTON = int(SourceKind.BUTTON)
_KIND_AXIS = int(SourceKind.AXIS)
_KIND_HAT = int(SourceKind.HAT)

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
        #: instance_id -> SDL_GameController*, for pads SDL has a layout for.
        self._handles: dict[int, object] = {}
        #: instance_id -> SDL_Joystick*, for pads driven through a user mapping.
        self._joysticks: dict[int, object] = {}
        #: guid -> DeviceMapping supplied by the player.
        self._mappings: dict[str, object] = {}
        #: instance_id -> CompiledMapping (or None), rebuilt on acquire/remap.
        self._compiled: dict[int, object] = {}
        #: instance_id -> DeviceInfo, so release() and remap() can find the guid.
        self._info: dict[int, DeviceInfo] = {}
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

    def _detect(self) -> None:
        """Let SDL notice devices that appeared or vanished since last time.

        SDL discovers hotplug inside ``SDL_JoystickUpdate``/``SDL_PumpEvents``,
        not in ``SDL_NumJoysticks``. Enumerating without this returns whatever
        was present at startup forever -- which is why "Refresh gamepad list"
        appeared to do nothing when a pad was connected after launch.
        """
        sdl2.SDL_PumpEvents()
        sdl2.SDL_JoystickUpdate()

    def list_devices(self) -> list[DeviceInfo]:
        self._require_open()
        self._detect()

        devices: list[DeviceInfo] = []

        for index in range(sdl2.SDL_NumJoysticks()):
            joystick = sdl2.SDL_JoystickOpen(index)
            if not joystick:
                continue
            try:
                instance_id = int(sdl2.SDL_JoystickInstanceID(joystick))
                mapped = bool(sdl2.SDL_IsGameController(index))

                # Prefer the GameController name -- it is the tidy marketing one
                # ("Xbox Series Controller") where the joystick name is often the
                # raw USB product string.
                if mapped:
                    name_ptr = sdl2.SDL_GameControllerNameForIndex(index)
                else:
                    name_ptr = sdl2.SDL_JoystickNameForIndex(index)
                name = name_ptr.decode("utf-8", "replace") if name_ptr else f"Controller {index}"

                devices.append(
                    DeviceInfo(
                        instance_id=instance_id,
                        name=name,
                        guid=_guid_string(sdl2.SDL_JoystickGetDeviceGUID(index)),
                        is_mapped=mapped,
                        axis_count=int(sdl2.SDL_JoystickNumAxes(joystick)),
                        button_count=int(sdl2.SDL_JoystickNumButtons(joystick)),
                        hat_count=int(sdl2.SDL_JoystickNumHats(joystick)),
                    )
                )
            finally:
                # Only close if we are not actively polling it; closing an open
                # handle would drop the controller mid-game.
                if int(sdl2.SDL_JoystickInstanceID(joystick)) not in self._handles:
                    sdl2.SDL_JoystickClose(joystick)

        return devices

    def set_mapping(self, guid: str, mapping) -> None:
        """Install a user mapping for a device model, keyed by GUID.

        Applies to mapped and unmapped pads alike: a player remapping a
        recognised controller goes through exactly the same path as one making
        an unrecognised pad work in the first place.
        """
        if mapping is None:
            self._mappings.pop(guid, None)
        else:
            self._mappings[guid] = mapping
        # Recompile anything already open so the change takes effect live.
        for instance_id, info in list(self._info.items()):
            if info.guid == guid:
                self._compiled[instance_id] = mapping.compile() if mapping else None

    def acquire(self, instance_id: int) -> DeviceInfo:
        self._require_open()
        if instance_id in self._handles:
            return self._device_info(instance_id)

        self._detect()

        for index in range(sdl2.SDL_NumJoysticks()):
            joystick = sdl2.SDL_JoystickOpen(index)
            if not joystick:
                continue
            found = int(sdl2.SDL_JoystickInstanceID(joystick)) == instance_id
            if not found:
                sdl2.SDL_JoystickClose(joystick)
                continue

            guid = _guid_string(sdl2.SDL_JoystickGetDeviceGUID(index))
            mapped = bool(sdl2.SDL_IsGameController(index))

            info = DeviceInfo(
                instance_id=instance_id,
                name=_device_name(index, mapped),
                guid=guid,
                is_mapped=mapped,
                axis_count=int(sdl2.SDL_JoystickNumAxes(joystick)),
                button_count=int(sdl2.SDL_JoystickNumButtons(joystick)),
                hat_count=int(sdl2.SDL_JoystickNumHats(joystick)),
            )

            if mapped:
                # Close the joystick handle: SDL_GameControllerOpen takes its own.
                sdl2.SDL_JoystickClose(joystick)
                handle = sdl2.SDL_GameControllerOpen(index)
                if not handle:
                    raise InputBackendError(
                        f"Could not open controller {instance_id}: {_sdl_error()}"
                    )
                self._handles[instance_id] = handle
            else:
                # No GameController layout: drive it as a raw joystick through a
                # user mapping. Keep the joystick handle we already opened.
                self._joysticks[instance_id] = joystick
                log.info(
                    "Controller %s (%s) has no SDL mapping; using raw joystick "
                    "(%d axes, %d buttons, %d hats). Bindings come from the "
                    "mapping screen.",
                    instance_id, info.name,
                    info.axis_count, info.button_count, info.hat_count,
                )

            self._info[instance_id] = info
            mapping = self._mappings.get(guid)
            self._compiled[instance_id] = mapping.compile() if mapping else None

            log.info("Acquired controller %s (%s)", instance_id, info.name)
            return info

        raise InputBackendError(f"Controller {instance_id} is not connected")

    def release(self, instance_id: int) -> None:
        handle = self._handles.pop(instance_id, None)
        if handle is not None:
            sdl2.SDL_GameControllerClose(handle)
        joystick = self._joysticks.pop(instance_id, None)
        if joystick is not None:
            sdl2.SDL_JoystickClose(joystick)
        self._info.pop(instance_id, None)
        self._compiled.pop(instance_id, None)
        if handle is not None or joystick is not None:
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
        compiled = self._compiled.get(instance_id)

        handle = self._handles.get(instance_id)
        if handle is None:
            # Raw joystick path: a pad with no SDL layout, or one the player has
            # remapped. Both read through the compiled mapping.
            return self._poll_joystick(instance_id, compiled, out)

        if not sdl2.SDL_GameControllerGetAttached(handle):
            return False

        if compiled is not None:
            # The player remapped this pad. Its physical controls are still the
            # GameController's, so read those and route them through the mapping.
            return self._poll_mapped_with_override(handle, compiled, out)

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

    def _poll_joystick(self, instance_id: int, compiled, out: ControllerState) -> bool:
        """Read a pad SDL has no layout for, through the player's mapping.

        Allocation-free: ``compiled`` is pre-flattened tuples, so this walks
        plain integers with no dict lookups per control.
        """
        joystick = self._joysticks.get(instance_id)
        if joystick is None:
            return False
        if not sdl2.SDL_JoystickGetAttached(joystick):
            return False

        out.left_x = out.left_y = out.right_x = out.right_y = 0
        out.left_trigger = out.right_trigger = 0

        if compiled is None:
            # Acquired but not yet bound. Report neutral rather than failing, so
            # the device stays selectable while the player maps it.
            out.buttons = 0
            return True

        buttons = 0
        for kind, index, value, bit in compiled.buttons:
            if kind == _KIND_BUTTON:
                if sdl2.SDL_JoystickGetButton(joystick, index):
                    buttons |= bit
            elif kind == _KIND_AXIS:
                reading = sdl2.SDL_JoystickGetAxis(joystick, index)
                if (reading >= AXIS_PRESS_THRESHOLD) if value >= 0 else (reading <= -AXIS_PRESS_THRESHOLD):
                    buttons |= bit
            elif kind == _KIND_HAT:
                if sdl2.SDL_JoystickGetHat(joystick, index) & value:
                    buttons |= bit

        for name, index, invert in compiled.sticks:
            reading = sdl2.SDL_JoystickGetAxis(joystick, index)
            setattr(out, name, clamp_axis(-reading if invert else reading))

        for name, index, invert in compiled.triggers:
            reading = sdl2.SDL_JoystickGetAxis(joystick, index)
            setattr(out, name, scale_sdl_trigger(-reading if invert else reading))

        out.buttons = buttons
        out.apply_trigger_buttons()
        return True

    def _poll_mapped_with_override(self, handle, compiled, out: ControllerState) -> bool:
        """Read a GameController whose bindings the player has changed."""
        joystick = sdl2.SDL_GameControllerGetJoystick(handle)
        if not joystick:
            return False

        out.left_x = out.left_y = out.right_x = out.right_y = 0
        out.left_trigger = out.right_trigger = 0

        buttons = 0
        for kind, index, value, bit in compiled.buttons:
            if kind == _KIND_BUTTON:
                if sdl2.SDL_JoystickGetButton(joystick, index):
                    buttons |= bit
            elif kind == _KIND_AXIS:
                reading = sdl2.SDL_JoystickGetAxis(joystick, index)
                if (reading >= AXIS_PRESS_THRESHOLD) if value >= 0 else (reading <= -AXIS_PRESS_THRESHOLD):
                    buttons |= bit
            elif kind == _KIND_HAT:
                if sdl2.SDL_JoystickGetHat(joystick, index) & value:
                    buttons |= bit

        for name, index, invert in compiled.sticks:
            reading = sdl2.SDL_JoystickGetAxis(joystick, index)
            setattr(out, name, clamp_axis(-reading if invert else reading))

        for name, index, invert in compiled.triggers:
            reading = sdl2.SDL_JoystickGetAxis(joystick, index)
            setattr(out, name, scale_sdl_trigger(-reading if invert else reading))

        out.buttons = buttons
        out.apply_trigger_buttons()
        return True

    def raw_snapshot(self, instance_id: int) -> dict | None:
        """Every physical control's current value, for the mapping UI.

        Off the hot path -- this backs press-to-bind, which runs at GUI rates.
        Returns None if the device is not open.
        """
        joystick = self._joysticks.get(instance_id)
        if joystick is None:
            handle = self._handles.get(instance_id)
            if handle is None:
                return None
            joystick = sdl2.SDL_GameControllerGetJoystick(handle)
            if not joystick:
                return None

        return {
            "axes": [
                int(sdl2.SDL_JoystickGetAxis(joystick, i))
                for i in range(sdl2.SDL_JoystickNumAxes(joystick))
            ],
            "buttons": [
                bool(sdl2.SDL_JoystickGetButton(joystick, i))
                for i in range(sdl2.SDL_JoystickNumButtons(joystick))
            ],
            "hats": [
                int(sdl2.SDL_JoystickGetHat(joystick, i))
                for i in range(sdl2.SDL_JoystickNumHats(joystick))
            ],
        }

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
        cached = self._info.get(instance_id)
        if cached is not None:
            handle = self._handles.get(instance_id)
            joystick = self._joysticks.get(instance_id)
            if handle is not None:
                cached.is_connected = bool(sdl2.SDL_GameControllerGetAttached(handle))
            elif joystick is not None:
                cached.is_connected = bool(sdl2.SDL_JoystickGetAttached(joystick))
            return cached

        handle = self._handles[instance_id]
        name_ptr = sdl2.SDL_GameControllerName(handle)
        joystick = sdl2.SDL_GameControllerGetJoystick(handle)
        return DeviceInfo(
            instance_id=instance_id,
            name=name_ptr.decode("utf-8", "replace") if name_ptr else "Controller",
            guid=_guid_string(sdl2.SDL_JoystickGetGUID(joystick)),
            is_connected=bool(sdl2.SDL_GameControllerGetAttached(handle)),
        )


def _device_name(index: int, mapped: bool) -> str:
    """Best available human name for a device index."""
    ptr = (
        sdl2.SDL_GameControllerNameForIndex(index)
        if mapped
        else sdl2.SDL_JoystickNameForIndex(index)
    )
    return ptr.decode("utf-8", "replace") if ptr else f"Controller {index}"


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
