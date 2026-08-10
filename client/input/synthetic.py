"""Synthetic input backend for testing without hardware.

Drives deterministic or scripted controller state so the whole pipeline --
poll loop, transport, crypto, server routing, HID report generation -- can be
exercised on any machine with no gamepad attached, and so latency measurements
have a ground truth to compare against.
"""

from __future__ import annotations

import math
import threading

from client.input.base import DeviceInfo, InputBackend, InputBackendError
from common.state import Button, ControllerState
from common.timing import now_ns


class SyntheticBackend(InputBackend):
    """Fake gamepads with programmable state.

    Two modes:

    * **Manual** -- tests call :meth:`set_state` and the next poll returns it.
      Deterministic, which is what protocol tests need.
    * **Animated** -- sticks trace a circle and buttons cycle, so a human can
      watch the pipeline move without touching a controller.
    """

    def __init__(self, count: int = 1, *, animate: bool = False) -> None:
        if count < 1:
            raise ValueError("need at least one synthetic controller")

        self._count = count
        self._animate = animate
        self._opened = False
        self._acquired: set[int] = set()
        self._states: dict[int, ControllerState] = {}
        self._detached: set[int] = set()
        self._lock = threading.Lock()
        self._start_ns = now_ns()

    def open(self) -> None:
        self._opened = True
        self._states = {i: ControllerState() for i in range(self._count)}

    def close(self) -> None:
        self._opened = False
        self._acquired.clear()

    def list_devices(self) -> list[DeviceInfo]:
        self._require_open()
        return [
            DeviceInfo(
                instance_id=i,
                name=f"Synthetic Controller {i}",
                guid=f"synthetic-{i:04d}",
                is_connected=i not in self._detached,
            )
            for i in range(self._count)
        ]

    def acquire(self, instance_id: int) -> DeviceInfo:
        self._require_open()
        if not 0 <= instance_id < self._count:
            raise InputBackendError(f"No synthetic controller {instance_id}")
        self._acquired.add(instance_id)
        return DeviceInfo(
            instance_id=instance_id,
            name=f"Synthetic Controller {instance_id}",
            guid=f"synthetic-{instance_id:04d}",
        )

    def release(self, instance_id: int) -> None:
        self._acquired.discard(instance_id)

    def pump(self) -> None:
        if self._animate:
            self._advance_animation()

    def poll(self, instance_id: int, out: ControllerState) -> bool:
        if instance_id not in self._acquired:
            return False
        if instance_id in self._detached:
            return False

        with self._lock:
            self._states[instance_id].copy_into(out)
        return True

    # -- test controls -----------------------------------------------------

    def set_state(self, instance_id: int, state: ControllerState) -> None:
        """Set what the next poll of ``instance_id`` will return."""
        with self._lock:
            state.copy_into(self._states[instance_id])

    def press(self, instance_id: int, button: Button) -> None:
        with self._lock:
            self._states[instance_id].buttons |= button

    def release_button(self, instance_id: int, button: Button) -> None:
        with self._lock:
            self._states[instance_id].buttons &= ~button

    def detach(self, instance_id: int) -> None:
        """Simulate an unplug, so disconnect handling can be tested."""
        self._detached.add(instance_id)

    def reattach(self, instance_id: int) -> None:
        self._detached.discard(instance_id)

    # -- internals ---------------------------------------------------------

    def _advance_animation(self) -> None:
        elapsed_s = (now_ns() - self._start_ns) / 1e9
        angle = elapsed_s * 2.0

        with self._lock:
            for index, state in self._states.items():
                phase = angle + index * (math.pi / 2)
                state.left_x = int(math.cos(phase) * 30000)
                state.left_y = int(math.sin(phase) * 30000)
                state.right_x = int(math.sin(phase * 0.5) * 20000)
                state.right_y = int(math.cos(phase * 0.5) * 20000)

                # Cycle one face button per second so button traffic is visible.
                cycle = int(elapsed_s) % 4
                state.buttons = (Button.A, Button.B, Button.X, Button.Y)[cycle]

                state.left_trigger = int((math.sin(phase) * 0.5 + 0.5) * 255)
                state.right_trigger = int((math.cos(phase) * 0.5 + 0.5) * 255)
                state.apply_trigger_buttons()

    def _require_open(self) -> None:
        if not self._opened:
            raise InputBackendError("Backend is not open; call open() first")
