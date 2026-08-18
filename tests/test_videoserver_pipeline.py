"""Video server: capture, encode, and serve over a real socket.

Runs the whole source-side pipeline in-process against a scripted receiver that
speaks the wire protocol directly, so what is verified is the bytes rather than
our own abstractions agreeing with each other. No hardware: the source is a
lavfi test pattern.

The failures this is aimed at are the ones that produce a *working-looking*
stream that is quietly wrong:

  * All-intra output. Every counter looks healthy and the picture is perfect;
    it just costs five times the bitrate. Only the keyframe ratio shows it.
  * An unpaced test source, which makes every latency measurement taken against
    it meaningless while looking like excellent performance.
  * Slices that reassemble into something the decoder rejects.
"""

from __future__ import annotations

import socket
import time

import pytest

av = pytest.importorskip("av", reason="video extras not installed")

from common import crypto, protocol, video          # noqa: E402
from common.protocol import PROTOCOL_VERSION, PacketType  # noqa: E402
from common.video import (                          # noqa: E402
    ClockSync,
    FrameAssembler,
    IdrReason,
    MediaCodec,
    VideoSettings,
)
from videoserver.encode import (                    # noqa: E402
    available_encoders,
    usable_encoders,
    encoder_candidates,
    pick_encoder,
)
from videoserver.net import VideoNet                # noqa: E402

PASSWORD = "video-pipeline-test-password"


def small_settings(**overrides) -> VideoSettings:
    """A cheap stream: big enough to slice, small enough to be quick."""
    base = {
        "test_source": True,
        "width": 320,
        "height": 240,
        "fps": 30,
        "bitrate_kbps": 2000,
        "encoder": "libx264",
        "audio_enabled": False,
        "preview_enabled": False,
        "gop_s": 1.0,
    }
    base.update(overrides)
    return VideoSettings(**base).clamped()


class ScriptedViewer:
    """A media client built straight on the wire protocol.

    Deliberately not the real VideoReceiver: this exists to check that what
    goes out is what the protocol says, so it must not share code with the
    thing under test.
    """

    def __init__(
        self,
        address: tuple[str, int],
        password: str = PASSWORD,
        ticket: str | None = None,
    ) -> None:
        self.address = address
        self._password = password
        self.ticket = ticket
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(5.0)
        self.crypto: crypto.SessionCrypto | None = None
        self.assembler = FrameAssembler()
        self.clock = ClockSync()
        self.audio_packets: list[bytes] = []
        self._replay = protocol.ReplayWindow()

    def connect(self) -> None:
        client_id = crypto.new_client_id()
        client_random = crypto.new_random()
        hello = (
            bytes([PacketType.HELLO])
            + PROTOCOL_VERSION.to_bytes(2, "little")
            + client_id
            + client_random
        )
        self.sock.sendto(hello, self.address)
        challenge, _ = self.sock.recvfrom(2048)
        assert challenge[0] == PacketType.CHALLENGE

        salt = challenge[1 : 1 + crypto.SALT_SIZE]
        server_random = challenge[
            1 + crypto.SALT_SIZE : 1 + crypto.SALT_SIZE + crypto.RANDOM_SIZE
        ]
        master = crypto.derive_master_key(self._password, salt)
        session_key, proof_key = crypto.derive_session_keys(
            master, client_random, server_random
        )
        proof = crypto.compute_auth_proof(proof_key, client_id, PROTOCOL_VERSION)
        payload = {"client_name": "viewer", "role": "video-client"}
        if self.ticket is not None:
            payload["ticket"] = self.ticket
        info = protocol.encode_control(
            0, protocol.ControlOp.SET_CONTROLLERS, payload
        )
        session = crypto.SessionCrypto.for_client(session_key)
        auth = bytes([PacketType.AUTH]) + client_id + proof + session.encrypt(info)

        self.sock.sendto(auth, self.address)
        accept, _ = self.sock.recvfrom(2048)
        assert accept[0] == PacketType.ACCEPT, "the media socket refused the handshake"
        self.crypto = session

    def send(self, plaintext: bytes) -> None:
        assert self.crypto is not None
        self.sock.sendto(self.crypto.encrypt(plaintext), self.address)

    def request_idr(self) -> None:
        buf = bytearray(64)
        size = video.encode_idr_request_into(buf, 0, 0, IdrReason.JOIN)
        self.send(bytes(buf[:size]))

    def probe_clock(self) -> None:
        from common.timing import now_ns

        buf = bytearray(64)
        size = video.encode_media_heartbeat_into(buf, 0, 1, now_ns())
        self.send(bytes(buf[:size]))

    def pump(self, duration_s: float) -> list:
        """Collect completed frames for a while."""
        from common.timing import now_ns

        frames = []
        deadline = time.monotonic() + duration_s
        self.sock.settimeout(0.3)
        while time.monotonic() < deadline:
            try:
                data, _ = self.sock.recvfrom(4096)
            except (socket.timeout, TimeoutError):
                continue
            assert self.crypto is not None
            try:
                counter, plaintext = self.crypto.decrypt(data)
            except crypto.CryptoError:
                continue
            if not self._replay.check_and_update(counter) or not plaintext:
                continue

            kind = plaintext[0]
            if kind == PacketType.VIDEO_FRAME:
                parsed = video.decode_video_slice(plaintext, 0)
                completed = self.assembler.add(*parsed)
                if completed is not None:
                    frames.append(completed)
            elif kind == PacketType.AUDIO_FRAME:
                _, _, payload = video.decode_audio_frame(plaintext, 0)
                self.audio_packets.append(bytes(payload))
            elif kind == PacketType.MEDIA_HEARTBEAT_ACK:
                _, t0, t1, t2 = video.decode_media_heartbeat_ack(plaintext, 0)
                self.clock.add_sample(t0, t1, t2, now_ns())
        return frames

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def running_source():
    """A VideoNet plus a live capture/encode pipeline, on an ephemeral port."""
    from videoserver.capture import VideoCapture
    from videoserver.encode import VideoEncoder

    created: list = []

    def build(settings: VideoSettings | None = None) -> VideoNet:
        settings = settings or small_settings()
        net = VideoNet(settings, PASSWORD, bind_host="127.0.0.1", bind_port=0)
        capture = VideoCapture(settings)
        encoder = VideoEncoder(settings, capture, on_frame=net.submit_frame)
        net._on_idr_request = encoder.request_idr

        net.start()
        capture.start()
        encoder.start()
        created.append((net, capture, encoder))
        return net

    yield build

    for net, capture, encoder in created:
        encoder.stop()
        capture.stop()
        net.stop()


class TestEncoderSelection:
    def test_the_wheel_ships_a_software_encoder(self):
        """Everything else falls back to libx264; if it is absent, nothing works."""
        assert "libx264" in available_encoders()

    def test_opus_is_available(self):
        assert "libopus" in av.codecs_available

    def test_an_explicit_choice_is_tried_first(self):
        assert encoder_candidates("h264_qsv")[0] == "h264_qsv"

    def test_the_chain_always_ends_in_software(self):
        assert encoder_candidates("auto")[-1] == "libx264"

    def test_a_nonsense_encoder_falls_through_to_one_that_works(self):
        """A bad name must degrade, not stop the stream.

        Which encoder it lands on depends on the machine -- hardware if this
        one has it, software otherwise -- so assert only that the fallback
        produced something usable.
        """
        name, ctx = pick_encoder(small_settings(encoder="h264_nonexistent"))
        assert name in encoder_candidates("auto")
        assert ctx.is_open

    def test_built_in_is_not_the_same_as_usable(self):
        """FFmpeg ships NVENC, QSV and AMF support whatever silicon is present.

        Conflating the two is not academic: it made the QSV and AMF test cases
        fall back to NVENC and re-test it under names claiming coverage they
        did not have, and it offered the operator encoders that cannot open.
        """
        built_in = available_encoders()
        usable = usable_encoders()

        assert "libx264" in usable, "software encoding must always work"
        assert set(usable) <= set(built_in), "usable must be a subset of built-in"

    def test_usable_encoders_is_cached(self):
        """The probe costs a real encode each; the answer needs new hardware."""
        first = usable_encoders()
        assert usable_encoders() == first

    def test_every_usable_encoder_really_opens(self):
        for name in usable_encoders():
            picked, ctx = pick_encoder(small_settings(encoder=name))
            assert picked == name, f"asked for {name}, silently got {picked}"
            assert ctx.is_open

    def test_picked_encoder_is_open_and_configured(self):
        name, ctx = pick_encoder(small_settings())
        assert name == "libx264"
        assert ctx.is_open
        assert ctx.max_b_frames == 0, "B-frames add a guaranteed frame of delay"
        assert ctx.width == 320


class TestEncodedStream:
    def test_frames_are_not_all_keyframes(self):
        """All-intra output looks perfect and costs ~5x the bitrate.

        It happens when a decoded frame's picture type is passed through to the
        encoder, which treats it as a forced I-frame. Nothing else in the
        system notices.
        """
        from videoserver.capture import VideoCapture
        from videoserver.encode import VideoEncoder

        settings = small_settings(fps=30, gop_s=1.0)
        capture = VideoCapture(settings)
        frames: list = []
        encoder = VideoEncoder(settings, capture, on_frame=frames.append)
        capture.start()
        encoder.start()
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(frames) < 45:
                time.sleep(0.05)
        finally:
            encoder.stop()
            capture.stop()

        assert len(frames) >= 20, f"only {len(frames)} frames encoded"
        keyframes = sum(1 for f in frames if f.keyframe)
        assert keyframes >= 1, "no keyframe at all -- a joining client could never start"
        assert keyframes < len(frames) / 2, (
            f"{keyframes}/{len(frames)} frames are keyframes; the encoder is "
            "producing intra-only output"
        )

    def test_the_test_source_is_paced_to_real_time(self):
        """An unpaced lavfi source invalidates every latency number taken from it."""
        from videoserver.capture import VideoCapture

        settings = small_settings(fps=30)
        capture = VideoCapture(settings)
        capture.start()
        try:
            time.sleep(2.0)
        finally:
            capture.stop()

        # 30 fps for 2 s is ~60. Unpaced, this was measured at ~15,000.
        assert 20 <= capture.frames_captured <= 200, (
            f"captured {capture.frames_captured} frames in 2s at 30 fps"
        )


class TestMediaSession:
    def test_a_viewer_authenticates_and_receives_frames(self, running_source):
        net = running_source()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            frames = viewer.pump(3.0)

            assert frames, "no complete frames arrived"
            assert all(f.codec == MediaCodec.H264 for f in frames)
            assert frames[0].capture_ts > 0
            assert net.client_count == 1
        finally:
            viewer.close()

    def test_received_frames_actually_decode(self, running_source):
        """Reassembly is only correct if a decoder accepts the result."""
        net = running_source()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            viewer.request_idr()
            frames = viewer.pump(3.0)
            assert frames

            decoder = av.CodecContext.create("h264", "r")
            decoded = 0
            for frame in frames:
                for packet in decoder.parse(frame.data):
                    for picture in decoder.decode(packet):
                        assert picture.width == 320
                        assert picture.height == 240
                        decoded += 1
            assert decoded > 0, "nothing decoded from the reassembled stream"
        finally:
            viewer.close()

    def test_a_multi_slice_frame_reassembles_over_the_wire(self, running_source):
        """A keyframe at any useful size exceeds one datagram."""
        net = running_source(small_settings(width=640, height=480, bitrate_kbps=6000))
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            viewer.request_idr()
            frames = viewer.pump(3.0)
            assert frames
            biggest = max(len(f.data) for f in frames)
            assert biggest > video.VIDEO_SLICE_PAYLOAD, (
                "no frame needed slicing; the test proves nothing"
            )
            assert viewer.assembler.frames_complete > 0
        finally:
            viewer.close()

    def test_clock_sync_completes_over_the_wire(self, running_source):
        net = running_source()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            for _ in range(6):
                viewer.probe_clock()
                viewer.pump(0.15)
            assert viewer.clock.locked, "clock never locked"
            # Same machine, so the offset is small -- but the exchange happened.
            assert abs(viewer.clock.offset_ns) < 1_000_000_000
        finally:
            viewer.close()

    def test_a_keyframe_request_is_answered(self, running_source):
        net = running_source(small_settings(gop_s=10.0))
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            viewer.pump(1.0)          # let the initial keyframe pass by
            before = net.idr_requests
            viewer.request_idr()
            frames = viewer.pump(2.0)
            assert net.idr_requests > before
            assert any(f.keyframe for f in frames), "no keyframe after requesting one"
        finally:
            viewer.close()

    def test_two_viewers_both_receive_the_stream(self, running_source):
        """Fan-out is interleaved so no viewer is systematically behind."""
        net = running_source()
        first = ScriptedViewer(("127.0.0.1", net.port))
        second = ScriptedViewer(("127.0.0.1", net.port))
        try:
            first.connect()
            second.connect()
            assert net.client_count == 2

            first_frames = first.pump(2.5)
            second_frames = second.pump(0.5)
            assert first_frames
            assert second_frames
        finally:
            first.close()
            second.close()

    def test_a_wrong_password_is_refused(self, running_source):
        net = running_source()
        viewer = ScriptedViewer(("127.0.0.1", net.port), password="not-the-password")
        try:
            with pytest.raises(AssertionError):
                viewer.connect()
        finally:
            viewer.close()

    def test_a_punch_probe_is_answered_before_any_session(self, running_source):
        """The reply is what opens our NAT mapping toward the peer."""
        net = running_source()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            sock.sendto(protocol.PUNCH_PROBE, ("127.0.0.1", net.port))
            data, _ = sock.recvfrom(1024)
            assert data.startswith(protocol.PUNCH_ACK_PROBE)
        finally:
            sock.close()

    def test_garbage_does_not_disturb_the_stream(self, running_source):
        net = running_source()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for payload in (b"", b"\x00", b"\xff" * 900, b"RBGC-NONSENSE"):
                noise.sendto(payload, ("127.0.0.1", net.port))
            noise.close()

            assert viewer.pump(2.0), "the stream stopped after malformed input"
        finally:
            viewer.close()


class TestAudio:
    def test_opus_packets_reach_a_viewer(self, running_source):
        from videoserver.capture import AudioCapture
        from videoserver.encode import AudioEncoder

        settings = small_settings(audio_enabled=True)
        net = running_source(settings)
        acap = AudioCapture(settings)
        aenc = AudioEncoder(settings, acap, on_packet=net.send_audio)
        acap.start()
        aenc.start()

        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            viewer.pump(2.0)
            assert viewer.audio_packets, "no audio arrived"

            decoder = av.CodecContext.create("libopus", "r")
            decoded = 0
            for payload in viewer.audio_packets[:20]:
                packet = av.Packet(payload)
                for frame in decoder.decode(packet):
                    decoded += frame.samples
            assert decoded > 0, "audio did not decode"
        finally:
            viewer.close()
            aenc.stop()
            acap.stop()


class TestTicketedAdmission:
    """With a Bluetooth server in charge, the password is not the whole check.

    It is shared with every player, so it cannot distinguish someone the
    operator approved from someone they denied. The source therefore also
    demands a ticket that only that server issues -- otherwise pressing *Deny*
    would take a player's controller away and leave them watching.
    """

    def _net(self, **kwargs) -> VideoNet:
        return VideoNet(
            small_settings(),
            PASSWORD,
            bind_host="127.0.0.1",
            bind_port=0,
            require_tickets=True,
            **kwargs,
        )

    def test_the_right_password_alone_is_not_enough(self):
        net = self._net()
        net.start()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            with pytest.raises(AssertionError):
                viewer.connect()          # no ticket presented
        finally:
            viewer.close()
            net.stop()

    def test_a_valid_ticket_is_admitted(self):
        net = self._net()
        net.set_tickets({"good-ticket"})
        net.start()
        viewer = ScriptedViewer(("127.0.0.1", net.port), ticket="good-ticket")
        try:
            viewer.connect()
            assert net.client_count == 1
        finally:
            viewer.close()
            net.stop()

    def test_a_forged_ticket_is_refused(self):
        net = self._net()
        net.set_tickets({"good-ticket"})
        net.start()
        viewer = ScriptedViewer(("127.0.0.1", net.port), ticket="guessed")
        try:
            with pytest.raises(AssertionError):
                viewer.connect()
            assert net.client_count == 0
        finally:
            viewer.close()
            net.stop()

    def test_revoking_drops_a_viewer_already_watching(self):
        """The operator pressed deny to stop them *now*, not at next connect."""
        net = self._net()
        net.set_tickets({"good-ticket"})
        net.start()
        viewer = ScriptedViewer(("127.0.0.1", net.port), ticket="good-ticket")
        try:
            viewer.connect()
            assert net.client_count == 1

            net.set_tickets(set())
            assert net.client_count == 0, "a denied viewer kept watching"
        finally:
            viewer.close()
            net.stop()

    def test_revoking_one_ticket_leaves_the_others_alone(self):
        net = self._net()
        net.set_tickets({"alice", "bob"})
        net.start()
        alice = ScriptedViewer(("127.0.0.1", net.port), ticket="alice")
        bob = ScriptedViewer(("127.0.0.1", net.port), ticket="bob")
        try:
            alice.connect()
            bob.connect()
            assert net.client_count == 2

            net.set_tickets({"bob"})
            assert net.client_count == 1
        finally:
            alice.close()
            bob.close()
            net.stop()

    def test_standalone_needs_no_ticket(self):
        """Nobody is there to issue one, which is what standalone means."""
        net = VideoNet(
            small_settings(), PASSWORD, bind_host="127.0.0.1", bind_port=0
        )
        net.start()
        viewer = ScriptedViewer(("127.0.0.1", net.port))
        try:
            viewer.connect()
            assert net.client_count == 1
        finally:
            viewer.close()
            net.stop()


def _nal_types(data: bytes) -> list[int]:
    """H.264 NAL unit types in an Annex B bitstream. 7=SPS, 8=PPS, 5=IDR."""
    types: list[int] = []
    index = 0
    while index < len(data) - 4:
        if data[index : index + 3] == b"\x00\x00\x01":
            offset = 3
        elif data[index : index + 4] == b"\x00\x00\x00\x01":
            offset = 4
        else:
            index += 1
            continue
        types.append(data[index + offset] & 0x1F)
        index += offset
    return types


@pytest.mark.parametrize("encoder", ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"])
class TestTheStreamStartsDecodable:
    """The first frame must carry SPS, PPS and an IDR.

    It did not, and the cause was subtle: `pick_encoder` probed a throwaway
    frame through the very context it then returned, so the stream's opening
    IDR *and its parameter sets* went into the probe's discarded output. Every
    context handed back therefore began mid-GOP, describing itself with
    parameter sets no client ever saw.

    Nothing on the source side notices. Frames are produced, counters climb,
    the encoder reports itself healthy -- and every client decodes exactly
    nothing. Measured on NVENC before the fix: 70 frames out, zero IDRs, zero
    pictures decoded, no error logged anywhere.

    Run against every encoder this machine has, because the severity varies:
    libx264 recovers at the next GOP boundary, which hid it in tests, while
    NVENC produced a stream that never became decodable at all.
    """

    def _encode(self, encoder: str, seconds: float = 2.5) -> list:
        from videoserver.capture import VideoCapture
        from videoserver.encode import VideoEncoder, encoder_candidates

        if encoder not in usable_encoders():
            # `available_encoders` would say yes here for hardware that is not
            # present: FFmpeg is built with NVENC, QSV and AMF whatever silicon
            # the machine has. Skipping on that answer meant the QSV and AMF
            # cases silently fell back to NVENC and re-tested it three times
            # over, under names claiming coverage they did not have.
            pytest.skip(f"{encoder} cannot be opened on this machine")
        assert encoder in encoder_candidates(encoder)

        settings = small_settings(width=640, height=480, encoder=encoder)
        capture = VideoCapture(settings)
        frames: list = []
        video_encoder = VideoEncoder(settings, capture, on_frame=frames.append)
        capture.start()
        video_encoder.start()
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and len(frames) < 30:
                time.sleep(0.05)
        finally:
            video_encoder.stop()
            capture.stop()

        assert frames, f"{encoder} produced nothing"
        return frames

    def test_the_first_frame_carries_parameter_sets(self, encoder):
        frames = self._encode(encoder)
        types = _nal_types(frames[0].data)

        assert 7 in types, "no SPS: a client cannot configure its decoder"
        assert 8 in types, "no PPS"
        assert frames[0].keyframe, "the stream does not open on a keyframe"

    def test_a_client_can_decode_from_the_very_first_frame(self, encoder):
        """The property that actually matters, checked end to end."""
        frames = self._encode(encoder)

        decoder = av.CodecContext.create("h264", "r")
        decoded = 0
        errors = 0
        for frame in frames[:25]:
            try:
                for packet in decoder.parse(frame.data):
                    decoded += len(decoder.decode(packet))
            except Exception:
                errors += 1

        assert errors == 0, f"{errors} frames failed to decode"
        assert decoded > 5, f"only {decoded} pictures decoded from {len(frames)} frames"


    def test_a_viewer_joining_mid_stream_can_decode(self, encoder):
        """The case every viewer after the first one hits.

        A joiner asks for a keyframe and can decode nothing until it gets one
        carrying SPS and PPS. Hardware encoders do not honour that request by
        default -- NVENC quietly ignored ``pict_type = I`` and emitted neither
        an IDR nor parameter sets, so a second viewer saw a permanently black
        window while every counter on both sides looked correct.
        """
        from videoserver.capture import VideoCapture
        from videoserver.encode import VideoEncoder

        if encoder not in usable_encoders():
            # `available_encoders` would say yes here for hardware that is not
            # present: FFmpeg is built with NVENC, QSV and AMF whatever silicon
            # the machine has. Skipping on that answer meant the QSV and AMF
            # cases silently fell back to NVENC and re-tested it three times
            # over, under names claiming coverage they did not have.
            pytest.skip(f"{encoder} cannot be opened on this machine")

        # A GOP long enough that no scheduled keyframe can rescue the test:
        # the only way to a decodable picture is the request itself.
        settings = small_settings(width=640, height=480, encoder=encoder, gop_s=100.0)
        capture = VideoCapture(settings)
        frames: list = []
        video_encoder = VideoEncoder(settings, capture, on_frame=frames.append)
        capture.start()
        video_encoder.start()
        try:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(frames) < 20:
                time.sleep(0.05)
            assert len(frames) >= 10, "the stream never got going"

            joined_at = len(frames)
            video_encoder.request_idr()

            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(frames) - joined_at < 15:
                time.sleep(0.05)
        finally:
            video_encoder.stop()
            capture.stop()

        joiner_sees = frames[joined_at:]
        assert joiner_sees, "nothing was produced after the keyframe request"

        decoder = av.CodecContext.create("h264", "r")
        decoded = 0
        for frame in joiner_sees:
            try:
                for packet in decoder.parse(frame.data):
                    decoded += len(decoder.decode(packet))
            except av.error.InvalidDataError:
                # Expected, and what a real joiner sees: frames that predate
                # the keyframe reference nothing it has. The client's decoder
                # swallows these too and asks for an IDR. What matters is that
                # a picture arrives once the keyframe does.
                continue

        assert decoded > 0, (
            f"{encoder} ignored the keyframe request: a viewer joining an "
            "existing stream would never see a picture"
        )


class TestThePreviewKeepsEncoding:
    """A real capture device froze the preview on its first picture.

    The cause was a timestamp without its base. A reformatted frame inherits
    the *capture's* time base -- a webcam's is 1/10000000 -- so a small
    per-frame counter written as pts rescales to 0 in the encoder's own base,
    and from the second frame `avcodec_send_frame` rejects them all as
    non-monotonic.

    It could not be seen with the test pattern: lavfi's time base is coarse
    enough that the same counter rescales to distinct values. So this builds
    frames with a webcam-like base explicitly, and needs no hardware.
    """

    def _frames(self, count: int, time_base, width=1280, height=720, fmt="yuvj422p"):
        """Frames shaped like a webcam's: fine time base, MJPEG pixel format."""
        from fractions import Fraction

        made = []
        for index in range(count):
            frame = av.VideoFrame(width, height, fmt)
            for plane in frame.planes:
                plane.update(bytes([(index * 17) % 251]) * plane.buffer_size)
            frame.time_base = time_base
            # Large, realistic timestamps -- a camera counts from its own epoch.
            frame.pts = 1_380_637_013_221 + index * 333_333
            made.append(frame)
        return made

    def test_a_webcam_time_base_does_not_freeze_it(self):
        from fractions import Fraction

        from videoserver.preview import PreviewEncoder

        encoder = PreviewEncoder()
        encoded = [encoder.encode(f) for f in self._frames(8, Fraction(1, 10_000_000))]

        assert all(encoded), (
            f"only {sum(1 for e in encoded if e)}/8 frames encoded -- "
            "the preview froze after the first"
        )
        assert encoder.errors == 0

    def test_a_coarse_time_base_still_works(self):
        """The test-pattern case, which always worked -- do not regress it."""
        from fractions import Fraction

        from videoserver.preview import PreviewEncoder

        encoder = PreviewEncoder()
        encoded = [encoder.encode(f) for f in self._frames(8, Fraction(1, 30))]
        assert all(encoded)

    def test_the_pictures_actually_differ(self):
        """Encoding without error is not enough; a frozen image would too."""
        from fractions import Fraction

        from videoserver.preview import PreviewEncoder

        encoder = PreviewEncoder()
        encoded = [encoder.encode(f) for f in self._frames(6, Fraction(1, 10_000_000))]
        assert len(set(encoded)) == len(encoded), "every preview frame was identical"

    def test_the_output_is_a_jpeg(self):
        from fractions import Fraction

        from videoserver.preview import PreviewEncoder

        data = PreviewEncoder().encode(self._frames(1, Fraction(1, 10_000_000))[0])
        assert data is not None
        assert data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")

    def test_a_source_that_changes_size_is_handled(self):
        """Switching capture device mid-run rebuilds the encoder context."""
        from fractions import Fraction

        from videoserver.preview import PreviewEncoder

        encoder = PreviewEncoder()
        assert encoder.encode(self._frames(1, Fraction(1, 10_000_000))[0])
        small = self._frames(1, Fraction(1, 10_000_000), width=640, height=480)[0]
        assert encoder.encode(small)
