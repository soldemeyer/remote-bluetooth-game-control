"""Session management: authentication, admission control, rate limiting."""

from __future__ import annotations

import pytest

from common import crypto
from common.protocol import PROTOCOL_VERSION, PacketType, RejectReason
from server.sessions import (
    MAX_ATTEMPTS_PER_WINDOW,
    SessionManager,
    SessionState,
    generate_password,
)

PASSWORD = "session-test-password"


@pytest.fixture(scope="module")
def manager_factory():
    """Shared factory -- SessionManager derives an Argon2id key on construction,
    which is deliberately slow, so avoid building more than necessary."""

    def build(**kwargs) -> SessionManager:
        return SessionManager(PASSWORD, **kwargs)

    return build


def do_hello(manager: SessionManager, address, client_id=None, version=PROTOCOL_VERSION):
    client_id = client_id or crypto.new_client_id()
    client_random = crypto.new_random()
    hello = (
        bytes([PacketType.HELLO])
        + version.to_bytes(2, "little")
        + client_id
        + client_random
    )
    return client_id, client_random, manager.handle_hello(hello, address)


def do_auth(manager, address, client_id, client_random, challenge, password=PASSWORD):
    salt = challenge[1 : 1 + crypto.SALT_SIZE]
    server_random = challenge[1 + crypto.SALT_SIZE : 1 + crypto.SALT_SIZE + crypto.RANDOM_SIZE]

    master = crypto.derive_master_key(password, salt)
    _, proof_key = crypto.derive_session_keys(master, client_random, server_random)
    proof = crypto.compute_auth_proof(proof_key, client_id, PROTOCOL_VERSION)

    auth = bytes([PacketType.AUTH]) + client_id + proof
    return manager.handle_auth(auth, address, capacity=4)


class TestHandshake:
    def test_hello_returns_a_challenge(self, manager_factory):
        manager = manager_factory()
        _, _, response = do_hello(manager, ("10.0.0.1", 5000))

        assert response[0] == PacketType.CHALLENGE
        assert len(response) == 1 + crypto.SALT_SIZE + crypto.RANDOM_SIZE

    def test_version_mismatch_is_named_explicitly(self, manager_factory):
        """A generic failure here looks like a wrong password and sends people
        down the wrong path."""
        manager = manager_factory()
        _, _, response = do_hello(manager, ("10.0.0.1", 5000), version=99)

        assert response[0] == PacketType.REJECT
        assert response[1] == RejectReason.VERSION_MISMATCH

    def test_truncated_hello_is_rejected(self, manager_factory):
        manager = manager_factory()
        response = manager.handle_hello(b"\x01\x01", ("10.0.0.1", 5000))
        assert response[0] == PacketType.REJECT

    def test_correct_password_is_accepted(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.0.2", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        response, session = do_auth(manager, address, client_id, client_random, challenge)

        assert response[0] == PacketType.ACCEPT
        assert session is not None
        assert manager.count == 1

    def test_accept_carries_capacity(self, manager_factory):
        """This is what drives slot greying-out in the client GUI."""
        manager = manager_factory()
        address = ("10.0.0.3", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        response, _ = do_auth(manager, address, client_id, client_random, challenge)

        assert response[5] == 4

    def test_wrong_password_is_rejected(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.0.4", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        response, session = do_auth(
            manager, address, client_id, client_random, challenge, password="wrong"
        )

        assert response[0] == PacketType.REJECT
        assert response[1] == RejectReason.BAD_PASSWORD
        assert session is None
        assert manager.count == 0

    def test_auth_without_hello_is_rejected(self, manager_factory):
        """Guards against a replayed AUTH from a captured transcript."""
        manager = manager_factory()
        auth = bytes([PacketType.AUTH]) + crypto.new_client_id() + b"\x00" * 32
        response, session = manager.handle_auth(auth, ("10.0.0.5", 5000), capacity=4)

        assert response[0] == PacketType.REJECT
        assert session is None

    def test_server_full_is_rejected(self, manager_factory):
        manager = manager_factory(max_clients=1)

        for index, port in enumerate((5000, 5001)):
            address = ("10.0.0.6", port)
            client_id, client_random, challenge = do_hello(manager, address)
            response, _ = do_auth(manager, address, client_id, client_random, challenge)

            if index == 0:
                assert response[0] == PacketType.ACCEPT
            else:
                assert response[0] == PacketType.REJECT
                assert response[1] == RejectReason.SERVER_FULL

    def test_reconnect_replaces_the_previous_session(self, manager_factory):
        """A client that crashed must be able to return, not be told it is full."""
        manager = manager_factory(max_clients=1)
        client_id = crypto.new_client_id()

        for port in (5000, 5001):
            address = ("10.0.0.7", port)
            _, client_random, challenge = do_hello(manager, address, client_id=client_id)
            response, _ = do_auth(manager, address, client_id, client_random, challenge)
            assert response[0] == PacketType.ACCEPT

        assert manager.count == 1


class TestAdmission:
    def test_default_is_pending_approval(self, manager_factory):
        """Knowing the password gets you a session, not a controller."""
        manager = manager_factory(auto_approve=False)
        address = ("10.0.1.1", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        assert session.state is SessionState.PENDING
        assert not session.is_approved

    def test_auto_approve_skips_the_operator(self, manager_factory):
        manager = manager_factory(auto_approve=True)
        address = ("10.0.1.2", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        assert session.is_approved

    def test_approve_and_deny(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.1.3", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        assert manager.approve(session.client_id)
        assert session.is_approved

        assert manager.deny(session.client_id)
        assert session.state is SessionState.DENIED

    def test_approve_unknown_client_is_false(self, manager_factory):
        assert not manager_factory().approve("nope")


class TestRateLimiting:
    def test_repeated_failures_trigger_lockout(self, manager_factory):
        """Argon2id costs ~0.1 s per attempt, so this protects the Pi's CPU as
        much as it protects the password."""
        manager = manager_factory()
        address = ("10.0.2.1", 5000)

        for _ in range(MAX_ATTEMPTS_PER_WINDOW):
            client_id, client_random, challenge = do_hello(manager, address)
            if challenge[0] == PacketType.REJECT:
                break
            do_auth(manager, address, client_id, client_random, challenge, password="wrong")

        _, _, response = do_hello(manager, address)
        assert response[0] == PacketType.REJECT
        assert response[1] == RejectReason.RATE_LIMITED

    def test_lockout_is_per_address(self, manager_factory):
        """One player fat-fingering the password must not lock out the others."""
        manager = manager_factory()
        bad = ("10.0.2.2", 5000)

        for _ in range(MAX_ATTEMPTS_PER_WINDOW + 1):
            client_id, client_random, challenge = do_hello(manager, bad)
            if challenge[0] == PacketType.REJECT:
                break
            do_auth(manager, bad, client_id, client_random, challenge, password="wrong")

        good = ("10.0.2.3", 5000)
        _, _, response = do_hello(manager, good)
        assert response[0] == PacketType.CHALLENGE

    def test_success_clears_the_counter(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.2.4", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        do_auth(manager, address, client_id, client_random, challenge, password="wrong")

        client_id, client_random, challenge = do_hello(manager, address)
        response, _ = do_auth(manager, address, client_id, client_random, challenge)
        assert response[0] == PacketType.ACCEPT

        manager.reset_rate_limit(address[0])
        _, _, response = do_hello(manager, address)
        assert response[0] == PacketType.CHALLENGE


class TestSessionLifecycle:
    def test_lookup_by_address_and_client_id(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.3.1", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        assert manager.by_address(address) is session
        assert manager.by_client_id(session.client_id) is session

    def test_address_update_follows_nat_rebinding(self, manager_factory):
        """Safe because the packet that triggered it was already authenticated
        by the AEAD -- an attacker cannot redirect a session without the key."""
        manager = manager_factory()
        old = ("10.0.3.2", 5000)
        new = ("10.0.3.2", 6000)

        client_id, client_random, challenge = do_hello(manager, old)
        _, session = do_auth(manager, old, client_id, client_random, challenge)

        manager.update_address(session, new)

        assert manager.by_address(new) is session
        assert manager.by_address(old) is None

    def test_drop_removes_the_session(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.3.3", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        assert manager.drop(session.client_id)
        assert manager.count == 0
        assert manager.by_address(address) is None

    def test_slots_are_created_on_demand(self, manager_factory):
        manager = manager_factory()
        address = ("10.0.3.4", 5000)

        client_id, client_random, challenge = do_hello(manager, address)
        _, session = do_auth(manager, address, client_id, client_random, challenge)

        slot = session.slot(2)
        slot.username = "carol"

        assert session.slot(2).username == "carol"
        assert session.snapshot()["slots"][0]["username"] == "carol"


def test_empty_password_is_refused():
    """A server with no password would be open to anyone who can reach the port."""
    with pytest.raises(ValueError, match="password is required"):
        SessionManager("")


class TestGeneratedPasswords:
    def test_length_and_uniqueness(self):
        assert len(generate_password(16)) == 16
        assert len({generate_password() for _ in range(50)}) == 50

    def test_excludes_ambiguous_characters(self):
        """These get read aloud or typed off a screen."""
        combined = "".join(generate_password(40) for _ in range(20))
        for char in "0O1lI":
            assert char not in combined
