"""Input backend interface.

Backends translate whatever the OS exposes into ``ControllerState``. Keeping
this behind an interface means we can drop in a lower-latency platform-native
backend (raw evdev on Linux, XInput on Windows) later without touching the
poll loop, the transport, or the GUI.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from common.state import ControllerState


@dataclass(slots=True)
class DeviceInfo:
    """A gamepad the backend can see.

    ``instance_id`` is the backend's live handle and is *not* stable across
    replug. ``guid`` identifies the hardware model and survives replug, so it
    is what we persist in the config to remember "player 2 uses the red pad".
    """

    instance_id: int
    name: str
    guid: str
    player_index: int = -1
    is_connected: bool = True

    #: False when the backend has no built-in layout for this pad, so it needs a
    #: user-supplied mapping before its buttons mean anything. Such devices are
    #: still listed -- hiding them made a working 18-button pad look undetected.
    is_mapped: bool = True

    #: Physical capabilities, for building a default mapping and for the
    #: mapping UI to know how many controls there are to bind.
    axis_count: int = 0
    button_count: int = 0
    hat_count: int = 0

    def display_name(self) -> str:
        return self.name or f"Controller {self.instance_id}"

    def status_note(self) -> str:
        """Short qualifier for the device list. Empty when nothing is wrong."""
        if not self.is_connected:
            return "disconnected"
        if not self.is_mapped:
            return "needs mapping"
        return ""


@dataclass(slots=True)
class PolledInput:
    """One controller's state as of the most recent poll."""

    device: DeviceInfo
    state: ControllerState = field(default_factory=ControllerState)
    #: Monotonic ns when the backend observed this state. Feeds the "input age"
    #: latency stage so we can tell a slow gamepad from a slow network.
    sampled_at_ns: int = 0


class InputBackend(abc.ABC):
    """Enumerates gamepads and samples their state.

    Implementations must be safe to call from a single dedicated thread. None
    of them are expected to be thread-safe for concurrent use.
    """

    @abc.abstractmethod
    def open(self) -> None:
        """Initialize the underlying library. Raises InputBackendError on failure."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release all resources. Must be idempotent."""

    @abc.abstractmethod
    def list_devices(self) -> list[DeviceInfo]:
        """Currently connected gamepads."""

    @abc.abstractmethod
    def acquire(self, instance_id: int) -> DeviceInfo:
        """Open a device for polling. Raises InputBackendError if unavailable."""

    @abc.abstractmethod
    def release(self, instance_id: int) -> None:
        """Stop polling a device. Must tolerate an already-released device."""

    @abc.abstractmethod
    def poll(self, instance_id: int, out: ControllerState) -> bool:
        """Sample one device into ``out``.

        Returns False if the device has gone away -- the caller then sends a
        neutral state so the console does not latch the last-held input.

        Must not allocate: this runs up to 1000 times a second per controller.
        """

    @abc.abstractmethod
    def pump(self) -> None:
        """Service the backend's event queue once per loop iteration.

        Called once per tick regardless of how many devices are open, so
        per-device polling stays cheap.
        """

    def rumble(self, instance_id: int, low: float, high: float, duration_ms: int) -> None:
        """Optional force feedback. Default is a no-op.

        Not on the latency path -- this exists so the server can eventually
        forward rumble from the console back to the player.
        """
        return None


class InputBackendError(RuntimeError):
    """Backend could not be initialized, or a device could not be opened."""
