"""One backend presenting several underlying ones as a single device list.

The player should be able to pick "Keyboard" from the same dropdown as their
gamepads, and assign it to a slot the same way. Rather than teaching the GUI and
the input loop about two backends, this merges them: everything downstream sees
one backend with one device list.

Routing is by ``instance_id``. Each sub-backend owns a disjoint id space (SDL
hands out non-negative ids; the keyboard uses a fixed negative one), and the
owner of every id is recorded as devices are listed and acquired, so a lookup is
never a guess.
"""

from __future__ import annotations

import logging

from client.input.base import DeviceInfo, InputBackend, InputBackendError
from common.state import ControllerState

log = logging.getLogger(__name__)


class CompositeBackend(InputBackend):
    """Merges several backends into one."""

    def __init__(self, backends: list[InputBackend]) -> None:
        if not backends:
            raise InputBackendError("CompositeBackend needs at least one backend")
        self._backends = list(backends)
        self._owner: dict[int, InputBackend] = {}

    @property
    def backends(self) -> list[InputBackend]:
        return list(self._backends)

    def backend_for(self, instance_id: int) -> InputBackend | None:
        return self._owner.get(instance_id)

    def open(self) -> None:
        opened: list[InputBackend] = []
        for backend in self._backends:
            try:
                backend.open()
                opened.append(backend)
            except InputBackendError:
                # One backend failing must not take the others down -- no SDL2
                # should still leave the keyboard usable, and vice versa.
                log.warning("Input backend %s unavailable", type(backend).__name__,
                            exc_info=True)
        if not opened:
            raise InputBackendError("No input backend could be opened")
        self._backends = opened

    def close(self) -> None:
        for backend in self._backends:
            try:
                backend.close()
            except Exception:
                log.debug("Closing %s failed", type(backend).__name__, exc_info=True)
        self._owner.clear()

    def list_devices(self) -> list[DeviceInfo]:
        devices: list[DeviceInfo] = []
        for backend in self._backends:
            try:
                found = backend.list_devices()
            except InputBackendError:
                continue
            for device in found:
                self._owner[device.instance_id] = backend
            devices.extend(found)
        return devices

    def acquire(self, instance_id: int) -> DeviceInfo:
        backend = self._owner.get(instance_id)
        if backend is None:
            # Not seen yet -- refresh and try again before giving up, so a pad
            # plugged in since the last listing still works.
            self.list_devices()
            backend = self._owner.get(instance_id)
        if backend is None:
            raise InputBackendError(f"Controller {instance_id} is not connected")
        return backend.acquire(instance_id)

    def release(self, instance_id: int) -> None:
        backend = self._owner.get(instance_id)
        if backend is not None:
            backend.release(instance_id)

    def poll(self, instance_id: int, out: ControllerState) -> bool:
        backend = self._owner.get(instance_id)
        if backend is None:
            return False
        return backend.poll(instance_id, out)

    def pump(self) -> None:
        for backend in self._backends:
            backend.pump()

    def rumble(self, instance_id: int, low: float, high: float, duration_ms: int) -> None:
        backend = self._owner.get(instance_id)
        if backend is not None:
            backend.rumble(instance_id, low, high, duration_ms)

    # -- pass-throughs used by the mapping screen --------------------------

    def set_mapping(self, guid: str, mapping) -> None:
        for backend in self._backends:
            setter = getattr(backend, "set_mapping", None)
            if setter is not None:
                setter(guid, mapping)

    def raw_snapshot(self, instance_id: int) -> dict | None:
        backend = self._owner.get(instance_id)
        snapshot = getattr(backend, "raw_snapshot", None)
        return snapshot(instance_id) if snapshot is not None else None
