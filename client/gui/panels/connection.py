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
from qtui.widgets import (
    ButtonSpinner,
    NoWheelComboBox,
    NoWheelSpinBox,
    cap_combo_width,
)

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
        self.mode = NoWheelComboBox()
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
        self.server_list = NoWheelComboBox()
        self.server_list.setMinimumWidth(280)
        # A *cap* as well as a floor, and only here: every other dropdown in
        # this window holds strings we wrote, so their width is known. This one
        # holds "<name> - <address> (n/m in use)" for a server somebody else
        # named, and without a cap one long name widens the whole card.
        cap_combo_width(self.server_list, 16)
        self.server_list.currentIndexChanged.connect(window._on_server_selected)
        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(window._on_discover)

        # **An animated indicator, not only a greyed button.** A search waits
        # 1.5 s for LAN replies, or a round trip for a broker, and a disabled
        # button says "not now" rather than "working on it" -- so it gets
        # pressed again.
        #
        # It spins *on* the button. A bar beside it was tried and is worse: a
        # bar is a measurement and there is nothing being measured, and it
        # pushed the controls around it sideways when it appeared.
        self.search_spinner = ButtonSpinner(self.search_button)

        server_row.addWidget(self.server_list, 1)
        server_row.addWidget(self.search_button)
        form.addRow("Server:", _wrap(server_row))

        host_row = QHBoxLayout()
        self.host = QLineEdit()
        self.host.setPlaceholderText("Server address, e.g. 192.168.1.50")
        self.port = NoWheelSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(client_config.DEFAULT_PORT)
        host_row.addWidget(self.host, 1)
        host_row.addWidget(QLabel("Port:"))
        host_row.addWidget(self.port)
        self.host_row = _wrap(host_row)
        form.addRow("Address:", self.host_row)

        # **The server's own words, and only those.** Its Visibility card
        # calls these "Room code" and "Rendezvous broker"; this form labelled
        # the row "Rendezvous:" with the *room code* box under it and called
        # the second one "Broker:", so the one word the two screens shared sat
        # in front of the wrong field.
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
        # The same placeholder the server's own field carries.
        self.broker.setPlaceholderText("host:port")
        self.broker.setToolTip(
            "The rendezvous broker set on the server, under Visibility. Host "
            "and port, e.g. broker.example.com:47900."
        )
        # The broker takes the larger share: a room code is a short word and
        # this is a host and a port, so an even split showed the address with
        # its front cut off -- which is the half that says which broker it is.
        punch_row.addWidget(self.room, 1)
        punch_row.addWidget(QLabel("Rendezvous broker:"))
        punch_row.addWidget(self.broker, 2)
        self.punch_row = _wrap(punch_row)
        form.addRow("Room code:", self.punch_row)

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

        # **Connect and Watch video are header actions, not card controls.**
        # They were the last row of this card, which meant the two things
        # somebody reaches for while a session is running were inside a panel
        # that folds away -- and this card folds first, because everything else
        # in it is set once. The header is on screen whatever the drawer shows.
        #
        # The state text went the same way earlier, into the header badge, and
        # the audio controls onto the bar over the picture.
        window._build_audio_controls()

    def set_searching(self, searching: bool) -> None:
        """Show that a search is running, or that it has finished.

        The label carries it as well as the spinner: a greyed button says "not
        now" and not "working on it", and the two are what somebody deciding
        whether to press again is trying to tell apart.
        """
        self.search_button.setEnabled(not searching)
        self.search_button.setText("Searching…" if searching else "Search")
        if searching:
            self.search_spinner.start()
        else:
            self.search_spinner.stop()
