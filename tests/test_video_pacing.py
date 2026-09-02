"""The sender's pacer: what gets spread out, and what does not.

Pacing exists because a keyframe is a genuine burst -- 100+ slices pushed into
the socket back to back overruns a home router's queue, and the loss lands on
our own stream. It does **not** exist to slow down ordinary frames, which are a
fraction of that size and are simply the stream running at the rate it was
configured for.

The old pacer could not tell the two apart. It spread *every* frame across half
a frame interval whenever the frame exceeded one 8-slice burst, and at the
settings this project actually runs -- 1080p60, 8 Mbps -- an average frame is 15
slices. So every frame paid a 4.2 ms sleep in the middle of its own fan-out.

Measured on real hardware, capture card to a Raspberry Pi over WiFi:

    old pacer          fan-out p50 4.70 ms
    no pacing at all   fan-out p50 0.46 ms
    this pacer         fan-out p50 0.53 ms

with no relationship between the setting and slice loss across paired runs
(the worst loss observed, 0.35%, was with the old pacer *enabled*).
"""

from __future__ import annotations

from common.timing import now_ns
from common.video import VideoSettings, slice_count_for
from videoserver.net import _MIN_UNPACED_SLICES, VideoNet


class _FakeCrypto:
    def encrypt(self, plaintext):
        return bytes(plaintext)


class _FakeSession:
    client_id = "client"
    client_name = "client"
    address = ("127.0.0.1", 1)
    is_approved = True
    role = "viewer"
    crypto = _FakeCrypto()


class _Frame:
    """Stands in for EncodedFrame; only these four fields are read."""

    def __init__(self, size: int, keyframe: bool = False) -> None:
        self.data = bytes(size)
        self.keyframe = keyframe
        self.capture_ts = now_ns()
        self.encode_ns = 0


def _net(bitrate_kbps: int = 8000, fps: int = 60) -> VideoNet:
    settings = VideoSettings(
        width=1920, height=1080, fps=fps, bitrate_kbps=bitrate_kbps
    )
    net = VideoNet(settings, "password")
    net._approved_sessions = lambda: [_FakeSession()]
    net._sendto = lambda data, address: None
    return net


def _fan_out_ms(net: VideoNet, size: int, keyframe: bool = False) -> float:
    started = now_ns()
    net._fan_out(_Frame(size, keyframe))
    return (now_ns() - started) / 1_000_000


def _nominal_bytes(bitrate_kbps: int = 8000, fps: int = 60) -> int:
    return int(bitrate_kbps * 1000 / 8 / fps)


class TestAnOrdinaryFrameIsNotPaced:
    def test_a_nominal_frame_leaves_in_one_go(self):
        net = _net()
        # 15 slices at the configured settings -- just over the old 8-slice
        # burst threshold, which is exactly why it used to draw a full budget.
        assert _fan_out_ms(net, _nominal_bytes()) < 1.5

    def test_a_frame_at_the_allowance_is_still_not_paced(self):
        net = _net()
        allowance = net._unpaced_allowance(60)
        size = allowance * 1150            # exactly the allowance, in slices
        assert slice_count_for(size) <= allowance
        assert _fan_out_ms(net, size) < 1.5


class TestAKeyframeIsStillPaced:
    """The case the pacer was written for, and it must keep working."""

    def test_a_keyframe_is_spread_out(self):
        net = _net()
        # ~105 slices: a realistic 1080p keyframe at this bitrate.
        elapsed = _fan_out_ms(net, 120_000, keyframe=True)
        assert elapsed > 2.0, f"a keyframe went out unpaced ({elapsed:.2f} ms)"

    def test_it_is_spread_over_no_more_than_the_budget(self):
        """Pacing must never become the bottleneck: half a frame interval, max.

        At 60 fps that is 8.33 ms. A pacer that overran it would delay the
        *next* frame, which is the failure it exists to prevent.
        """
        net = _net()
        elapsed = _fan_out_ms(net, 400_000, keyframe=True)
        assert elapsed < 1000.0 / 60 * 0.5 + 3.0, f"fan-out overran ({elapsed:.2f} ms)"

    def test_the_bigger_the_frame_the_more_it_is_paced(self):
        net = _net()
        small = _fan_out_ms(net, _nominal_bytes())
        large = _fan_out_ms(net, 200_000, keyframe=True)
        assert large > small


class TestTheAllowanceFollowsTheConfiguration:
    """Derived from the bitrate, never from the frame in hand.

    A pacer whose behaviour depends on how busy the picture is produces
    content-dependent jitter, which is indistinguishable from a network fault
    to anyone trying to diagnose it.
    """

    def test_a_higher_bitrate_allows_a_bigger_frame_through(self):
        assert _net(20000)._unpaced_allowance(60) > _net(4000)._unpaced_allowance(60)

    def test_a_higher_frame_rate_allows_a_smaller_one(self):
        # The same bitrate spread over more frames means each is smaller.
        assert _net(8000, 120)._unpaced_allowance(120) < _net(8000, 30)._unpaced_allowance(30)

    def test_a_tiny_bitrate_still_gets_a_usable_allowance(self):
        # Otherwise a 500 kbps stream computes an allowance of one or two
        # slices and paces frames that are trivially small.
        assert _net(500, 60)._unpaced_allowance(60) >= _MIN_UNPACED_SLICES

    def test_the_allowance_sits_above_a_nominal_frame(self):
        """Encoder output varies around the rate it was asked for.

        An allowance at exactly the average would put ordinary frames on both
        sides of it, making pacing a coin toss -- worse than either answer,
        because the jitter would then track the content.
        """
        net = _net()
        assert net._unpaced_allowance(60) > slice_count_for(_nominal_bytes())


class TestEveryClientStillGetsEverySlice:
    """Pacing changes timing, never delivery."""

    def test_all_slices_are_sent(self):
        net = _net()
        sent: list[int] = []
        net._sendto = lambda data, address: sent.append(len(data))
        size = 120_000
        net._fan_out(_Frame(size, keyframe=True))
        assert len(sent) == slice_count_for(size)

    def test_interleaved_across_clients(self):
        """Slice i reaches everyone before slice i+1 reaches anyone.

        Sending one client a whole frame first would hand the last client a
        frame already (clients-1) x frame-time old.
        """
        net = _net()
        first, second = _FakeSession(), _FakeSession()
        second.client_id = "second"
        second.address = ("127.0.0.1", 2)
        net._approved_sessions = lambda: [first, second]

        order: list[tuple[str, int]] = []
        net._sendto = lambda data, address: order.append(address)
        net._fan_out(_Frame(_nominal_bytes() * 3))

        # Addresses must alternate, never run in blocks.
        assert order[0] != order[1]
        assert order[0] == order[2]
