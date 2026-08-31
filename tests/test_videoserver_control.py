"""The video control plane on the Bluetooth server.

Runs a real Datapath, SessionManager and Router in-process with mock Bluetooth,
and drives it with real ClientTransports. What matters here is the *contract*
between the three parties -- source, server, and player -- because each failure
below looks like something else entirely from the outside:

  * A video source counted as a player: the fourth person to join is refused
    with "server full" and nothing explains why.
  * The loopback allowance too wide or too narrow: either an embedded source
    cannot reach its own parent, or a switched-off server is answering
    strangers.
  * A config that is never acknowledged and never re-pushed: the operator
    changes a setting in the web GUI and it silently does not apply.
"""

from __future__ import annotations

import time

import pytest

from common.protocol import ControlOp
from common.video import VideoSettings
from client.net.transport import ClientTransport, TransportError
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.video import MODE_EMBEDDED, MODE_EXTERNAL, MODE_OFF, VideoRegistry

PASSWORD = "video-control-test-password"


@pytest.fixture
def server():
    """A real server stack on loopback, with mock Bluetooth."""
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr="00:00:00:00:00:01",
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(PASSWORD, max_clients=4, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EXTERNAL)
    datapath = Datapath(
        sessions,
        router,
        bind_host="127.0.0.1",
        bind_port=0,
        realtime=False,
        video_registry=registry,
    )
    datapath.start()
    datapath.set_accepting(lan=True, internet=False)

    yield datapath, registry, sessions, router

    datapath.stop()


def connect(port: int, *, role: str | None = None, name: str = "peer") -> ClientTransport:
    extra = {"role": role} if role else None
    transport = ClientTransport(
        PASSWORD, client_name=name, auth_extra=extra, rumble_enabled=False
    )
    transport.connect("127.0.0.1", port, timeout_ns=5_000_000_000)
    return transport


def pump(transports, seconds: float = 0.5) -> None:
    """Service transports for a while, the way their owning loops would."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for transport in transports:
            transport.service()
        time.sleep(0.01)


#: Generous on purpose. These wait on real loopback UDP round trips, and the
#: loop exits the moment the predicate holds -- so a longer budget costs nothing
#: when things work and only buys patience when the whole suite is competing for
#: the CPU. At 5 s this flaked intermittently under full-suite load, which is
#: worse than useless: a test that fails for reasons unrelated to the code it
#: covers trains you to ignore it.
_WAIT_TIMEOUT_S = 15.0


def wait_for(predicate, timeout: float = _WAIT_TIMEOUT_S, transports=()) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for transport in transports:
            transport.service()
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Collector:
    """Records the control messages a client receives."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def __call__(self, body: dict) -> None:
        self.messages.append(body)

    def latest(self, op: str) -> dict | None:
        for body in reversed(self.messages):
            if body.get("op") == op:
                return body
        return None


def attach_source(registry, datapath, media_port: int = 47810) -> None:
    """Stand in for the outbound link coming up.

    The Bluetooth server dials the video server now, so there is no inbound
    session to fake -- what a test needs is the registry state that link
    produces.
    """
    registry.attach_source_endpoint("127.0.0.1", media_port)
    sync_source(registry, datapath, media_port)


def sync_source(registry, datapath, media_port: int = 47810) -> None:
    """One round trip with the video server: it acknowledges what we sent.

    Echoing the *current* cfg_seq is the point. Minting a ticket bumps it, and
    until the source acknowledges that sequence the client is deliberately told
    there is no video -- otherwise it would be sent to a source that has never
    heard of its ticket.
    """
    registry.update_status_from_link(
        {
            "cfg_seq": registry.cfg_seq,
            "media_port": media_port,
            "lan_host": "192.168.1.50",
            "status": {"streaming": True, "encoder": "libx264", "clients": 0},
        }
    )
    datapath.broadcast_video_source()


class TestSourceAttachment:
    def test_a_source_attaches_and_is_told_the_configuration(self, server):
        datapath, registry, _sessions, _router = server
        collector = Collector()
        source = ClientTransport(
            PASSWORD,
            client_name="capture-pc",
            auth_extra={"role": "video-source"},
            on_control=collector,
            rumble_enabled=False,
        )
        source.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        try:
            assert wait_for(lambda: registry.has_source, transports=[source])
            # Configuration is pushed unprompted, so the source can start
            # capturing without asking first.
            assert wait_for(
                lambda: collector.latest(ControlOp.VIDEO_CONFIG) is not None,
                transports=[source],
            )
            config = collector.latest(ControlOp.VIDEO_CONFIG)
            assert "cfg_seq" in config
            # ...but settings are absent while nothing has been configured
            # here, so a source that was already set up in front of its capture
            # card is not reset by the act of connecting. See
            # tests/test_video_settings_ownership.py.
            assert "config" not in config

            # Once the operator chooses, settings are in the message again.
            # Asserted on the registry rather than on the wire: the datapath
            # sends VIDEO_CONFIG once, when the source attaches, and re-pushing
            # is the video link's job (see `_answer_video_query`).
            registry.set_config(VideoSettings(width=1280, height=720, fps=60))
            assert registry.config_message()["config"]["width"] == 1280
        finally:
            source.close()

    def test_a_source_takes_no_controller_slot(self, server):
        """Otherwise the last player to join is refused with no explanation."""
        datapath, _registry, sessions, router = server
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            assert wait_for(lambda: sessions.count == 1, transports=[source])
            assert sessions.controller_count == 0
            # The router must not have handed it an adapter either.
            assert all(not c.is_assigned for c in router.channels())
        finally:
            source.close()

    def test_losing_the_source_is_broadcast_to_players(self, server):
        datapath, registry, _sessions, _router = server
        collector = Collector()
        player = ClientTransport(
            PASSWORD, client_name="player", on_control=collector, rumble_enabled=False
        )
        player.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        try:
            attach_source(registry, datapath)
            sync_source(registry, datapath)      # acknowledge the new ticket
            assert wait_for(
                lambda: (collector.latest(ControlOp.VIDEO_SOURCE) or {}).get("available"),
                transports=[player],
            )

            # The link to the video server drops.
            registry.detach_source("video-link")
            datapath.broadcast_video_source()
            assert wait_for(
                lambda: (collector.latest(ControlOp.VIDEO_SOURCE) or {}).get("available")
                is False,
                transports=[player],
            )
        finally:
            player.close()


class TestAdvertising:
    def test_a_player_is_told_where_the_video_is(self, server):
        datapath, registry, _sessions, _router = server
        collector = Collector()
        player = ClientTransport(
            PASSWORD, client_name="player", on_control=collector, rumble_enabled=False
        )
        player.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        try:
            attach_source(registry, datapath)
            player.queue_control(ControlOp.VIDEO_QUERY, {})
            pump([player], 0.3)
            sync_source(registry, datapath)      # acknowledge the minted ticket

            assert wait_for(
                lambda: (collector.latest(ControlOp.VIDEO_SOURCE) or {}).get("available"),
                transports=[player],
            )
            advert = collector.latest(ControlOp.VIDEO_SOURCE)
            assert advert["port"] == 47810
            assert advert["lan_host"] == "192.168.1.50"
        finally:
            player.close()

    def test_a_query_with_no_source_says_so(self, server):
        datapath, _registry, _sessions, _router = server
        collector = Collector()
        player = ClientTransport(
            PASSWORD, client_name="player", on_control=collector, rumble_enabled=False
        )
        player.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        try:
            player.queue_control(ControlOp.VIDEO_QUERY, {})
            assert wait_for(
                lambda: collector.latest(ControlOp.VIDEO_SOURCE) is not None,
                transports=[player],
            )
            assert collector.latest(ControlOp.VIDEO_SOURCE)["available"] is False
        finally:
            player.close()

    def test_mode_off_hides_an_attached_source(self, server):
        """Switching video off must stop advertising it, not just stop new ones."""
        datapath, registry, _sessions, _router = server
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            attach_source(registry, datapath)
            assert registry.source_advert()["available"] is True

            registry.mode = MODE_OFF
            assert registry.source_advert()["available"] is False
        finally:
            source.close()


class TestAdvertisedAddress:
    """Where clients are told to look, when that is not where we see the source.

    Behind an frp UDP proxy or a port forward the address we reach the source at
    is on the *far* side of the forwarder and unreachable from a client. Both
    overrides existed as fields and neither was ever assigned, so there was no
    way to say otherwise -- the advert always handed out the wrong address.
    """

    def test_by_default_it_advertises_the_source(self, server):
        datapath, registry, _sessions, _router = server
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            attach_source(registry, datapath)
            advert = registry.source_advert()

            assert advert["port"] == 47810
        finally:
            source.close()

    def test_an_advertised_port_overrides_the_bound_one(self, server):
        """frp's remote_port need not equal local_port."""
        datapath, registry, _sessions, _router = server
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            attach_source(registry, datapath)
            registry.advertise_port = 51810
            advert = registry.source_advert()

            assert advert["port"] == 51810
        finally:
            source.close()

    def test_the_lan_host_still_carries_the_short_path(self, server):
        """A viewer on the capture PC's own network must not be sent via a VPS."""
        datapath, registry, _sessions, _router = server
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            attach_source(registry, datapath)
            registry.advertise_host = "vps.example.com"
            registry.advertise_port = 51810
            advert = registry.source_advert()

            assert advert["host"] == "vps.example.com"
            # The client's ladder tries lan_host first, so this is what keeps a
            # local viewer off the tunnel.
            assert advert["lan_host"] != "vps.example.com"
        finally:
            source.close()


class TestConfiguration:
    def test_config_is_offered_again_until_acknowledged(self, server):
        """The server -> client direction has no retransmit; this is the retry."""
        _datapath, registry, _sessions, _router = server
        registry.attach_source_endpoint("127.0.0.1", 47810)
        seq = registry.set_config(VideoSettings(width=640, height=480, fps=30))

        def push() -> bool:
            """Ask, and actually send when told to -- as the link does.

            The two belong together: `config_message()` is what records what
            the source has been told, including the live preview state. Asking
            without sending leaves that unrecorded, so it would keep asking.
            """
            if not registry.needs_config_push():
                return False
            registry.config_message()
            return True

        assert push() is True
        # Rate limited in between, so a stalled source is not hammered.
        assert push() is False

        registry._last_pushed_ns = 0          # as if the interval had elapsed
        assert push() is True, (
            "the server gave up on an unacknowledged configuration"
        )

        # Acknowledged, so it stops.
        registry.update_status_from_link(
            {"cfg_seq": seq, "media_port": 47810, "status": {}}
        )
        registry._last_pushed_ns = 0
        assert push() is False

    def test_only_the_link_may_consume_the_push_flag(self, server):
        """`needs_config_push()` *records* that a push happened.

        A second caller that consumes it and then sends nothing swallows the
        link's next attempt. That is not hypothetical: the datapath used to ask
        on every video query, and with a client querying twice a second a newly
        minted ticket almost never reached the source -- so the player waited
        on an advert that stayed unavailable.
        """
        datapath, registry, _sessions, _router = server
        assert not hasattr(datapath, "_push_video_config"), (
            "the datapath must not push configuration; that is the link's job"
        )

    def test_embedded_mode_caps_what_a_pi_is_asked_to_encode(self):
        """The Pi 5 has no hardware H.264 encoder; 1080p60 is not on offer."""
        registry = VideoRegistry(mode=MODE_EMBEDDED)
        registry.set_config(
            VideoSettings(width=1920, height=1080, fps=60, bitrate_kbps=20000)
        )
        settings = registry.settings
        assert settings.width <= 1280
        assert settings.height <= 720
        assert settings.fps <= 30
        assert settings.bitrate_kbps <= 6000

    def test_external_mode_does_not_cap(self, server):
        """A capture PC with a real GPU should not inherit the Pi's limits."""
        _datapath, registry, _sessions, _router = server
        registry.set_config(
            VideoSettings(width=1920, height=1080, fps=60, bitrate_kbps=20000)
        )
        assert registry.settings.width == 1920
        assert registry.settings.fps == 60


class TestPreviewSecurity:
    def test_a_controller_session_cannot_feed_the_preview(self, server):
        """The one path that retains a client's bytes must be role-gated."""
        datapath, registry, _sessions, _router = server
        from common import video as video_wire

        player = connect(datapath.port, name="player")
        try:
            buf = bytearray(2048)
            size = video_wire.encode_video_slice_into(
                buf, 0, 1, 0, 1, video_wire.SliceFlags.KEYFRAME,
                video_wire.MediaCodec.MJPEG, 0, b"\xff\xd8pretend-jpeg\xff\xd9",
            )
            player.send_unreliable(bytes(buf[:size]))
            pump([player], 0.4)

            assert registry.preview() is None, "a player fed the operator's preview"
        finally:
            player.close()

    def test_the_source_can_feed_the_preview(self, server):
        datapath, registry, _sessions, _router = server
        from common import video as video_wire

        source = connect(datapath.port, role="video-source", name="capture")
        try:
            payload = b"\xff\xd8" + b"jpeg-bytes" * 40 + b"\xff\xd9"
            buf = bytearray(2048)
            size = video_wire.encode_video_slice_into(
                buf, 0, 1, 0, 1, video_wire.SliceFlags.KEYFRAME,
                video_wire.MediaCodec.MJPEG, 0, payload,
            )
            source.send_unreliable(bytes(buf[:size]))
            assert wait_for(lambda: registry.preview() is not None, transports=[source])
            assert registry.preview() == payload
        finally:
            source.close()


class TestLoopbackGate:
    """The allowance that lets an embedded source reach its own parent."""

    def test_loopback_is_refused_when_the_allowance_is_off(self, server):
        datapath, _registry, _sessions, _router = server
        datapath.set_accepting(lan=False, internet=False)
        datapath.allow_loopback_video = False

        with pytest.raises(TransportError):
            connect(datapath.port, role="video-source", name="capture")

    def test_loopback_is_admitted_with_both_gates_off(self, server):
        """Embedded video must work on a server that accepts nobody."""
        datapath, registry, _sessions, _router = server
        datapath.set_accepting(lan=False, internet=False)
        datapath.allow_loopback_video = True

        source = connect(datapath.port, role="video-source", name="capture")
        try:
            assert wait_for(lambda: registry.has_source, transports=[source])
        finally:
            source.close()

    def test_turning_lan_off_does_not_drop_an_embedded_source(self, server):
        """It belongs to neither transport the operator is switching."""
        datapath, registry, sessions, _router = server
        datapath.allow_loopback_video = True

        source = connect(datapath.port, role="video-source", name="capture")
        try:
            assert wait_for(lambda: registry.has_source, transports=[source])
            assert sessions.count == 1

            datapath.set_accepting(lan=False)
            assert registry.has_source, "switching LAN off killed the embedded source"
            assert sessions.count == 1
        finally:
            source.close()

    def test_a_loopback_player_is_still_dropped_with_everyone_else(self, server):
        """The exemption is for the video source, not for any local process.

        Every peer in this test arrives over loopback, so if the allowance were
        keyed on address alone a player would silently survive a gate the
        operator just closed.
        """
        datapath, _registry, sessions, _router = server
        datapath.allow_loopback_video = True

        player = connect(datapath.port, name="player")
        try:
            assert wait_for(lambda: sessions.controller_count == 1, transports=[player])
            datapath.set_accepting(lan=False)
            assert sessions.controller_count == 0
        finally:
            player.close()


class TestOnlyApprovedClientsMayWatch:
    """The video socket shares its password with every player, so the password
    alone cannot tell an approved client from a denied one. Without a second
    check, pressing *Deny* took someone's controller away and left them
    watching -- the button only half worked.

    A ticket closes it: the Bluetooth server issues one to each client it
    approves, and the source refuses anyone who cannot present a current one.
    """

    def test_an_approved_client_is_given_a_ticket(self, server):
        datapath, registry, _sessions, _router = server
        collector = Collector()
        player = ClientTransport(
            PASSWORD, client_name="player", on_control=collector, rumble_enabled=False
        )
        player.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        try:
            attach_source(registry, datapath)
            player.queue_control(ControlOp.VIDEO_QUERY, {})
            pump([player], 0.3)
            sync_source(registry, datapath)

            assert wait_for(
                lambda: (collector.latest(ControlOp.VIDEO_SOURCE) or {}).get("available"),
                transports=[player],
            )
            assert collector.latest(ControlOp.VIDEO_SOURCE)["ticket"]
        finally:
            player.close()

    def test_a_pending_client_is_told_there_is_no_video(self, server):
        """Not an endpoint it would be refused at -- there is nothing for it."""
        datapath, registry, sessions, _router = server
        sessions.auto_approve = False

        collector = Collector()
        player = ClientTransport(
            PASSWORD, client_name="pending", on_control=collector, rumble_enabled=False
        )
        player.connect("127.0.0.1", datapath.port, timeout_ns=5_000_000_000)
        source = connect(datapath.port, role="video-source", name="capture")
        try:
            attach_source(registry, datapath)
            player.queue_control(ControlOp.VIDEO_QUERY, {})
            assert wait_for(
                lambda: collector.latest(ControlOp.VIDEO_SOURCE) is not None,
                transports=[player, source],
            )
            advert = collector.latest(ControlOp.VIDEO_SOURCE)
            assert advert["available"] is False
            assert "ticket" not in advert
        finally:
            player.close()
            source.close()
            sessions.auto_approve = True

    def test_a_ticket_is_not_advertised_until_the_source_has_it(self, server):
        """Otherwise the advert races the configuration carrying its ticket.

        The client acts on an advert the moment it arrives; if the source has
        not been told about that ticket yet the handshake is refused with "the
        operator denied this connection" -- alarming, and untrue.
        """
        _datapath, registry, _sessions, _router = server
        registry.attach_source_endpoint("127.0.0.1", 47810)
        registry.update_status_from_link(
            {"cfg_seq": registry.cfg_seq, "media_port": 47810, "status": {}}
        )

        registry.ticket_for("client-1")
        assert registry.source_advert("client-1")["available"] is False, (
            "a client was sent to a source that has never heard of its ticket"
        )

        registry.update_status_from_link(
            {"cfg_seq": registry.cfg_seq, "media_port": 47810, "status": {}}
        )
        advert = registry.source_advert("client-1")
        assert advert["available"] is True
        assert advert["ticket"]

    def test_one_newcomer_does_not_interrupt_everybody_else(self, server):
        """A global in-sync flag would; the client tears down video when told
        it is unavailable, so a joiner would blink out every other player."""
        _datapath, registry, _sessions, _router = server
        registry.attach_source_endpoint("127.0.0.1", 47810)
        registry.ticket_for("early")
        registry.update_status_from_link(
            {"cfg_seq": registry.cfg_seq, "media_port": 47810, "status": {}}
        )
        assert registry.source_advert("early")["available"] is True

        registry.ticket_for("latecomer")      # bumps the configuration
        assert registry.source_advert("early")["available"] is True
        assert registry.source_advert("latecomer")["available"] is False

    def test_revoking_marks_the_config_stale(self, server):
        _datapath, registry, _sessions, _router = server
        registry.ticket_for("abc123")
        before = registry.cfg_seq

        assert registry.revoke_ticket("abc123") is True
        assert registry.cfg_seq > before, "the source would never learn of the revocation"
        assert registry.valid_tickets() == set()

    def test_revoking_an_unknown_client_is_harmless(self, server):
        _datapath, registry, _sessions, _router = server
        before = registry.cfg_seq
        assert registry.revoke_ticket("never-seen") is False
        assert registry.cfg_seq == before

    def test_a_ticket_is_stable_across_repeated_adverts(self, server):
        """Re-issuing on every advert would churn the config for no reason."""
        _datapath, registry, _sessions, _router = server
        first = registry.ticket_for("client-1")
        seq = registry.cfg_seq

        assert registry.ticket_for("client-1") == first
        assert registry.cfg_seq == seq

    def test_a_departing_client_loses_its_ticket(self, server):
        datapath, registry, _sessions, _router = server
        player = connect(datapath.port, name="player")
        try:
            player.queue_control(ControlOp.VIDEO_QUERY, {})
            assert wait_for(lambda: registry.valid_tickets(), transports=[player])
        finally:
            player.close()

        assert wait_for(lambda: not registry.valid_tickets(), timeout=15.0), (
            "an approved-once client kept the right to watch forever"
        )
