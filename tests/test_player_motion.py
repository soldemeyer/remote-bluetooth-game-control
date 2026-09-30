"""Recording what each player's thumb has been doing.

The one identity signal that survives a shared screen. Most of these are about
*not* sending something: a partial window, a player standing still, somebody
who has left. Each of those, sent, is a trace that correlates weakly with
everything -- which is worse than sending nothing, because it can still win a
comparison against a player who is genuinely motionless.
"""

from __future__ import annotations

from server.player_motion import SAMPLE_HZ, WINDOW_SAMPLES, MotionRecorder


class FakeSlot:
    def __init__(self, left_x=0, left_y=0):
        self.left_x = left_x
        self.left_y = left_y


class FakeSession:
    def __init__(self, client_id, slots):
        self.client_id = client_id
        self.slots = slots


class FakeSessions:
    def __init__(self, *sessions):
        self._sessions = list(sessions)

    def all_sessions(self):
        return list(self._sessions)


class FakeChannel:
    def __init__(self, number, client=None, slot=0):
        self.number = number
        self.assigned_client = client
        self.assigned_slot = slot if client else None

    @property
    def is_assigned(self):
        return self.assigned_client is not None and self.assigned_slot is not None


class FakeRouter:
    def __init__(self, *channels):
        self._channels = list(channels)

    def channels(self):
        return list(self._channels)


def _world(*, p1=(0, 0), p2=(0, 0)):
    router = FakeRouter(FakeChannel(1, "c1", 0), FakeChannel(2, "c2", 0))
    sessions = FakeSessions(
        FakeSession("c1", {0: FakeSlot(*p1)}),
        FakeSession("c2", {0: FakeSlot(*p2)}),
    )
    return router, sessions


FULL_RIGHT = (32767, 0)
FULL_DOWN = (0, 32767)


class TestSampling:
    def test_it_records_each_assigned_player(self):
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT, p2=FULL_DOWN)
        recorder.sample(router, sessions)
        assert len(recorder) == 2

    def test_the_stick_is_normalised(self):
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT)
        for _ in range(WINDOW_SAMPLES):
            recorder.sample(router, sessions)
        trace = next(t for t in recorder.traces() if t.player_id == 1)
        assert trace.samples[-1] == (1.0, 0.0)

    def test_y_is_not_flipped(self):
        """The stick's own Y is already down-positive, so it agrees with
        normalised frame coordinates. A flip added 'for safety' would invert
        every correlation and turn the one signal that separates two identical
        characters into the thing that swaps them."""
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_DOWN)
        for _ in range(WINDOW_SAMPLES):
            recorder.sample(router, sessions)
        trace = next(t for t in recorder.traces() if t.player_id == 1)
        assert trace.samples[-1] == (0.0, 1.0)

    def test_a_resting_stick_records_as_zero(self):
        """A resting stick does not sit exactly at centre, and a trace full of
        small nonsense correlates weakly with everything."""
        recorder = MotionRecorder()
        router, sessions = _world(p1=(600, -400))
        recorder.sample(router, sessions)
        trace = recorder._traces[1]
        assert trace.samples[-1] == (0.0, 0.0)

    def test_the_window_is_bounded(self):
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT)
        for _ in range(WINDOW_SAMPLES * 4):
            recorder.sample(router, sessions)
        assert len(recorder._traces[1].samples) == WINDOW_SAMPLES

    def test_an_unnumbered_adapter_is_not_a_player(self):
        recorder = MotionRecorder()
        router = FakeRouter(FakeChannel(0, "c1", 0))
        sessions = FakeSessions(FakeSession("c1", {0: FakeSlot(*FULL_RIGHT)}))
        recorder.sample(router, sessions)
        assert len(recorder) == 0

    def test_an_unassigned_adapter_is_not_a_player(self):
        recorder = MotionRecorder()
        router = FakeRouter(FakeChannel(1))
        recorder.sample(router, FakeSessions())
        assert len(recorder) == 0

    def test_a_channel_whose_session_has_gone_is_skipped(self):
        recorder = MotionRecorder()
        router = FakeRouter(FakeChannel(1, "vanished", 0))
        recorder.sample(router, FakeSessions())
        assert len(recorder) == 0


class TestWhatIsSent:
    def _full_window(self, **kwargs):
        recorder = MotionRecorder()
        router, sessions = _world(**kwargs)
        for _ in range(WINDOW_SAMPLES):
            recorder.sample(router, sessions)
        return recorder

    def test_a_full_window_of_movement_is_sent(self):
        recorder = self._full_window(p1=FULL_RIGHT, p2=FULL_DOWN)
        assert {t.player_id for t in recorder.traces()} == {1, 2}

    def test_a_partial_window_is_not(self):
        """Correlating a fragment of a gesture against a whole one gives a
        score that means nothing."""
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT)
        for _ in range(WINDOW_SAMPLES - 1):
            recorder.sample(router, sessions)
        assert recorder.traces() == []

    def test_a_player_who_has_not_moved_is_not_sent(self):
        """A trace of nothing but zeros is not evidence that somebody is
        standing still -- it is evidence of nothing, and it invites a match
        against whatever else on screen happens to be motionless."""
        recorder = self._full_window(p1=FULL_RIGHT, p2=(0, 0))
        assert {t.player_id for t in recorder.traces()} == {1}


class TestLifecycle:
    def test_a_departed_player_is_dropped(self):
        """The same leak _forget_rumble_state and SyncGovernor.forget close:
        a trace for somebody who left goes on offering itself as a match."""
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT, p2=FULL_DOWN)
        recorder.sample(router, sessions)
        assert len(recorder) == 2

        alone = FakeRouter(FakeChannel(1, "c1", 0))
        recorder.sample(alone, sessions)
        assert len(recorder) == 1
        assert 2 not in recorder._traces

    def test_forget_and_clear(self):
        recorder = MotionRecorder()
        router, sessions = _world(p1=FULL_RIGHT, p2=FULL_DOWN)
        recorder.sample(router, sessions)
        recorder.forget(1)
        assert len(recorder) == 1
        recorder.clear()
        assert len(recorder) == 0


class TestBudget:
    def test_a_full_window_for_four_players_fits_a_control_message(self):
        """It has to: `encode_control` refuses an oversized message whole."""
        import json

        from common import protocol
        from common.player_labels import encode_traces

        class Trace:
            def __init__(self, player_id):
                self.player_id = player_id
                self.hz = SAMPLE_HZ
                self.samples = tuple((1.0, -1.0) for _ in range(WINDOW_SAMPLES))

        payload = encode_traces([Trace(n) for n in range(1, 5)])
        raw = json.dumps({"op": "video_player_input", **payload}, separators=(",", ":"))
        assert len(raw) < protocol.MAX_DATAGRAM - 5, len(raw)
