"""Drawing labels in the real window, and refusing to.

Driven through an actual ``VideoWindow`` and a real paint, because the thing
worth pinning is not that a function returns a rectangle -- it is *where the
name lands*, and that it lands inside the piece of picture it belongs to. A
label drawn a viewport away is not a degraded picture, it is somebody else's
name over your character.

The refusals matter as much: nothing during a camera move, nothing when the
player has not asked, nothing when the anchor falls outside every piece this
client is drawing.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect              # noqa: E402
from PySide6.QtGui import QImage, QPainter    # noqa: E402
from PySide6.QtWidgets import QWidget         # noqa: E402

from client.gui.player_labels import LabelStore   # noqa: E402
from client.gui.video_window import VideoWindow   # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])

class FakeDecoder:
    """Enough of a decoder for the window. Touches no PyAV."""

    upscaler_fault = ""

    def __init__(self):
        self._listener = None
        self.viewport = (0, 0)

    def set_frame_listener(self, listener):
        self._listener = listener

    def set_viewport(self, width, height):
        self.viewport = (width, height)

    def latest(self):
        return None

    @property
    def version(self):
        return 0

    def set_upscaler(self, _):
        return None

    def set_overlay(self, _):
        return None


class FakeReceiver:
    """Enough of a VideoReceiver for the window, including the OSD.

    `osd_lines` reads several of these, and the GPU overlay builds them on
    every publish -- so a fake missing one fails inside `_publish_overlay`
    rather than anywhere near what is under test.
    """

    clock_locked = False
    clock_offset_ns = 0
    connection_mode = "direct"
    slices_received = 0
    frames_arrived = 0
    frames_decoded = 0
    decode_errors = 0
    audio_underruns = 0

    def __init__(self):
        from common.timing import LatencyStats

        self.present_stats = LatencyStats()
        self.paint_stats = LatencyStats()
        self.pickup_stats = LatencyStats()

    def snapshot(self):
        return {}


@pytest.fixture
def window(qt_app):
    host = QWidget()
    win = VideoWindow(FakeDecoder(), FakeReceiver(), parent=host)
    win.resize(400, 300)
    yield win
    win.release()
    win.close()
    host.close()


def _store(*labels):
    """Ingested against the real clock, because `_draw_labels` reads it.

    Stamping these in the past is the obvious thing to do and every label is
    then already expired by the time the paint runs -- which looks exactly
    like the drawing being broken.
    """
    from common.timing import now_ns

    # No fade: these are about where a name is drawn, and a name that has only
    # just appeared would otherwise be at the very start of fading in.
    store = LabelStore(fade_ns=0)
    store.ingest("QUAD_4" if labels and labels[0].get("region") else "FULL",
                 list(labels), now_ns())
    return store


def _label(**kwargs):
    base = {
        "player_id": 2, "track_id": 1, "name": "Bo", "region": "",
        "x": 0.4, "y": 0.3, "w": 0.1, "h": 0.2, "confidence": 0.9,
    }
    base.update(kwargs)
    return base


def _paint(window, *, views=None):
    """Paint into an image and hand back the pixels.

    The window is painted off-screen rather than shown: what is being checked
    is where `drawImage` and `drawText` put things, and that is the same
    either way.
    """
    window._drawn_views = views if views is not None else [
        ((0.0, 0.0, 1.0, 1.0), window.rect())
    ]
    image = QImage(window.size(), QImage.Format.Format_RGB888)
    image.fill(0)
    painter = QPainter(image)
    window._draw_labels(painter)
    painter.end()
    return image


def _ink_columns(image):
    """Which columns have anything drawn in them."""
    columns = set()
    for x in range(image.width()):
        for y in range(image.height()):
            if image.pixel(x, y) & 0xFFFFFF:
                columns.add(x)
                break
    return columns


class TestDrawing:
    def test_a_label_is_drawn(self, window):
        window.set_labels(_store(_label()))
        assert _ink_columns(_paint(window)), "nothing was drawn"

    def test_no_store_draws_nothing(self, window):
        """The ordinary case: a player who has not asked for labels has no
        store, and the paint path returns on its first test."""
        assert not _ink_columns(_paint(window))

    def test_an_empty_store_draws_nothing(self, window):
        window.set_labels(LabelStore())
        assert not _ink_columns(_paint(window))

    def test_it_lands_above_the_character(self, window):
        """The anchor is the top middle of the box and the name floats above
        it -- over their head, not over their feet."""
        window.set_labels(_store(_label(x=0.4, y=0.5, w=0.2, h=0.3)))
        image = _paint(window)
        rows = [
            y for y in range(image.height())
            if any(image.pixel(x, y) & 0xFFFFFF for x in range(image.width()))
        ]
        assert rows, "nothing was drawn"
        # y = 0.5 of a 300px window is 150; the label sits above it.
        assert max(rows) <= 150

    def test_it_follows_the_character_across_the_picture(self, window):
        left = _paint_at(window, 0.2)
        right = _paint_at(window, 0.8)
        assert min(right) > max(left), "the label did not move with the entity"


    def test_a_pointer_runs_from_the_name_to_the_character(self, window):
        """Asked for so it is easier to tell which character a name goes
        with. Painted on a light background, because the bubble is a
        translucent dark scrim and is invisible over black."""
        from PySide6.QtGui import QColor

        from client.gui.player_labels import TAIL_HEIGHT

        window.set_labels(_store(_label(x=0.5, y=0.5, w=0.0, h=0.2)))
        window._drawn_views = [((0.0, 0.0, 1.0, 1.0), window.rect())]
        image = QImage(window.size(), QImage.Format.Format_RGB888)
        background = QColor(220, 220, 220)
        image.fill(background)
        painter = QPainter(image)
        window._draw_labels(painter)
        painter.end()

        def shaded(x, y):
            return image.pixelColor(x, y).lightness() < background.lightness() - 20

        # The character's top is at (200, 150); the pointer spans the gap
        # between the bubble and it.
        assert shaded(200, 150 - 2), "no pointer just above the character"
        assert shaded(200, 150 - TAIL_HEIGHT + 1), "the pointer does not reach the bubble"
        assert not shaded(200 - 12, 150 - 2), "the pointer is not narrowing to a tip"
        assert not shaded(200, 150 + 3), "drawn over the character"

def _paint_at(window, x):
    window.set_labels(_store(_label(x=x, w=0.0)))
    return _ink_columns(_paint(window))


class TestViewport:
    """A client drawing two pieces of a split screen."""

    @staticmethod
    def _two_views(window):
        return [
            ((0.0, 0.0, 0.5, 1.0), QRect(0, 0, 200, 300)),
            ((0.5, 0.0, 0.5, 1.0), QRect(200, 0, 200, 300)),
        ]

    def test_a_label_lands_in_the_piece_its_anchor_is_in(self, window):
        window.set_labels(_store(_label(x=0.7, y=0.3, w=0.0)))
        columns = _ink_columns(_paint(window, views=self._two_views(window)))
        assert columns, "nothing was drawn"
        assert min(columns) >= 200, "player's label landed in the wrong half"

    def test_and_the_other_half_for_the_other_anchor(self, window):
        window.set_labels(_store(_label(x=0.2, y=0.3, w=0.0)))
        columns = _ink_columns(_paint(window, views=self._two_views(window)))
        assert max(columns) < 200

    def test_an_anchor_outside_every_piece_draws_nothing(self, window):
        """A client holding one quadrant is sent labels only for what it can
        see -- but if one ever arrives for somewhere it is not drawing, the
        answer is nothing, never a clamp into the nearest piece."""
        views = [((0.0, 0.0, 0.5, 0.5), QRect(0, 0, 200, 150))]
        window.set_labels(_store(_label(x=0.9, y=0.9, w=0.0)))
        assert not _ink_columns(_paint(window, views=views))

    def test_it_is_drawn_once_not_in_every_piece(self, window):
        """An entity straddling a seam belongs to the viewport its anchor is
        in. Drawing it in both would show the same name twice."""
        views = [
            ((0.0, 0.0, 1.0, 1.0), QRect(0, 0, 200, 300)),
            ((0.0, 0.0, 1.0, 1.0), QRect(200, 0, 200, 300)),
        ]
        window.set_labels(_store(_label(x=0.5, y=0.3, w=0.0)))
        columns = _ink_columns(_paint(window, views=views))
        assert columns and max(columns) < 200

    def test_a_label_at_the_edge_stays_inside_its_own_piece(self, window):
        """Clamped into the piece, not into the widget: a name pushed out of
        its own viewport would land over the neighbour's picture."""
        views = [((0.5, 0.0, 0.5, 1.0), QRect(200, 0, 200, 300))]
        window.set_labels(_store(_label(x=0.5, y=0.0, w=0.0)))
        columns = _ink_columns(_paint(window, views=views))
        assert columns, "nothing was drawn"
        assert min(columns) >= 200, "a label escaped its own viewport"


class TestRefusals:
    def test_nothing_is_drawn_during_a_camera_move(self, window):
        """The picture is then a sub-rectangle of a *union* of two views and
        the window is never told what that union is -- so there is no
        transform, only a plausible-looking wrong one."""
        window.set_labels(_store(_label()))
        window._zoom = (0, 0, 100, 100)
        assert not _ink_columns(_paint(window))

    def test_nothing_is_drawn_with_no_picture(self, window):
        window.set_labels(_store(_label()))
        assert not _ink_columns(_paint(window, views=[]))

    def test_a_nameless_label_draws_nothing(self, window):
        window.set_labels(_store(_label(name="")))
        assert not _ink_columns(_paint(window))


class TestDebugView:
    def test_it_is_off_by_default(self, window):
        """A normal player sees a name and nothing else."""
        window.set_labels(_store(_label()))
        plain = _ink_columns(_paint(window))
        window.set_labels(_store(_label()), debug=True)
        noisy = _ink_columns(_paint(window))
        assert len(noisy) > len(plain), "the debug view drew no more than the label"

    def test_set_labels_none_turns_everything_off(self, window):
        window.set_labels(_store(_label()))
        assert _ink_columns(_paint(window))
        window.set_labels(None)
        assert not _ink_columns(_paint(window))


class TestTheGpuPath:
    """Labels on the upscaler path, where `paintEvent` draws nothing.

    The native child covers the widget and presents the picture itself, so a
    label drawn in `paintEvent` is invisible work under it. They go into the
    overlay image instead, beside the OSD and the control bar -- and if this
    is wrong, the feature silently does nothing for anybody who turned
    upscaling on, which is exactly the kind of half-working this project keeps
    having to unpick.
    """

    class FakeOverlay:
        def __init__(self):
            self.calls = []

        def update(self, **kwargs):
            self.calls.append(kwargs)
            return False

        def to_overlay(self):
            return None

    class FakeUpscaler:
        """`release()` tears the renderer down, so this needs shutting down."""

        def shutdown(self):
            return None

    def _armed(self, window, *, crops=(), source=(640, 360)):
        window._overlay = self.FakeOverlay()
        window._upscaler = self.FakeUpscaler()
        decoder = window._decoder
        decoder._crops = tuple(crops)
        decoder._transition = None
        decoder.last_source_size = source
        decoder.last_output = (1280, 720)
        window._viewport = (400, 300)
        return window._overlay

    def test_a_label_reaches_the_overlay(self, window):
        overlay = self._armed(window)
        window.set_labels(_store(_label(x=0.4, y=0.3, w=0.1)))
        window._publish_overlay()
        assert overlay.calls, "the overlay was never updated"
        assert overlay.calls[-1]["labels"], "no label was handed to the overlay"

    def test_nothing_without_a_store(self, window):
        overlay = self._armed(window)
        window._publish_overlay()
        assert overlay.calls[-1]["labels"] == []

    def test_nothing_during_a_camera_move(self, window):
        overlay = self._armed(window)
        window.set_labels(_store(_label()))
        window._decoder._transition = (0, (0, 0, 1, 1), (0, 0, 0.5, 0.5))
        window._publish_overlay()
        assert overlay.calls[-1]["labels"] == []

    def test_nothing_before_a_frame_has_been_presented(self, window):
        overlay = self._armed(window, source=(0, 0))
        window.set_labels(_store(_label()))
        window._publish_overlay()
        assert overlay.calls[-1]["labels"] == []

    def test_a_cropped_client_places_it_in_its_own_piece(self, window):
        """The same rule as the software path, through entirely different
        geometry: `plan_blits` and `rebase` rather than the drawn rects."""
        overlay = self._armed(window, crops=((0.5, 0.0, 0.5, 1.0),))
        window.set_labels(_store(_label(x=0.7, y=0.3, w=0.0)))
        window._publish_overlay()
        assert overlay.calls[-1]["labels"], "a label inside the crop was dropped"

    def test_a_label_outside_the_crop_is_dropped(self, window):
        overlay = self._armed(window, crops=((0.5, 0.0, 0.5, 1.0),))
        window.set_labels(_store(_label(x=0.1, y=0.3, w=0.0)))
        window._publish_overlay()
        assert overlay.calls[-1]["labels"] == []

    def test_positions_are_whole_pixels(self, window):
        """They enter the overlay's change signature, and an exact position
        would rebuild a full-window RGBA image on every single frame."""
        overlay = self._armed(window)
        window.set_labels(_store(_label()))
        window._publish_overlay()
        for _text, x, y, tail, _opacity in overlay.calls[-1]["labels"]:
            assert x == int(x) and y == int(y)
            assert all(isinstance(v, int) for point in tail for v in point)


class TestItFadesInOnScreen:
    """The store fades a new name in; the painter has to honour that, or the
    fade is arithmetic nobody sees."""

    def test_a_name_that_has_only_just_appeared_is_barely_there(self, window):
        from common.timing import now_ns

        from client.gui.player_labels import LabelStore

        store = LabelStore()
        store.ingest("FULL", [_label()], now_ns())
        window.set_labels(store)
        assert len(_ink_columns(_paint(window))) < 3

    def test_and_is_drawn_once_it_has_faded_in(self, window):
        from common.timing import now_ns

        from client.gui.player_labels import FADE_NS, LabelStore

        store = LabelStore()
        store.ingest("FULL", [_label()], now_ns() - FADE_NS)
        store.visible(now_ns() - FADE_NS)
        window.set_labels(store)
        assert _ink_columns(_paint(window)), "nothing was drawn"
