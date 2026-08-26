"""Target profiles: what the server pretends to be over Bluetooth."""

from __future__ import annotations

from server.bt.profiles.base import ProfileDescriptor, RumbleCommand, TargetProfile
from server.bt.profiles.eightbitdo64 import EightBitDo64Profile
from server.bt.profiles.generic_gamepad import GenericGamepadProfile
from server.bt.profiles.switch_pro import SwitchProProfile

#: Registry keyed by the identifier used in config and the web GUI.
PROFILES: dict[str, type[TargetProfile]] = {
    "generic": GenericGamepadProfile,
    "switch_pro": SwitchProProfile,
    # Reproduced field for field from a physical pad -- see its module
    # docstring. This is the profile an Analogue 3D needs.
    "8bitdo_64": EightBitDo64Profile,
}

DEFAULT_PROFILE = "generic"

__all__ = [
    "DEFAULT_PROFILE",
    "PROFILES",
    "EightBitDo64Profile",
    "GenericGamepadProfile",
    "ProfileDescriptor",
    "RumbleCommand",
    "SwitchProProfile",
    "TargetProfile",
    "available_profiles",
    "create_profile",
]


def create_profile(name: str, **kwargs) -> TargetProfile:
    """Instantiate a profile by name."""
    try:
        cls = PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown profile {name!r}. Available: {', '.join(sorted(PROFILES))}"
        ) from None
    return cls(**kwargs)


def available_profiles() -> list[dict[str, str]]:
    """Profile list for the web GUI dropdown."""
    entries = []
    for key, cls in PROFILES.items():
        instance = cls()
        entries.append({"name": key, "display_name": instance.display_name})
    return entries
