"""Every user-facing dialog is themed.

`ConfirmDialog` and `Notice` live in `qtui/feedback.py`. `ConfirmDialog` was
written during Stage 2 and then wired into **nothing** for the rest of the
project, while twenty-two `QMessageBox` calls carried on rendering with
OS-default buttons -- the same "the feature looks present and is absent" shape
as the dead `vendor_id`/`advertise_host` parameters elsewhere in this tree.

A `QMessageBox` under the theme is not unstyled: it inherits the dark palette
from the `QMainWindow, QDialog` rule. It just keeps flat grey buttons with
mnemonic underlines and no accent, so it reads as a different application --
which is the exact cohesion problem this work exists to fix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Everything a user can be shown. `qtui/feedback.py` is excluded because it is
#: where the replacements live.
GUI_SOURCES = [
    ROOT / "client" / "gui" / "app.py",
    ROOT / "client" / "gui" / "configurations_dialog.py",
    ROOT / "client" / "gui" / "mapping_dialog.py",
    ROOT / "client" / "gui" / "server_picker.py",
    ROOT / "videoserver" / "gui.py",
]


@pytest.mark.parametrize("path", [p for p in GUI_SOURCES if p.exists()],
                         ids=lambda p: p.name)
def test_no_raw_message_boxes(path: Path) -> None:
    """`QMessageBox` must not come back.

    Named per file rather than counted across the tree, so a reintroduction
    points at the file rather than at a number that went up.
    """
    src = path.read_text(encoding="utf-8")
    assert "QMessageBox" not in src, (
        f"{path.name} uses QMessageBox; use Notice or ConfirmDialog from "
        "qtui.feedback instead"
    )


def test_the_themed_dialogs_are_actually_used() -> None:
    """The failure this guards is a component built and never wired in.

    `ConfirmDialog` sat unused through six stages while the call sites it was
    written to replace went on using `QMessageBox`. Nothing failed, nothing
    warned, and the only symptom was two dialogs in one application looking
    like two applications.
    """
    users = [p for p in GUI_SOURCES
             if p.exists() and "from qtui.feedback import" in p.read_text(encoding="utf-8")]
    assert len(users) >= 4, (
        "the themed dialogs are imported by almost nothing, which is how the "
        f"last one went unused: {[p.name for p in users]}"
    )


def test_destructive_confirmations_are_marked_destructive() -> None:
    """Deleting and replacing a configuration are both unrecoverable.

    `destructive=True` is not decoration: it switches the confirming button to
    the danger styling *and* moves the default and the focus onto Cancel, so
    the Enter reflex after reading a warning is not itself the damage.
    """
    for name in ("configurations_dialog.py", "mapping_dialog.py"):
        src = (ROOT / "client" / "gui" / name).read_text(encoding="utf-8")
        # Paren-matched rather than indentation-matched: these two calls sit at
        # different nesting depths, and a fixed-indent pattern found nothing in
        # the deeper one -- passing vacuously would have been worse than the
        # failure it actually produced.
        asks = []
        for match in re.finditer(r"ConfirmDialog\.ask\(", src):
            depth, i = 1, match.end()
            while i < len(src) and depth:
                depth += (src[i] == "(") - (src[i] == ")")
                i += 1
            asks.append(src[match.end():i])
        assert asks, f"{name} has no ConfirmDialog.ask call"
        for call in asks:
            assert "destructive=True" in call, (
                f"{name}: a confirmation that discards work is not marked "
                "destructive"
            )


def test_notice_mirrors_the_message_box_signature() -> None:
    """The conversion had to be mechanical, or it would reorder arguments.

    `QMessageBox.warning(parent, title, body)` and
    `Notice.warning(parent, title, body)` take the same three positionally, so
    a call site changes by one word and cannot silently swap title for body.
    """
    import inspect

    from qtui.feedback import Notice

    for kind in ("information", "warning", "critical"):
        method = getattr(Notice, kind)
        params = list(inspect.signature(method).parameters)
        assert params == ["parent", "title", "body"], (kind, params)


def test_notice_keeps_the_text_selectable() -> None:
    """Several of these bodies are exception text someone needs to paste.

    `QMessageBox` allows selection by default; a themed replacement that did
    not would quietly take that away.
    """
    src = (ROOT / "qtui" / "feedback.py").read_text(encoding="utf-8")
    assert "TextSelectableByMouse" in src
