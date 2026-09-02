"""Transient and modal feedback: toasts and confirmations.

Both applications currently use `QMessageBox` -- eight calls in the client
alone -- which is unstyled, always modal, and gives no way to distinguish a
destructive confirmation from an informational one. These are the themed
replacements, with the same call shape so the migration is mechanical.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    Qt,
    QTimer,
)
from PySide6.QtWidgets import (
    QDialog,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from common.design.tokens import Motion, Space
from qtui.buttons import DangerButton, PrimaryButton, SecondaryButton
from qtui.theme import pixmap
from qtui.widgets import GlassPanel

__all__ = ["ConfirmDialog", "Toast", "ToastHost"]

#: How long a toast stays up before fading. Long enough to read a sentence,
#: short enough not to sit over the picture.
TOAST_MS = 4200

_KIND_TOKEN = {
    "info": ("info", "info"),
    "success": ("check", "success"),
    "warning": ("alert", "warning"),
    "error": ("alert", "error"),
}


class Toast(GlassPanel):
    """One transient message. Created by :class:`ToastHost`, not directly."""

    def __init__(self, text: str, kind: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(surface="card", parent=parent, shadow=True,
                         padding=Space.MD, spacing=Space.SM)
        icon_name, token = _KIND_TOKEN.get(kind, _KIND_TOKEN["info"])

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(Space.SM)

        glyph = QLabel()
        glyph.setPixmap(pixmap(icon_name, token, 18))
        glyph.setFixedSize(18, 18)
        row.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)

        label = QLabel(text)
        label.setWordWrap(True)
        label.setMaximumWidth(360)
        row.addWidget(label, 1)

        self.add(row)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # Announced to assistive technology; a toast that only exists visually
        # is invisible to anyone using a screen reader.
        self.setAccessibleName(f"{kind}: {text}")


class ToastHost(QWidget):
    """Stacks toasts in a corner of a window.

    A host rather than free-floating windows: separate top-level windows steal
    focus on some platforms, and a game-streaming client taking focus mid-game
    is worse than the message being missed.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(Space.SM)
        self._layout.addStretch(1)
        self._toasts: list[Toast] = []
        parent.installEventFilter(self)
        self._reposition()

    def show_toast(self, text: str, kind: str = "info", timeout: int = TOAST_MS) -> None:
        toast = Toast(text, kind, self)
        self._layout.addWidget(toast, 0, Qt.AlignmentFlag.AlignRight)
        self._toasts.append(toast)
        toast.show()
        self._reposition()
        QTimer.singleShot(timeout, lambda: self._dismiss(toast))

    def _dismiss(self, toast: Toast) -> None:
        if toast not in self._toasts:
            return
        self._toasts.remove(toast)

        # Opacity only. Animating geometry would relayout every frame, and in
        # the client that competes with the decoder for the GIL.
        effect = QGraphicsOpacityEffect(toast)
        toast.setGraphicsEffect(effect)
        fade = QPropertyAnimation(effect, b"opacity", toast)
        fade.setDuration(Motion.NORMAL)
        fade.setStartValue(1.0)
        fade.setEndValue(0.0)
        fade.setEasingCurve(QEasingCurve.Type.InOutCubic)
        fade.finished.connect(toast.deleteLater)
        fade.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)

    def eventFilter(self, watched, event):  # noqa: N802 - Qt naming
        from PySide6.QtCore import QEvent

        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._reposition()
        return False

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        width = min(420, parent.width() - Space.XL * 2)
        height = parent.height() - Space.XL * 2
        if width <= 0 or height <= 0:
            return
        self.setGeometry(parent.width() - width - Space.XL, Space.XL, width, height)
        self.raise_()


class ConfirmDialog(QDialog):
    """A themed confirmation.

    `destructive` is not decoration: it switches the confirming button from the
    accent fill to the danger outline, so "Reset all controllers" cannot be
    mistaken for "Connect" by someone clicking where the primary button usually
    is.
    """

    def __init__(
        self,
        title: str,
        body: str,
        *,
        confirm_text: str = "Confirm",
        cancel_text: str = "Cancel",
        destructive: bool = False,
        icon_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(Space.XL, Space.XL, Space.XL, Space.XL)
        layout.setSpacing(Space.LG)

        head = QHBoxLayout()
        head.setSpacing(Space.MD)
        glyph = QLabel()
        glyph.setPixmap(
            pixmap(icon_name or ("alert" if destructive else "info"),
                   "error" if destructive else "accent-primary", 24)
        )
        glyph.setFixedSize(24, 24)
        head.addWidget(glyph, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(Space.SM)
        heading = QLabel(title)
        heading.setProperty("role", "heading")
        text.addWidget(heading)
        message = QLabel(body)
        message.setWordWrap(True)
        message.setProperty("role", "label")
        text.addWidget(message)
        head.addLayout(text, 1)
        layout.addLayout(head)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = SecondaryButton(cancel_text)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        confirm_cls = DangerButton if destructive else PrimaryButton
        self._confirm = confirm_cls(confirm_text)
        self._confirm.clicked.connect(self.accept)
        buttons.addWidget(self._confirm)
        layout.addLayout(buttons)

        # Cancel is focused first on a destructive dialog. Enter is a reflex
        # after reading a warning, and the reflex should not be the damage.
        (cancel if destructive else self._confirm).setDefault(True)
        (cancel if destructive else self._confirm).setFocus()

    @classmethod
    def ask(cls, parent, title: str, body: str, **kwargs) -> bool:
        """Show it and return whether the user confirmed."""
        return cls(title, body, parent=parent, **kwargs).exec() == QDialog.DialogCode.Accepted
