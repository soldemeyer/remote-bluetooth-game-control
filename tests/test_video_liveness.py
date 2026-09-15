"""What "streaming" means, and what a black picture is allowed to claim.

**Reported as: connected to the server, and the video never showed -- a black
screen with the text "Streaming direct".**

Two faults, and the second is what made the first permanent.

1. `_on_media` stamped the liveness clock *before* dispatching, so every packet
   counted -- including `MEDIA_HEARTBEAT_ACK`, which is the reply to our own
   keepalive. The stream therefore looked alive on the strength of its own
   heartbeat: with the control path up and not one video slice arriving, the
   receiver stayed STREAMING for ever. It never stalled, never failed, never
   retried, and never re-asked for a keyframe -- so a stream that missed its
   opening IDR stayed black with nothing trying to recover it.

2. The placeholder printed the *receiver's* state and detail, which for that
   receiver is "Streaming" and the transport mode. So the window said
   "Streaming direct" over a picture that had never existed: true of the
   socket, and a lie about the thing somebody was looking at.
"""

from __future__ import annotations

import pytest

from client.net.video import VideoReceiver, VideoStreamState
from common.protocol import PacketType
from common.timing import now_ns


@pytest.fixture
def receiver():
    return VideoReceiver("pw", client_name="test")


class TestOnlyMediaCountsAsLife:
    """The liveness clock answers "is a picture coming", so only a picture may
    refresh it."""

    def test_a_heartbeat_ack_does_not_refresh_it(self, receiver, monkeypatch):
        monkeypatch.setattr(receiver, "_handle_clock_ack", lambda _data: None)
        receiver._last_media_ns = 0

        receiver._on_media(bytes([PacketType.MEDIA_HEARTBEAT_ACK]) + b"\x00" * 40)

        assert receiver._last_media_ns == 0, (
            "the reply to our own keepalive made the stream look alive"
        )

    def test_a_video_slice_does(self, receiver, monkeypatch):
        monkeypatch.setattr(receiver, "_handle_slice", lambda _data: None)
        receiver._last_media_ns = 0

        receiver._on_media(bytes([PacketType.VIDEO_FRAME]) + b"\x00" * 40)

        assert receiver._last_media_ns > 0

    def test_audio_does_too(self, receiver, monkeypatch):
        """Audio arriving means the media path works; a picture missing while
        sound plays is a different fault and should not read as a dead link."""
        monkeypatch.setattr(receiver, "_handle_audio", lambda _data: None)
        receiver._last_media_ns = 0

        receiver._on_media(bytes([PacketType.AUDIO_FRAME]) + b"\x00" * 40)

        assert receiver._last_media_ns > 0

    def test_a_quiet_stream_now_stalls(self, receiver):
        """The whole point: with only heartbeats flowing this stayed STREAMING
        for ever, so nothing ever re-asked for a keyframe."""
        receiver._state = VideoStreamState.STREAMING
        receiver._last_media_ns = now_ns() - 4_000_000_000
        requested = []
        receiver._request_idr = lambda reason, force=False: requested.append(reason)

        stop = receiver._check_liveness()

        assert stop is False
        assert receiver.state is VideoStreamState.STALLED
        assert requested, "a stalled stream must ask for a keyframe"

    def test_and_eventually_fails_so_it_is_retried(self, receiver):
        receiver._state = VideoStreamState.STREAMING
        receiver._last_media_ns = now_ns() - 9_000_000_000

        stop = receiver._check_liveness()

        assert stop is True
        assert receiver.state is VideoStreamState.FAILED

    def test_the_first_frame_still_gets_a_grace_period(self, receiver):
        """Stamped once at connect, so a source that needs a moment to emit a
        keyframe is not declared dead before it can."""
        receiver._state = VideoStreamState.STREAMING
        receiver._last_media_ns = now_ns()

        assert receiver._check_liveness() is False
        assert receiver.state is VideoStreamState.STREAMING


class TestTheReceiverCanSayWhetherAPictureArrived:
    """The state cannot: STREAMING means the socket handshook."""

    def test_nothing_arrived_yet(self, receiver):
        assert receiver.frames_arrived is False
        assert receiver.slices_received == 0

    def test_slices_are_counted_separately_from_frames(self, receiver):
        """"Nothing is arriving" and "pieces arrive and never complete" are
        different faults, and a black window looks the same for both."""
        receiver._assembler.slices_received = 12

        assert receiver.slices_received == 12
        assert receiver.frames_arrived is False

    def test_a_complete_frame_shows_up(self, receiver):
        receiver._assembler.frames_complete = 1

        assert receiver.frames_arrived is True


@pytest.mark.usefixtures("receiver")
class TestThePlaceholderDescribesThePicture:
    """Not the socket. "Streaming direct" over a black window named the half
    that was working."""

    def placeholder(self, receiver):
        from client.gui.video_window import VideoWindow

        # The method reads only the receiver, so it is exercised unbound
        # rather than by building a widget -- this file has no Qt application
        # and does not need one.
        return VideoWindow._placeholder_text(
            type("S", (), {"_receiver": receiver})()
        )

    def test_a_connected_stream_with_nothing_arriving_says_so(self, receiver):
        receiver._state = VideoStreamState.STREAMING
        receiver._detail = "direct"

        text = self.placeholder(receiver)

        assert "Streaming direct" not in text
        assert "first frame" in text

    def test_slices_without_a_frame_are_named(self, receiver):
        receiver._state = VideoStreamState.STREAMING
        receiver._assembler.slices_received = 37

        text = self.placeholder(receiver)

        assert "37" in text
        assert "no complete frame" in text

    def test_a_real_state_is_still_reported(self, receiver):
        """Stalled, failed, connecting: there the state *is* the news, and it
        already said something useful."""
        receiver._state = VideoStreamState.STALLED
        receiver._detail = "no video arriving"

        text = self.placeholder(receiver)

        assert "Stalled" in text
        assert "no video arriving" in text

    def test_a_gap_after_a_picture_is_not_a_cold_start(self, receiver):
        receiver._state = VideoStreamState.STREAMING
        receiver._assembler.slices_received = 500
        receiver._assembler.frames_complete = 10

        assert self.placeholder(receiver) == "Waiting for the next frame"
