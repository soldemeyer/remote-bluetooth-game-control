"""Session roles: a video source is not a player.

The role travels inside the encrypted AUTH payload, so it is authenticated
before it is trusted. Two things must hold, and both are the kind of bug that
only shows up when the room is full:

  * A video source must not consume one of the four controller slots. Getting
    this wrong means the last player to join is refused for no visible reason.
  * A peer that declares nothing -- every client built before video existed --
    must still be admitted as a controller.
"""

from __future__ import annotations

import pytest

from common import crypto, protocol
from common.protocol import PROTOCOL_VERSION, ControlOp, PacketType, RejectReason
from server.sessions import (
    ROLE_CONTROLLER,
    ROLE_VIDEO_SOURCE,
    SessionManager,
)

PASSWORD = "role-test-password"


@pytest.fixture(scope="module")
def manager_factory():
    """Argon2id runs once per construction, so build as few as possible."""

    def build(**kwargs) -> SessionManager:
        return SessionManager(PASSWORD, **kwargs)

    return build


def handshake(manager: SessionManager, address, *, role=None, name="pad"):
    """Full HELLO/AUTH against a real SessionManager. Returns (response, session)."""
    client_id = crypto.new_client_id()
    client_random = crypto.new_random()
    hello = (
        bytes([PacketType.HELLO])
        + PROTOCOL_VERSION.to_bytes(2, "little")
        + client_id
        + client_random
    )
    challenge = manager.handle_hello(hello, address)
    assert challenge[0] == PacketType.CHALLENGE

    salt = challenge[1 : 1 + crypto.SALT_SIZE]
    server_random = challenge[1 + crypto.SALT_SIZE : 1 + crypto.SALT_SIZE + crypto.RANDOM_SIZE]

    master = crypto.derive_master_key(PASSWORD, salt)
    session_key, proof_key = crypto.derive_session_keys(master, client_random, server_random)
    proof = crypto.compute_auth_proof(proof_key, client_id, PROTOCOL_VERSION)

    auth = bytes([PacketType.AUTH]) + client_id + proof
    if role is not None or name is not None:
        payload: dict[str, object] = {"client_name": name}
        if role is not None:
            payload["role"] = role
        info = protocol.encode_control(0, ControlOp.SET_CONTROLLERS, payload)
        auth += crypto.SessionCrypto.for_client(session_key).encrypt(info)

    return manager.handle_auth(auth, address, capacity=4)


class TestRoleParsing:
    def test_a_client_declaring_nothing_is_a_controller(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        _, session = handshake(manager, ("10.0.0.1", 5000), role=None)
        assert session is not None
        assert session.role == ROLE_CONTROLLER

    def test_a_video_source_is_recorded_as_one(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        _, session = handshake(manager, ("10.0.0.2", 5000), role=ROLE_VIDEO_SOURCE)
        assert session is not None
        assert session.role == ROLE_VIDEO_SOURCE

    def test_an_unknown_role_falls_back_to_controller(self, manager_factory):
        """Never trust a declared string into a privileged branch."""
        manager = manager_factory(auto_approve=True)
        _, session = handshake(manager, ("10.0.0.3", 5000), role="administrator")
        assert session is not None
        assert session.role == ROLE_CONTROLLER

    def test_an_auth_with_no_payload_at_all_still_authenticates(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        _, session = handshake(manager, ("10.0.0.4", 5000), role=None, name=None)
        assert session is not None
        assert session.role == ROLE_CONTROLLER

    def test_role_appears_in_the_snapshot(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        handshake(manager, ("10.0.0.5", 5000), role=ROLE_VIDEO_SOURCE)
        roles = [entry["role"] for entry in manager.snapshot()]
        assert roles == [ROLE_VIDEO_SOURCE]


class TestCapacityAccounting:
    def test_a_video_source_does_not_consume_a_player_slot(self, manager_factory):
        """The whole point of the role: watching is not playing."""
        manager = manager_factory(auto_approve=True, max_clients=2)

        _, source = handshake(manager, ("10.0.1.1", 5000), role=ROLE_VIDEO_SOURCE)
        assert source is not None

        for index in range(2):
            response, session = handshake(manager, ("10.0.1.2", 5000 + index))
            assert session is not None, "a controller was refused because of the video source"
            assert response[0] == PacketType.ACCEPT

        assert manager.controller_count == 2
        assert manager.count == 3

    def test_controllers_are_still_capped(self, manager_factory):
        manager = manager_factory(auto_approve=True, max_clients=2)
        handshake(manager, ("10.0.2.1", 5000), role=ROLE_VIDEO_SOURCE)
        handshake(manager, ("10.0.2.2", 5001))
        handshake(manager, ("10.0.2.3", 5002))

        response, session = handshake(manager, ("10.0.2.4", 5003))
        assert session is None
        assert response[0] == PacketType.REJECT
        assert response[1] == RejectReason.SERVER_FULL

    def test_a_video_source_is_admitted_even_when_players_are_full(self, manager_factory):
        manager = manager_factory(auto_approve=True, max_clients=1)
        handshake(manager, ("10.0.3.1", 5000))

        response, session = handshake(manager, ("10.0.3.2", 5001), role=ROLE_VIDEO_SOURCE)
        assert session is not None
        assert response[0] == PacketType.ACCEPT

    def test_non_controller_roles_are_still_bounded(self, manager_factory):
        """Exempting a role from the player limit must not exempt it from any.

        Otherwise anyone holding the password can open unbounded sessions just
        by declaring a different role, and each one costs an Argon2id
        derivation plus permanent server-side state.
        """
        from server.sessions import MAX_AUXILIARY_SESSIONS

        manager = manager_factory(auto_approve=True, max_clients=2)
        ceiling = 2 + MAX_AUXILIARY_SESSIONS

        admitted = 0
        for index in range(ceiling + 4):
            _response, session = handshake(
                manager, ("10.0.5.1", 6000 + index), role=ROLE_VIDEO_SOURCE
            )
            if session is not None:
                admitted += 1

        assert admitted <= ceiling, f"{admitted} auxiliary sessions were admitted"
        assert admitted >= 1, "the first video source must always get in"

    def test_video_source_lookup_finds_it(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        handshake(manager, ("10.0.4.1", 5000))
        assert manager.video_source() is None

        _, source = handshake(manager, ("10.0.4.2", 5001), role=ROLE_VIDEO_SOURCE)
        found = manager.video_source()
        assert found is not None
        assert found.client_id == source.client_id
