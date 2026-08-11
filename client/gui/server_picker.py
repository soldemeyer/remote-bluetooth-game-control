"""Browse for servers and pick one.

Two sources, one list:

  * **This network** -- servers answering the LAN discovery probe.
  * **Internet** -- servers that opted in to being listed on the rendezvous
    broker.

Either way the entry carries only a name and how to reach it. The password is
asked for here and travels no further than the handshake, and a server set to
hidden appears in neither list -- reaching one of those needs its address (or
room code) typed in by hand, which is the whole point of hidden mode.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

_LAN = "lan"
_INTERNET = "internet"


class ServerPicker(QDialog):
    """Lists discoverable servers and collects the password for the chosen one."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        broker_host: str = "",
        broker_port: int = 47900,
        password: str = "",
    ) -> None:
        super().__init__(parent)
        self._broker_host = broker_host
        self._broker_port = broker_port

        self.selection: dict | None = None

        self.setWindowTitle("Find a server")
        self.setMinimumSize(620, 440)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())

        root = QVBoxLayout(self)

        blurb = QLabel(
            "Servers that broadcast their name appear here. A server set to "
            "<b>hidden</b> will not be listed — for those, close this and type the "
            "address (or room code) in directly."
        )
        blurb.setWordWrap(True)
        root.addWidget(blurb)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Server", "Where", "Capacity"])
        self._tree.setRootIsDecorated(False)
        self._tree.itemDoubleClicked.connect(lambda *_: self._accept_if_selected())
        self._tree.currentItemChanged.connect(self._on_selection_changed)
        root.addWidget(self._tree, 1)

        password_row = QHBoxLayout()
        password_row.addWidget(QLabel("Password"))
        self._password = QLineEdit(password)
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("Server password")
        self._password.returnPressed.connect(self._accept_if_selected)
        password_row.addWidget(self._password, 1)
        root.addLayout(password_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        root.addWidget(self._status)

        buttons = QDialogButtonBox()
        rescan = QPushButton("Search again")
        rescan.clicked.connect(self.refresh)
        buttons.addButton(rescan, QDialogButtonBox.ButtonRole.ActionRole)
        self._connect_button = buttons.addButton(
            "Connect", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._connect_button.setEnabled(False)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_selected)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self.refresh()

    # -- population --------------------------------------------------------

    def refresh(self) -> None:
        self._tree.clear()
        self._status.setText("Searching…")
        QApplication.processEvents()

        found = 0
        found += self._add_lan()
        found += self._add_internet()

        if not found:
            self._status.setText(
                "No servers found. Check the server is switched on and set to "
                "broadcast, that you are on the same network, and that UDP "
                "broadcast is not blocked."
            )
        else:
            self._status.setText(f"{found} server(s) found.")

        self._tree.resizeColumnToContents(0)

    def _add_lan(self) -> int:
        import asyncio

        try:
            from server.discovery import discover_servers

            servers = asyncio.run(discover_servers(timeout=1.5))
        except Exception as exc:
            log.debug("LAN discovery failed: %s", exc)
            return 0

        for entry in servers:
            item = QTreeWidgetItem(
                [
                    str(entry.get("name") or entry.get("host", "?")),
                    f"{entry.get('host')}:{entry.get('port')}",
                    _capacity_text(entry),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": _LAN,
                "host": entry.get("host"),
                "port": entry.get("port"),
                "name": entry.get("name", ""),
            })
            self._tree.addTopLevelItem(item)

        return len(servers)

    def _add_internet(self) -> int:
        if not self._broker_host:
            return 0

        from client.net.connect import list_broker_servers

        servers = list_broker_servers(self._broker_host, self._broker_port)
        for entry in servers:
            item = QTreeWidgetItem(
                [str(entry.get("name")), "Internet", _capacity_text(entry)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, {
                "kind": _INTERNET,
                "room": entry.get("room"),
                "name": entry.get("name", ""),
            })
            self._tree.addTopLevelItem(item)

        return len(servers)

    # -- selection ---------------------------------------------------------

    def _on_selection_changed(self, current, _previous) -> None:
        self._connect_button.setEnabled(current is not None)

    def _accept_if_selected(self) -> None:
        item = self._tree.currentItem()
        if item is None:
            self._status.setText("Pick a server first.")
            return

        data = dict(item.data(0, Qt.ItemDataRole.UserRole) or {})
        password = self._password.text()
        if not password:
            self._status.setText("Enter the server password.")
            self._password.setFocus()
            return

        data["password"] = password
        self.selection = data
        self.accept()


def _capacity_text(entry: dict) -> str:
    capacity = entry.get("capacity")
    if capacity in (None, ""):
        return "—"
    in_use = entry.get("in_use")
    if in_use in (None, ""):
        return f"{capacity} slots"
    return f"{in_use} / {capacity} in use"
