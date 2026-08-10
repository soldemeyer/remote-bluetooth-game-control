"""Crypto handshake and AEAD session tests.

Focused on the properties that actually matter: wrong passwords fail, sessions
are unique per connection, tampering is detected, and the two directions never
share a nonce.
"""

from __future__ import annotations

import pytest

from common.crypto import (
    KEY_SIZE,
    SessionCrypto,
    CryptoError,
    compute_auth_proof,
    derive_master_key,
    derive_session_keys,
    new_client_id,
    new_random,
    new_salt,
    verify_auth_proof,
)

# Argon2id is deliberately slow, so derive the common-case keys once.
PASSWORD = "hunter2-correct-horse"
SALT = b"\x01" * 16


@pytest.fixture(scope="module")
def master_key() -> bytes:
    return derive_master_key(PASSWORD, SALT)


def test_master_key_is_deterministic(master_key):
    assert derive_master_key(PASSWORD, SALT) == master_key
    assert len(master_key) == KEY_SIZE


def test_different_password_gives_different_key(master_key):
    assert derive_master_key("wrong-password", SALT) != master_key


def test_different_salt_gives_different_key(master_key):
    assert derive_master_key(PASSWORD, b"\x02" * 16) != master_key


def test_rejects_wrong_salt_size():
    with pytest.raises(ValueError, match="salt must be"):
        derive_master_key(PASSWORD, b"tooshort")


def test_session_keys_differ_per_connection(master_key):
    """Fresh randomness each connection means a captured transcript from one
    session cannot decrypt another."""
    k1, _ = derive_session_keys(master_key, new_random(), new_random())
    k2, _ = derive_session_keys(master_key, new_random(), new_random())
    assert k1 != k2


def test_both_sides_derive_identical_keys(master_key):
    cr, sr = new_random(), new_random()
    assert derive_session_keys(master_key, cr, sr) == derive_session_keys(master_key, cr, sr)


def test_session_key_and_proof_key_are_distinct(master_key):
    session_key, proof_key = derive_session_keys(master_key, new_random(), new_random())
    assert session_key != proof_key


class TestAuthProof:
    def test_valid_proof_verifies(self, master_key):
        _, proof_key = derive_session_keys(master_key, new_random(), new_random())
        cid = new_client_id()
        assert verify_auth_proof(proof_key, cid, 1, compute_auth_proof(proof_key, cid, 1))

    def test_wrong_password_fails(self, master_key):
        cr, sr = new_random(), new_random()
        _, good = derive_session_keys(master_key, cr, sr)
        _, bad = derive_session_keys(derive_master_key("nope", SALT), cr, sr)

        cid = new_client_id()
        assert not verify_auth_proof(good, cid, 1, compute_auth_proof(bad, cid, 1))

    def test_proof_is_bound_to_client_id(self, master_key):
        """A proof captured from one client must not work for another."""
        _, proof_key = derive_session_keys(master_key, new_random(), new_random())
        proof = compute_auth_proof(proof_key, new_client_id(), 1)
        assert not verify_auth_proof(proof_key, new_client_id(), 1, proof)

    def test_proof_is_bound_to_version(self, master_key):
        """Downgrade attempts must fail authentication."""
        _, proof_key = derive_session_keys(master_key, new_random(), new_random())
        cid = new_client_id()
        assert not verify_auth_proof(proof_key, cid, 2, compute_auth_proof(proof_key, cid, 1))


class TestSessionCrypto:
    @pytest.fixture
    def pair(self, master_key):
        session_key, _ = derive_session_keys(master_key, new_random(), new_random())
        return SessionCrypto.for_client(session_key), SessionCrypto.for_server(session_key)

    def test_client_to_server_round_trip(self, pair):
        client, server = pair
        plaintext = b"controller state payload"
        counter, decrypted = server.decrypt(client.encrypt(plaintext))
        assert decrypted == plaintext
        assert counter == 0

    def test_server_to_client_round_trip(self, pair):
        client, server = pair
        counter, decrypted = client.decrypt(server.encrypt(b"ack"))
        assert decrypted == b"ack"
        assert counter == 0

    def test_counter_increments(self, pair):
        client, server = pair
        for expected in range(5):
            counter, _ = server.decrypt(client.encrypt(b"x"))
            assert counter == expected

    def test_directions_use_disjoint_nonce_space(self, pair):
        """Both directions share a key, so identical plaintext at the same
        counter must still produce different ciphertext -- otherwise the two
        directions could collide on (key, nonce), which catastrophically
        breaks ChaCha20-Poly1305."""
        client, server = pair
        assert client.encrypt(b"same") != server.encrypt(b"same")

    def test_tampering_is_detected(self, pair):
        client, server = pair
        blob = bytearray(client.encrypt(b"important state"))
        blob[-1] ^= 0x01
        with pytest.raises(CryptoError, match="authentication failed"):
            server.decrypt(bytes(blob))

    def test_counter_tampering_is_detected(self, pair):
        """The counter travels in the clear but is authenticated as AD."""
        client, server = pair
        blob = bytearray(client.encrypt(b"state"))
        blob[0] ^= 0xFF
        with pytest.raises(CryptoError):
            server.decrypt(bytes(blob))

    def test_wrong_key_fails(self, pair, master_key):
        client, _ = pair
        other_key, _ = derive_session_keys(master_key, new_random(), new_random())
        with pytest.raises(CryptoError):
            SessionCrypto.for_server(other_key).decrypt(client.encrypt(b"secret"))

    def test_truncated_ciphertext_rejected(self, pair):
        _, server = pair
        with pytest.raises(CryptoError, match="too short"):
            server.decrypt(b"\x00" * 4)

    def test_rejects_bad_key_size(self):
        with pytest.raises(ValueError, match="key must be"):
            SessionCrypto.for_client(b"short")

    def test_empty_plaintext_round_trips(self, pair):
        client, server = pair
        assert server.decrypt(client.encrypt(b""))[1] == b""

    def test_overhead_is_bounded(self, pair):
        """1-byte outer tag + 8-byte counter + 16-byte Poly1305 tag. Guards
        against a change that would push the hot-path datagram toward the MTU."""
        client, _ = pair
        assert len(client.encrypt(b"x" * 31)) == 31 + 1 + 8 + 16

    def test_ciphertext_carries_the_session_tag(self, pair):
        """The outer tag is what stops an encrypted packet whose counter is 1
        from being dispatched as a plaintext HELLO."""
        from common.protocol import PacketType

        client, _ = pair
        assert client.encrypt(b"x")[0] == PacketType.SESSION

    def test_rejects_datagram_without_session_tag(self, pair):
        client, server = pair
        blob = bytearray(client.encrypt(b"state"))
        blob[0] = 0x01  # masquerade as HELLO
        with pytest.raises(CryptoError, match="not a session datagram"):
            server.decrypt(bytes(blob))


def test_randoms_are_unique():
    assert len({new_random() for _ in range(100)}) == 100
    assert len({new_salt() for _ in range(100)}) == 100
    assert len({new_client_id() for _ in range(100)}) == 100
