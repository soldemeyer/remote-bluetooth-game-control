"""The pipeline strip: four stages, left to right, with live state.

This window's job is answering one question -- *where has it stopped?* -- and
the previous answer was a monospace summary line that reported every stage at
once and highlighted none of them. Capture, encode, network and viewers fail in
completely different ways, and the fix for each is in a different place.

So each stage is a card carrying its own value, its own detail and its own
health colour, and the arrows between them say which way the data goes. A stage
that is fine is quiet; the one that is wrong is the coloured one.

**Health is never colour alone.** Each card's state also changes its wording --
"waiting", "no device", "0 fps" -- because a red card that still reads "1080p60"
tells a colour-blind operator nothing, and tells everyone else less than the
word would.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from common.design.tokens import Space, Type
from qtui.theme import pixmap, qcolor
from qtui.widgets import GlassPanel

__all__ = ["PipelineStrip", "StageCard"]

#: The stages, in the order the data moves through them.
STAGES = (
    ("capture", "Capture", "capture"),
    ("encode", "Encode", "activity"),
    ("network", "Network", "wifi"),
    ("viewers", "Viewers", "monitor"),
)


class StageCard(GlassPanel):
    """One stage: a title, a headline value, and a line of detail."""

    def __init__(self, title: str, icon_name: str, parent: QWidget | None = None) -> None:
        super().__init__(surface="card", parent=parent, shadow=False,
                         padding=Space.MD, spacing=Space.XS)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(Space.SM)

        self._glyph = QLabel()
        self._icon_name = icon_name
        self._glyph.setFixedSize(16, 16)
        self._glyph.setPixmap(pixmap(icon_name, "text-muted", 16))
        head.addWidget(self._glyph)

        self._title = QLabel(title.upper())
        self._title.setProperty("role", "meta")
        head.addWidget(self._title)
        head.addStretch(1)

        self._value = QLabel("—")
        self._value.setProperty("role", "heading")
        font = self._value.font()
        font.setFamilies(list(Type.FAMILIES_MONO))
        self._value.setFont(font)

        self._detail = QLabel("")
        self._detail.setProperty("role", "muted")
        self._detail.setWordWrap(False)

        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._value)
        body.addWidget(self._detail)

        self.add(head)
        self.add(body)
        self._token = ""

    def set_state(self, value: str, detail: str = "", token: str = "text-primary") -> None:
        """Update the card. Guarded -- this runs on the window's status tick."""
        if self._value.text() != value:
            self._value.setText(value)
        if self._detail.text() != detail:
            self._detail.setText(detail)
        if token != self._token:
            self._token = token
            self._value.setStyleSheet(f"color: {qcolor(token).name()};")
            # The icon follows the health too, so the card reads at a glance
            # from its left edge rather than only from the number.
            self._glyph.setPixmap(pixmap(self._icon_name, token, 16))

    def retheme(self) -> None:
        """Re-resolve the cached colours after a theme change."""
        token, self._token = self._token, ""
        self.set_state(self._value.text(), self._detail.text(), token or "text-primary")


class PipelineStrip(QWidget):
    """The four stage cards with arrows between them."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Space.SM)

        self.cards: dict[str, StageCard] = {}
        self._arrows: list[QLabel] = []
        for index, (key, title, icon_name) in enumerate(STAGES):
            if index:
                arrow = QLabel()
                arrow.setPixmap(pixmap("chevron-right", "text-muted", 14))
                arrow.setFixedSize(14, 14)
                row.addWidget(arrow, 0, Qt.AlignmentFlag.AlignVCenter)
                self._arrows.append(arrow)
            card = StageCard(title, icon_name)
            self.cards[key] = card
            row.addWidget(card, 1)

    def retheme(self) -> None:
        for card in self.cards.values():
            card.retheme()
        for arrow in self._arrows:
            arrow.setPixmap(pixmap("chevron-right", "text-muted", 14))

    # -- the readings ------------------------------------------------------

    def update_from(self, status: dict | None, *, streaming: bool) -> None:
        """Translate one `VideoServerApp.status()` into four cards.

        Each stage decides its own health from what it can actually see, and
        says so in words as well as colour.
        """
        if status is None or not streaming:
            for card in self.cards.values():
                card.set_state("—", "not streaming", "text-muted")
            return

        # **A stage is only healthy if something is actually reaching it.**
        # Without this, a capture card that opens and then sends nothing left
        # encode reporting a green "0.0 ms" and network a green "0.0 Mbps" --
        # three cards saying fine and one saying broken, when the truthful
        # reading is one broken and two waiting on it. A green stage that is
        # merely idle is the confidently-wrong display this window is meant to
        # replace.
        flowing = self._flowing(status)
        self._set_capture(status)
        self._set_encode(status, flowing)
        self._set_network(status, flowing)
        self._set_viewers(status)

    @staticmethod
    def _flowing(status: dict) -> bool:
        """Whether frames are actually coming out of capture."""
        return bool(
            status.get("width") and status.get("height") and (status.get("fps") or 0) >= 1
        )

    def _set_capture(self, status: dict) -> None:
        width, height = status.get("width", 0), status.get("height", 0)
        fps = status.get("fps", 0.0) or 0.0
        # `streaming` means frames are flowing; a device that opened and then
        # sat there is the failure this card exists to show.
        if not width or not height:
            self.cards["capture"].set_state("no signal", "device opened, no frames", "error")
        elif fps < 1.0:
            self.cards["capture"].set_state("0 fps", f"{width}x{height}", "error")
        else:
            token = "success" if fps >= 24 else "warning"
            self.cards["capture"].set_state(
                f"{width}x{height}", f"{fps:.0f} fps", token
            )

    def _set_encode(self, status: dict, flowing: bool) -> None:
        encoder = status.get("encoder") or ""
        p50 = status.get("encode_p50_ms", 0.0) or 0.0
        p99 = status.get("encode_p99_ms", 0.0) or 0.0
        if not encoder:
            self.cards["encode"].set_state("starting", "no encoder yet", "warning")
            return
        if not flowing:
            self.cards["encode"].set_state("waiting", f"{encoder} - no frames in", "text-muted")
            return
        # Against the frame budget, not an absolute: 16 ms is a whole frame at
        # 60 fps, and an encoder taking that long is the bottleneck.
        token = "success" if p99 < 8 else "warning" if p99 < 16 else "error"
        self.cards["encode"].set_state(
            f"{p50:.1f} ms", f"{encoder} - p99 {p99:.1f}", token
        )

    def _set_network(self, status: dict, flowing: bool) -> None:
        kbps = status.get("bitrate_kbps", 0) or 0
        dropped = status.get("dropped", 0) or 0
        if not flowing:
            self.cards["network"].set_state("waiting", "nothing to send", "text-muted")
            return
        token = "success" if not dropped else "warning"
        detail = "0 dropped" if not dropped else f"{dropped} dropped"
        self.cards["network"].set_state(f"{kbps / 1000:.1f} Mbps", detail, token)

    def _set_viewers(self, status: dict) -> None:
        clients = status.get("clients", 0) or 0
        if not clients:
            # Not an error: a source with nobody watching is doing its job.
            self.cards["viewers"].set_state("0", "nobody watching", "text-muted")
        else:
            self.cards["viewers"].set_state(
                str(clients), "watching" if clients == 1 else "watching", "success"
            )
