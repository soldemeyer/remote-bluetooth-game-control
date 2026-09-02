"""The product's status language, as a Qt widget.

One vocabulary in all three applications. Before this there were three: the
client showed "Not connected" in a grey `QLabel`, the video server built a
monospace summary string, and the web GUI used coloured dots -- three ways of
saying the same six things, none of which looked like the others.

**Colour is never the only signal.** Every state carries an icon and a word as
well, because a coloured dot alone excludes anyone who cannot distinguish the
colours and reads as decoration to everyone glancing at it.
"""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from common.design.tokens import Space, Type
from qtui.theme import icon, pixmap

__all__ = ["Status", "StatusBadge"]


class Status(Enum):
    """What a connection or pipeline is doing.

    The tuple is ``(label, colour token, icon name)``. Keeping the three
    together is what stops a new state being added with a colour and no icon.
    """

    IDLE = ("Idle", "text-muted", "power")
    CONNECTING = ("Connecting", "warning", "refresh")
    CONNECTED = ("Connected", "success", "link")
    STREAMING = ("Streaming", "success", "play")
    RECONNECTING = ("Reconnecting", "warning", "refresh")
    DISCONNECTED = ("Disconnected", "text-muted", "link-off")
    ERROR = ("Error", "error", "alert")

    @property
    def label(self) -> str:
        return self.value[0]

    @property
    def token(self) -> str:
        return self.value[1]

    @property
    def icon_name(self) -> str:
        return self.value[2]


class StatusBadge(QWidget):
    """Icon, state and an optional detail, on one line.

    Deliberately not a `QLabel` with rich text: the icon has to be recoloured
    per state, and rebuilding an HTML string on every status tick would
    re-layout the label several times a second for no reason. The icon is a
    cached pixmap and only the text changes.
    """

    def __init__(
        self,
        status: Status = Status.IDLE,
        detail: str = "",
        *,
        icon_size: int = 16,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # Deliberately *not* `status`: `set_status` skips the icon when the
        # state is unchanged, so seeding this with the initial value means the
        # first paint has no icon at all -- a badge that only grows one once
        # the state happens to change.
        self._status: Status | None = None
        self._icon_size = icon_size

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Space.SM)

        self._icon = QLabel()
        self._icon.setFixedSize(icon_size, icon_size)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel()
        self._label.setProperty("role", "label")

        self._detail = QLabel()
        self._detail.setProperty("role", "meta")

        layout.addWidget(self._icon)
        layout.addWidget(self._label)
        layout.addWidget(self._detail)
        layout.addStretch(1)

        self.set_status(status, detail)

    # -- state -------------------------------------------------------------

    def set_status(self, status: Status, detail: str = "") -> None:
        """Update the badge. Cheap enough to call on every status tick.

        Every write is guarded by a comparison: these are driven from polling
        timers, and setting a `QLabel` to the text it already has still costs a
        relayout.
        """
        if status is not self._status:
            self._status = status
            self._icon.setPixmap(
                pixmap(status.icon_name, status.token, self._icon_size)
            )
            self._label.setStyleSheet(f"color: {self._colour(status)};")
        if self._label.text() != status.label:
            self._label.setText(status.label)
        if self._detail.text() != detail:
            self._detail.setText(detail)
            self._detail.setVisible(bool(detail))

        self.setAccessibleName(f"{status.label} {detail}".strip())

    @property
    def status(self) -> Status | None:
        return self._status

    @staticmethod
    def _colour(status: Status) -> str:
        from common.design.tokens import palette

        return palette[status.token].qss


def status_icon(status: Status, size: int = 16):
    """The icon for a state, for callers that want it without the badge."""
    return icon(status.icon_name, status.token, size)
