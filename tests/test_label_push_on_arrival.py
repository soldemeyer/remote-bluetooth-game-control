"""Labels leave the Bluetooth server when new tracks arrive, not on a timer.

Reported as the names lagging behind the characters. The status tick added up
to 100 ms before a new position left this machine, capped updates at 10 Hz
whatever the source identified at, and made the client see samples 100 or
200 ms apart for a source sampling in between -- which is what made its
smoothing stall.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from common.player_labels import encode_tracks
from common.screen_regions import QUAD_4, Rect
from server.video import VideoRegistry
from server.web import app as web_app


class _Row:
    def __init__(self, track_id, player_id):
        self.track_id = track_id
        self.player_id = player_id
        self.box = Rect(0.2, 0.2, 0.1, 0.1)
        self.confidence = 0.9
        self.region = "upper_left"
        self.source = "appearance"


def _tracks():
    return encode_tracks([_Row(1, 1)], QUAD_4, 123)


class TestTheRegistrySaysWhenTracksArrive:
    def test_it_calls_back_with_new_tracks(self):
        registry = VideoRegistry()
        heard = []
        registry.on_tracks = lambda: heard.append(True)
        registry.update_tracks(_tracks())
        assert heard == [True]

    def test_a_failing_callback_never_reaches_the_delivering_thread(self):
        """It runs on the video link's thread or the datapath's; an exception
        there would cost far more than a late label."""
        registry = VideoRegistry()

        def broken():
            raise RuntimeError("no loop")

        registry.on_tracks = broken
        registry.update_tracks(_tracks())
        assert registry.tracks[1], "the tracks were not kept"

    def test_nothing_is_called_when_nobody_listens(self):
        VideoRegistry().update_tracks(_tracks())


class _Datapath:
    def __init__(self):
        self.pushes = 0

    def broadcast_player_labels(self):
        self.pushes += 1


class TestThePusher:
    @pytest.mark.asyncio
    async def test_labels_go_out_as_soon_as_tracks_arrive(self):
        datapath = _Datapath()
        app = {"state": SimpleNamespace(datapath=datapath)}
        arrived = asyncio.Event()
        task = asyncio.create_task(web_app._label_pusher(app, arrived))
        try:
            arrived.set()
            await asyncio.sleep(0.005)
            assert datapath.pushes == 1, "not pushed on arrival"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_burst_is_not_echoed_one_for_one(self):
        datapath = _Datapath()
        app = {"state": SimpleNamespace(datapath=datapath)}
        arrived = asyncio.Event()
        task = asyncio.create_task(web_app._label_pusher(app, arrived))
        try:
            # Ten arrivals back to back -- no sleeps between them, because a
            # "1 ms" sleep is about 15 ms on Windows and the burst would then
            # be spread over longer than the throttle.
            for _ in range(10):
                arrived.set()
            await asyncio.sleep(0.005)
            assert datapath.pushes == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_a_failing_push_does_not_stop_the_next(self):
        class Broken(_Datapath):
            def broadcast_player_labels(self):
                self.pushes += 1
                raise RuntimeError("socket gone")

        datapath = Broken()
        app = {"state": SimpleNamespace(datapath=datapath)}
        arrived = asyncio.Event()
        task = asyncio.create_task(web_app._label_pusher(app, arrived))
        try:
            arrived.set()
            await asyncio.sleep(web_app.LABEL_MIN_INTERVAL_S + 0.01)
            arrived.set()
            await asyncio.sleep(0.005)
            assert datapath.pushes == 2
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
