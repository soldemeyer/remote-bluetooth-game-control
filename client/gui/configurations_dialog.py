"""Manage the configurations the player has saved.

This replaced a bare pair of "Save…" / "Load…" buttons, which exported *every*
configuration to one file and imported files back — useful for moving a whole
setup between machines, and no use at all for the ordinary job of "rename that
one, delete this one, send a friend the other". There was no way to see what
existed, let alone act on one of them.

Built-ins are deliberately absent from the list. They are regenerated from a
rule on every launch, so there is nothing here to manage: deleting one would
bring it back, and editing one is done by opening it and choosing "Save as…",
which produces a custom configuration that *does* appear here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from client.gui.controller_layouts import get_layout
from client.gui.controller_presets import materialise
from client.gui.mapping_dialog import MappingDialog
from common.design.tokens import Space
from qtui.feedback import ConfirmDialog, Notice

log = logging.getLogger(__name__)

_FILE_FILTER = "Controller configurations (*.json);;All files (*)"


class ConfigurationsDialog(QDialog):
    """List, edit, delete, export and import saved configurations."""

    def __init__(
        self,
        store,
        backend,
        devices,
        parent: QWidget | None = None,
        *,
        on_changed=None,
        pad_bindings=None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._backend = backend
        self._devices = list(devices or [])
        self._on_changed = on_changed
        self._pad_bindings = pad_bindings or (lambda device: None)

        self.setWindowTitle("Manage custom configurations")
        self.setMinimumSize(560, 420)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        self._build_ui()
        self._reload()

    # -- ui ----------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hint = QLabel(
            "Configurations you have saved. Built-in ones are not listed — "
            "they are rebuilt each launch, so open one from the Controllers "
            "table and use Save as… to make an editable copy."
        )
        hint.setWordWrap(True)
        hint.setProperty("role", "muted")
        root.addWidget(hint)

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.currentItemChanged.connect(lambda *_: self._update_buttons())
        self._list.itemDoubleClicked.connect(lambda *_: self._on_edit())
        root.addWidget(self._list, 1)

        self._empty = QLabel(
            "No custom configurations yet.\n\n"
            "Pick a gamepad in the Controllers table, press Configure…, "
            "and save your bindings under a name."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setProperty("role", "muted")
        self._empty.setContentsMargins(Space.XL, Space.XL, Space.XL, Space.XL)
        root.addWidget(self._empty, 1)

        row = QHBoxLayout()
        root.addLayout(row)

        self._edit_button = QPushButton("Edit…")
        self._edit_button.setToolTip("Open this configuration in the binding editor.")
        self._edit_button.clicked.connect(self._on_edit)
        row.addWidget(self._edit_button)

        self._rename_button = QPushButton("Rename…")
        self._rename_button.clicked.connect(self._on_rename)
        row.addWidget(self._rename_button)

        self._delete_button = QPushButton("Delete")
        self._delete_button.clicked.connect(self._on_delete)
        row.addWidget(self._delete_button)

        self._export_button = QPushButton("Export…")
        self._export_button.setToolTip("Write this configuration to a file to share.")
        self._export_button.clicked.connect(self._on_export)
        row.addWidget(self._export_button)

        row.addStretch(1)

        import_button = QPushButton("Import…")
        import_button.setToolTip("Add configurations from a file.")
        import_button.clicked.connect(self._on_import)
        row.addWidget(import_button)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        close.setDefault(True)
        row.addWidget(close)

    # -- list --------------------------------------------------------------

    def _custom(self) -> list:
        return [entry for entry in self._store if not entry.builtin]

    def _reload(self, select: str | None = None) -> None:
        self._list.clear()

        for entry in self._custom():
            item = QListWidgetItem(self._describe(entry))
            item.setData(Qt.ItemDataRole.UserRole, entry.name)
            self._list.addItem(item)
            if entry.name == select:
                self._list.setCurrentItem(item)

        has_any = self._list.count() > 0
        self._list.setVisible(has_any)
        self._empty.setVisible(not has_any)

        if has_any and self._list.currentItem() is None:
            self._list.setCurrentRow(0)
        self._update_buttons()

    def _describe(self, entry) -> str:
        types = [get_layout(key).name for key in entry.configured_layouts()]
        detail = ", ".join(types) if types else "no bindings yet"

        line = f"{entry.name}\n    {detail}"
        if entry.device_name:
            line += f"  ·  built for {entry.device_name}"
        if entry.approximate:
            line += "  ·  approximate"
        return line

    def _selected(self):
        item = self._list.currentItem()
        if item is None:
            return None
        return self._store.get(item.data(Qt.ItemDataRole.UserRole))

    def _update_buttons(self) -> None:
        enabled = self._selected() is not None
        for button in (
            self._edit_button,
            self._rename_button,
            self._delete_button,
            self._export_button,
        ):
            button.setEnabled(enabled)

    def _changed(self, select: str | None = None) -> None:
        if self._on_changed is not None:
            self._on_changed()
        self._reload(select)

    # -- actions -----------------------------------------------------------

    def _on_edit(self) -> None:
        entry = self._selected()
        if entry is None:
            return

        device = self._device_for(entry)
        if device is None:
            return

        borrowed = False
        try:
            device = self._backend.acquire(device.instance_id)
            borrowed = True
        except Exception as exc:
            Notice.warning(self, "Controller unavailable", str(exc))
            return

        try:
            working = materialise(
                entry, device, self._pad_bindings(device), keep_builtin=True
            )
            dialog = MappingDialog(
                self._backend, device, working, self, store=self._store
            )
            accepted = dialog.exec()

            if accepted or dialog.created_copy:
                self._changed(dialog.configuration.name)
        finally:
            if borrowed:
                try:
                    self._backend.release(device.instance_id)
                except Exception:
                    log.debug("Could not release %s", device.instance_id, exc_info=True)

    def _device_for(self, entry):
        """Which pad to bind against.

        The editor reads a live device -- press-to-bind and the preview both
        need one -- so editing is only possible with something plugged in. Prefer
        the pad the configuration was built for; it is the one whose buttons are
        where the bindings expect them.
        """
        if not self._devices:
            Notice.information(
                self,
                "No controller connected",
                "Editing bindings needs a controller to read, so you can press "
                "the buttons you want to bind.\n\n"
                "Connect one and press 'Refresh gamepad list'.",
            )
            return None

        for device in self._devices:
            if entry.device_guid and device.guid == entry.device_guid:
                return device

        if len(self._devices) == 1:
            return self._devices[0]

        names = [device.display_name() for device in self._devices]
        choice, ok = QInputDialog.getItem(
            self,
            "Which controller?",
            f"'{entry.name}' was built for "
            f"{entry.device_name or 'a controller that is not connected'}.\n"
            f"Bind against:",
            names,
            0,
            False,
        )
        if not ok:
            return None
        return self._devices[names.index(choice)]

    def _on_rename(self) -> None:
        entry = self._selected()
        if entry is None:
            return

        name, ok = QInputDialog.getText(
            self, "Rename configuration", "Name", text=entry.name
        )
        if not ok:
            return
        name = name.strip()
        if not name or name == entry.name:
            return

        clash = self._store.get(name)
        if clash is not None:
            Notice.warning(
                self,
                "Name in use",
                f"'{name}' already exists. Choose a different name.",
            )
            return

        # Slots reference configurations by name, so renaming has to carry
        # those references with it or the slot silently falls back to its
        # default bindings.
        old = entry.name
        entry.name = name
        self._changed(name)
        log.info("Renamed configuration %r to %r", old, name)

    def _on_delete(self) -> None:
        entry = self._selected()
        if entry is None:
            return

        # Destructive: the confirming button takes the danger styling and
        # Cancel holds the focus, so Enter after reading the warning is not
        # itself the deletion.
        if not ConfirmDialog.ask(
            self,
            "Delete configuration?",
            f"Delete '{entry.name}'? Any controller slot using it falls back "
            "to the default bindings for its gamepad. This cannot be undone.",
            confirm_text="Delete",
            destructive=True,
        ):
            return

        self._store.remove(entry.name)
        self._changed()

    def _on_export(self) -> None:
        entry = self._selected()
        if entry is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export configuration",
            f"{_safe_filename(entry.name)}.json",
            _FILE_FILTER,
        )
        if not path:
            return

        try:
            self._store.export_to_file(Path(path), names=[entry.name])
        except OSError as exc:
            Notice.warning(self, "Could not export", str(exc))
            return

        Notice.information(
            self, "Exported", f"Wrote '{entry.name}' to\n{path}"
        )

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import configurations", "", _FILE_FILTER
        )
        if not path:
            return

        try:
            added = self._store.import_from_file(Path(path))
        except (OSError, ValueError) as exc:
            Notice.warning(self, "Could not import", str(exc))
            return

        if not added:
            Notice.information(
                self, "Nothing imported", "That file contained no configurations."
            )
            return

        self._changed(added[0])
        Notice.information(
            self,
            "Imported",
            "Added:\n  " + "\n  ".join(added)
            + (
                "\n\nNames that clashed were given a suffix rather than "
                "replacing what you already had."
                if any(name.endswith(")") for name in added)
                else ""
            ),
        )


def _safe_filename(name: str) -> str:
    """A filename that will not upset Windows."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name)
    return cleaned.strip().rstrip(".") or "configuration"
