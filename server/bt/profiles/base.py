"""Target profile interface.

A profile owns everything specific to what we are pretending to be: the HID
report descriptor, the SDP service record, the device name and class, and the
translation from our normalized ``ControllerState`` into the exact bytes that
target expects on the interrupt channel.

Keeping this behind an interface is what lets one client stream drive either a
generic BT gamepad or a Switch Pro Controller with no change above this layer.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from common.state import ControllerState


@dataclass(slots=True)
class RumbleCommand:
    """A rumble effect the console asked the controller to play.

    Normalized across targets: two motors at 0-255, plus an optional duration.
    ``duration_ms`` of 0 means "run until superseded", which is how most
    consoles drive rumble -- they send a stop command rather than a timeout.
    """

    low_freq: int = 0       # heavy motor
    high_freq: int = 0      # light motor
    duration_ms: int = 0

    @property
    def is_stop(self) -> bool:
        return self.low_freq == 0 and self.high_freq == 0

    def clamped(self) -> RumbleCommand:
        return RumbleCommand(
            low_freq=max(0, min(255, self.low_freq)),
            high_freq=max(0, min(255, self.high_freq)),
            duration_ms=max(0, min(65535, self.duration_ms)),
        )


@dataclass(slots=True)
class ProfileDescriptor:
    """Static identity a profile advertises over Bluetooth."""

    #: Shown in the console's pairing list.
    device_name: str

    #: Bluetooth Class of Device. 0x002508 = peripheral / gamepad.
    device_class: int

    #: HID report descriptor bytes, embedded in the SDP record.
    report_descriptor: bytes

    #: Vendor/product identifiers advertised via SDP. Some consoles gate
    #: behaviour on these, so profiles set them deliberately.
    vendor_id: int
    product_id: int
    version: int = 0x0100

    #: HID report id prefixed to interrupt-channel writes, or None if the
    #: descriptor declares no report ids.
    input_report_id: int | None = None


class TargetProfile(abc.ABC):
    """Translates controller state into a specific target's HID reports."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier used in config and the web GUI (e.g. 'generic')."""

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """Human-readable name for the web GUI."""

    @property
    @abc.abstractmethod
    def descriptor(self) -> ProfileDescriptor:
        """Static Bluetooth identity."""

    @abc.abstractmethod
    def build_input_report(self, state: ControllerState, buf: bytearray) -> int:
        """Serialize ``state`` into ``buf``. Returns the number of bytes written.

        Called on the datapath for every input packet, so it must not allocate
        -- write into the caller's buffer via slice assignment or struct
        ``pack_into``.

        The returned bytes are what goes out on the interrupt channel, minus
        the L2CAP HID transaction header the caller prepends.
        """

    def on_output_report(self, data: bytes) -> bytes | None:
        """Handle a report from the console (rumble, LED, feature requests).

        Returns a response to send back, or None. Runs off the hot path.

        The default ignores everything, which is correct for a generic HID
        gamepad: the console does not expect a reply. Profiles with a
        handshake -- notably the Switch -- override this.
        """
        return None

    def extract_rumble(self, data: bytes) -> "RumbleCommand | None":
        """Pull a rumble command out of a console output report, if present.

        Returns None when the report carries no rumble, which is the common
        case -- most output reports are LED or configuration traffic.

        Runs on the Bluetooth control thread, off the input hot path. Profiles
        that cannot decode rumble simply return None and the feature is silently
        unavailable for that target rather than misbehaving.
        """
        return None

    def on_connected(self) -> None:
        """Called once the interrupt channel is up. Reset any handshake state."""

    def on_disconnected(self) -> None:
        """Called when the console disconnects."""

    @property
    def is_ready(self) -> bool:
        """True when the target will actually accept input reports.

        Generic HID is ready immediately. The Switch is not ready until its
        subcommand handshake completes -- sending input before then is ignored
        and can wedge pairing.
        """
        return True
