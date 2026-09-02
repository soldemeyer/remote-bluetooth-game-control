"""Where to connect, how, and as whom.

The four connection modes and the fields each one needs. Which rows are
visible is decided by the window's `_on_mode_changed`, which is why the two
row containers are exposed by name.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from client import config as client_config

__all__ = ["ConnectionPanel"]


def _wrap(layout) -> QWidget:
    """A layout as a widget, so a QFormLayout row can hold several controls."""
    holder = QWidget()
    holder.setLayout(layout)
    return holder


class ConnectionPanel(QGroupBox):
    """The connection group.

    `window` supplies the handlers, exactly as they were before the move.
    """

    def __init__(self, window, parent=None) -> None:
        super().__init__("Connection", parent)
        outer = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.form = form

        # Every entry names one transport. There is deliberately no "Auto":
        # it tried direct then hole-punch, which meant a failed connection could
        # not be attributed to either path -- the player could not tell whether
        # the address was wrong or the broker was down. Choosing the transport
        # makes the failure legible.
        self.mode = QComboBox()
        self.mode.addItem("On this network (LAN / VPN)", "direct")
        self.mode.addItem("Through a tunnel or port forward", "tunnel")
        self.mode.addItem("Over the Internet (hole-punch)", "punch")
        self.mode.addItem("Over the Internet (relay via broker)", "relay")
        self.mode.setItemData(
            1,
            "A public address that forwards to the server -- an frp UDP proxy, "
            "a router port forward, or a mesh VPN such as Tailscale. The "
            "lowest-latency way across the internet, because nothing bounces "
            "off a third machine.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.mode.setItemData(
            2,
            "Connects the two machines directly by punching through both NATs. "
            "Falls back to relaying by itself if that fails.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.mode.setItemData(
            3,
            "Sends everything through the broker. Slower than hole-punch, but "
            "it works on networks where punching cannot -- and it skips the "
            "~10 s of probing that is guaranteed to fail there.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.mode.currentIndexChanged.connect(window._on_mode_changed)
        form.addRow("Connect:", self.mode)

        # Servers found for the selected mode, plus a Custom row for a server
        # that is hidden or otherwise not announcing itself.
        server_row = QHBoxLayout()
        self.server_list = QComboBox()
        self.server_list.setMinimumWidth(280)
        self.server_list.currentIndexChanged.connect(window._on_server_selected)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(window._on_discover)
        server_row.addWidget(self.server_list, 1)
        server_row.addWidget(self.search_button)
        form.addRow("Server:", _wrap(server_row))

        host_row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("Server address, e.g. 192.168.1.50")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(client_config.DEFAULT_PORT)
        host_row.addWidget(self.host, 1)
        host_row.addWidget(QLabel("Port:"))
        host_row.addWidget(self.port)
        self.host_row = _wrap(host_row)
        form.addRow("Address:", self.host_row)

        punch_row = QHBoxLayout()
        self.room = QLineEdit()
        # NOT the server name. The broker keys rooms by this code alone
        # (`rendezvous/broker.py` -- `message.get("room")`); the name is a
        # cosmetic label in the public listing and matches nothing. The old
        # placeholder said "Server name or room code" and was followed
        # literally, which fails with no diagnosis on either side.
        self.room.setPlaceholderText("Room code from the server")
        self.room.setToolTip(
            "The room code set on the server, under Visibility. Not the "
            "server's name -- the broker matches on the code alone."
        )
        self.broker = QLineEdit()
        self.broker.setPlaceholderText("Broker address")
        punch_row.addWidget(self.room, 1)
        punch_row.addWidget(QLabel("Broker:"))
        punch_row.addWidget(self.broker, 1)
        self.punch_row = _wrap(punch_row)
        form.addRow("Rendezvous:", self.punch_row)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Server password")
        self.save_password = QCheckBox("Remember")
        password_row = QHBoxLayout()
        password_row.addWidget(self.password, 1)
        password_row.addWidget(self.save_password)
        form.addRow("Password:", _wrap(password_row))

        self.client_name = QLineEdit()
        form.addRow("This PC:", self.client_name)

        outer.addLayout(form)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(window._on_connect_clicked)
        self.connect_button.setDefault(True)

        # Enabled only once the server tells us a source exists, so the button
        # never offers something that cannot happen.
        self.video_button = QPushButton("Watch stream")
        self.video_button.setEnabled(False)
        self.video_button.setToolTip(
            "Open the video stream. F11 for fullscreen, L for the latency overlay."
        )
        self.video_button.clicked.connect(window._on_watch_clicked)

        # The state text is in the header badge now, and the audio controls on
        # the bar over the picture. Both used to sit on this row, which put the
        # connection state in the least likely place to look for it -- after
        # the volume slider -- and hid the volume behind a panel the player
        # closes once the session is running.
        window._build_audio_controls()

        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.video_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)
