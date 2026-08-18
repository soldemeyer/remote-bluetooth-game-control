"""Named controller configurations.

A configuration bundles the two things that always travel together:

  * the **controller type** it was designed against (the preview layout), and
  * the **bindings** themselves.

Bindings are meaningful only relative to a target. An N64 configuration wants the
C buttons on the right stick; an Xbox one wants a normal face diamond. Storing
the layout beside the mapping means selecting a configuration selects both, and
the preview always shows the controller the bindings were actually built for.

Configurations are referenced by **name** from each controller slot, so one pad
can have several (say "8BitDo — N64" and "8BitDo — SNES") and slots can share
them. They are portable: :func:`export_to_file` writes a plain JSON document
that can be copied to another machine.

The controller type is a **binding and preview concept only**. It does not change
what the server emulates — that stays the generic HID gamepad (or Switch Pro)
chosen per adapter in the server GUI. Real 8BitDo receivers do the per-console
translation on the console side, not over Bluetooth.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from client.gui.controller_layouts import DEFAULT_LAYOUT, get_layout
from client.input.mapping import DeviceMapping

log = logging.getLogger(__name__)

#: Version stamp on exported files, so a future format change can be detected
#: rather than silently mis-parsed.
FILE_VERSION = 1


@dataclass(slots=True)
class ControllerConfiguration:
    """One named bundle of controller type plus bindings.

    Holds a **mapping per controller type**, not one shared mapping. Bindings
    are only meaningful relative to a target: an N64 configuration wants the C
    buttons on the right stick and has no X/Y at all, while an Xbox one wants a
    full face diamond. Sharing one mapping across types meant switching type
    silently kept bindings for buttons the new type does not have.

    ``layout`` names the type currently in effect; ``mapping`` reads and writes
    that type's bindings.
    """

    name: str
    layout: str = DEFAULT_LAYOUT
    mappings: dict = field(default_factory=dict)
    #: GUID of the pad this was built against. Informational: a configuration
    #: can be applied to any device, it just may not fit.
    device_guid: str = ""
    device_name: str = ""

    #: Set on the seven shipped presets. Built-ins are markers, not bindings:
    #: ``mappings`` is empty and ``family`` names the rule that produces them
    #: for whichever pad the slot is using. Editing one saves a copy under a new
    #: name, so a built-in is never destroyed.
    #:
    #: Not persisted -- built-ins are re-seeded on every load, which is also how
    #: a deleted one comes back and how an improved preset reaches an existing
    #: install.
    builtin: bool = False
    family: str = ""
    #: True when the bindings had to be guessed because SDL has no entry for
    #: this pad. Surfaced in the UI: a guess that looks authoritative is worse
    #: than one that admits it.
    approximate: bool = False

    @property
    def mapping(self) -> DeviceMapping:
        """Bindings for the active controller type, created on first use."""
        entry = self.mappings.get(self.layout)
        if entry is None:
            entry = DeviceMapping(guid=self.device_guid, name=self.device_name)
            self.mappings[self.layout] = entry
        return entry

    @mapping.setter
    def mapping(self, value: DeviceMapping) -> None:
        self.mappings[self.layout] = value

    def mapping_for(self, layout: str) -> DeviceMapping:
        entry = self.mappings.get(layout)
        if entry is None:
            entry = DeviceMapping(guid=self.device_guid, name=self.device_name)
            self.mappings[layout] = entry
        return entry

    def configured_layouts(self) -> list[str]:
        """Types that actually have bindings, for display.

        A built-in has bindings for every type by construction -- they are
        produced on demand rather than stored -- so it reports all of them.
        """
        if self.builtin:
            from client.gui.controller_layouts import LAYOUTS

            return [layout.key for layout in LAYOUTS]
        # .get avoids mapping_for()'s create-on-read, which would otherwise
        # write empty mappings back into the config file just by looking.
        return [key for key, m in self.mappings.items() if not m.is_empty()]

    @property
    def layout_name(self) -> str:
        return get_layout(self.layout).name

    def describe(self) -> str:
        """Label for the Configuration dropdown.

        The controller type used to be appended here. It is now its own column,
        because a slot picks the type independently of the configuration.
        """
        if self.builtin:
            return f"{self.name} (built-in)"
        return self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "layout": self.layout,
            "device_guid": self.device_guid,
            "device_name": self.device_name,
            "mappings": {k: m.to_dict() for k, m in self.mappings.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> ControllerConfiguration:
        mappings: dict = {}
        for key, payload in (data.get("mappings") or {}).items():
            mappings[str(key)] = DeviceMapping.from_dict(payload)

        # Files written before mappings became per-type carry a single
        # "mapping"; attribute it to whichever layout was active.
        legacy = data.get("mapping")
        layout = str(data.get("layout", DEFAULT_LAYOUT))
        if legacy and layout not in mappings:
            mappings[layout] = DeviceMapping.from_dict(legacy)

        return cls(
            name=str(data.get("name", "")).strip() or "Unnamed",
            layout=layout,
            device_guid=str(data.get("device_guid", "")),
            device_name=str(data.get("device_name", "")),
            mappings=mappings,
        )

    def copy_as(self, name: str) -> ControllerConfiguration:
        """An independent copy under a new name.

        Never ``builtin``: a copy is the player's, and editing it must not be
        able to destroy the shipped preset it came from.
        """
        return ControllerConfiguration(
            name=name,
            layout=self.layout,
            device_guid=self.device_guid,
            device_name=self.device_name,
            approximate=self.approximate,
            mappings={
                key: DeviceMapping.from_dict(m.to_dict())
                for key, m in self.mappings.items()
            },
        )


class ConfigurationStore:
    """The set of configurations, backed by the client config file."""

    def __init__(self, entries: list[ControllerConfiguration] | None = None) -> None:
        self._entries: list[ControllerConfiguration] = list(entries or [])

    # -- persistence -------------------------------------------------------

    @classmethod
    def from_config(cls, config) -> ConfigurationStore:
        entries = []
        for payload in getattr(config, "configurations", None) or []:
            try:
                entries.append(ControllerConfiguration.from_dict(payload))
            except Exception:
                # A hand-edited or truncated entry must not stop the client
                # starting; the rest of the list is still usable.
                log.warning("Skipping unreadable controller configuration", exc_info=True)
        store = cls(entries)
        store.seed_builtins()
        return store

    def seed_builtins(self) -> None:
        """Add any shipped preset the store does not already have.

        Runs on every load rather than once, so a preset that is improved in a
        later release reaches existing installs, and a deleted one comes back
        instead of leaving the player with an empty list. A user configuration
        that happens to share a name wins -- theirs is the one they edited.
        """
        from client.gui.controller_presets import builtin_configurations

        taken = {entry.name for entry in self._entries}
        for builtin in builtin_configurations():
            if builtin.name not in taken:
                self._entries.append(builtin)

    def into_config(self, config) -> None:
        """Persist user configurations. Built-ins are re-seeded, not saved."""
        config.configurations = [
            entry.to_dict() for entry in self._entries if not entry.builtin
        ]

    # -- access ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def names(self) -> list[str]:
        return [entry.name for entry in self._entries]

    def get(self, name: str) -> ControllerConfiguration | None:
        for entry in self._entries:
            if entry.name == name:
                return entry
        return None

    def upsert(self, configuration: ControllerConfiguration) -> None:
        for index, entry in enumerate(self._entries):
            if entry.name == configuration.name:
                self._entries[index] = configuration
                return
        self._entries.append(configuration)

    def remove(self, name: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.name != name]
        return len(self._entries) != before

    def unique_name(self, base: str) -> str:
        """``base``, or ``base (2)``, ``base (3)``... if taken."""
        base = base.strip() or "Configuration"
        if self.get(base) is None:
            return base
        for suffix in range(2, 100):
            candidate = f"{base} ({suffix})"
            if self.get(candidate) is None:
                return candidate
        return f"{base} ({len(self._entries) + 1})"

    def for_device(self, guid: str) -> list[ControllerConfiguration]:
        """Configurations built for this pad, most relevant first.

        Others are still returned afterwards: a configuration is applicable to
        any device, and refusing to show them would strand a player who swapped
        controllers.
        """
        mine = [e for e in self._entries if e.device_guid == guid]
        others = [e for e in self._entries if e.device_guid != guid]
        return mine + others

    # -- file import / export ---------------------------------------------

    def export_to_file(self, path: Path, names: list[str] | None = None) -> int:
        """Write configurations to a shareable JSON file. Returns how many."""
        selected = [
            entry for entry in self._entries if names is None or entry.name in names
        ]
        document = {
            "version": FILE_VERSION,
            "kind": "rbgc-controller-configurations",
            "configurations": [entry.to_dict() for entry in selected],
        }
        Path(path).write_text(json.dumps(document, indent=2), encoding="utf-8")
        return len(selected)

    def import_from_file(self, path: Path) -> list[str]:
        """Merge a configuration file in. Returns the names added.

        Imported names that clash are suffixed rather than overwriting: losing a
        working local configuration to an import would be a nasty surprise, and
        the player can delete the duplicate in a click.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))

        if not isinstance(raw, dict) or raw.get("kind") != "rbgc-controller-configurations":
            raise ValueError("Not an RBGC controller configuration file.")
        if int(raw.get("version", 0)) > FILE_VERSION:
            raise ValueError(
                "This file was written by a newer version of the client."
            )

        added: list[str] = []
        for payload in raw.get("configurations") or []:
            entry = ControllerConfiguration.from_dict(payload)
            entry.name = self.unique_name(entry.name)
            self._entries.append(entry)
            added.append(entry.name)
        return added


def default_configuration(device, layout: str = DEFAULT_LAYOUT) -> ControllerConfiguration:
    """A starting configuration for a device that has none.

    Uses the same best-effort default mapping the backend would, so a pad is
    immediately usable and the player edits from something rather than nothing.
    """
    from client.input.keyboard_backend import KEYBOARD_GUID
    from client.input.mapping import default_joystick_mapping

    if device.guid == KEYBOARD_GUID:
        from client.input.keyboard_backend import default_keyboard_mapping

        mapping = default_keyboard_mapping()
    else:
        mapping = default_joystick_mapping(
            device.guid,
            device.name,
            axes=device.axis_count,
            buttons=device.button_count,
            hats=device.hat_count,
        )

    return ControllerConfiguration(
        name=f"{device.display_name()} — {get_layout(layout).name}",
        layout=layout,
        mappings={layout: mapping},
        device_guid=device.guid,
        device_name=device.name,
    )
