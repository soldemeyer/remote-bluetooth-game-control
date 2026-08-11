"""Password-authenticated session crypto.

Design constraints, in priority order:

1. Correct. Only standard libsodium primitives, composed in a standard way.
   Never invent crypto here.
2. Cheap on the hot path. AEAD of a 31-byte packet costs ~1-2 us, which is
   noise next to the 5-15 ms Bluetooth floor. There is *no* performance
   argument for weakening this, so don't.
3. The expensive part (Argon2id password stretching) happens exactly once per
   connection, during the handshake, never per packet.

Handshake
---------
    client -> server   HELLO      version, client_id, client_random
    server -> client   CHALLENGE  salt, server_random, kdf params
    client -> server   AUTH       proof, encrypted{usernames, controller info}
    server -> client   ACCEPT     session_id, capacity
                  or   REJECT     reason

Both sides derive the same session key from the password. The client proves it
knows the password by producing a MAC the server can recheck; the server
implicitly proves the same by being able to decrypt the AUTH payload.

Why not TLS/DTLS? We need a single UDP socket that also carries unreliable,
loss-tolerant traffic, and we need it to survive NAT rebinding. DTLS would add
a handshake RTT, a record layer we'd be fighting, and would not let us drop
stale packets. A shared-password AEAD channel is the right shape for this
problem and is a well-understood construction.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
import threading
from dataclasses import dataclass

import nacl.bindings
import nacl.exceptions
import nacl.pwhash
import nacl.utils

#: Argon2id parameters. The interactive profile is ~0.1 s / 64 MiB on a desktop.
#: Deliberately not higher: this runs on a Raspberry Pi, it runs on every
#: connection attempt, and the password is a LAN-party shared secret rather than
#: a password database entry. Rate limiting (see server/sessions.py) is what
#: actually stops online guessing.
ARGON2_OPS = nacl.pwhash.argon2id.OPSLIMIT_INTERACTIVE
ARGON2_MEM = nacl.pwhash.argon2id.MEMLIMIT_INTERACTIVE

SALT_SIZE = nacl.pwhash.argon2id.SALTBYTES     # 16
RANDOM_SIZE = 32
KEY_SIZE = 32                                   # ChaCha20-Poly1305 key
TAG_SIZE = 16                                   # Poly1305 tag
NONCE_SIZE = 12                                 # IETF ChaCha20-Poly1305
PROOF_SIZE = 32

#: Nonce construction: 4-byte direction/context prefix ‖ 8-byte counter.
#: Each direction has its own counter and its own prefix, so the two directions
#: can never collide on a (key, nonce) pair -- which is the one thing that
#: catastrophically breaks ChaCha20-Poly1305.
_NONCE_PREFIX_C2S = b"\x00\x00\x00\x01"
_NONCE_PREFIX_S2C = b"\x00\x00\x00\x02"

#: Outer framing byte prefixed to every encrypted datagram. Must match
#: ``protocol.PacketType.SESSION``; duplicated as a literal here to keep this
#: module free of a circular import back into protocol.
SESSION_TAG = b"\x40"

_COUNTER_STRUCT = struct.Struct("<Q")

#: Counter is 64-bit; at 1000 packets/s exhausting it takes ~584 million years.
#: We still assert on overflow rather than wrapping, because wrapping would
#: silently reuse a nonce.
_MAX_COUNTER = (1 << 64) - 1


class CryptoError(Exception):
    """Authentication or decryption failure. Always treat as 'drop the packet'."""


def derive_master_key(password: str, salt: bytes) -> bytes:
    """Stretch the shared password into a master key. Expensive -- call once."""
    if len(salt) != SALT_SIZE:
        raise ValueError(f"salt must be {SALT_SIZE} bytes, got {len(salt)}")

    return nacl.pwhash.argon2id.kdf(
        KEY_SIZE,
        password.encode("utf-8"),
        salt,
        opslimit=ARGON2_OPS,
        memlimit=ARGON2_MEM,
    )


def derive_session_keys(
    master_key: bytes, client_random: bytes, server_random: bytes
) -> tuple[bytes, bytes]:
    """Derive ``(session_key, auth_proof_key)`` from the master key and both randoms.

    Mixing in fresh randomness from both sides means each connection gets a
    distinct session key even though the password never changes. Without this,
    every session would share one key and a captured transcript would decrypt
    every future session.

    HKDF-SHA256 (extract skipped -- the master key is already uniformly random
    out of Argon2id, so we only need the expand step).
    """
    transcript = client_random + server_random

    session_key = _hkdf_expand(master_key, b"rbgc session key v1" + transcript, KEY_SIZE)
    proof_key = _hkdf_expand(master_key, b"rbgc auth proof v1" + transcript, KEY_SIZE)
    return session_key, proof_key


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF-Expand with SHA-256."""
    if length > 255 * 32:
        raise ValueError("requested key material too long")

    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        output += block
        counter += 1
    return output[:length]


def compute_auth_proof(proof_key: bytes, client_id: bytes, version: int) -> bytes:
    """MAC proving the client knows the password.

    Binds the client id and protocol version so a proof captured from one
    client cannot be replayed by another, and so a downgrade attempt fails
    authentication rather than silently negotiating an older format.
    """
    message = client_id + struct.pack("<I", version)
    return hmac.new(proof_key, message, hashlib.sha256).digest()


def verify_auth_proof(proof_key: bytes, client_id: bytes, version: int, proof: bytes) -> bool:
    """Constant-time proof check."""
    expected = compute_auth_proof(proof_key, client_id, version)
    return hmac.compare_digest(expected, proof)


@dataclass(slots=True)
class SessionCrypto:
    """Per-session AEAD state.

    One of these per client↔server pair, on each side. Send and receive
    counters are independent; the direction prefix keeps the two nonce spaces
    disjoint even though both directions share one key.
    """

    key: bytes
    send_prefix: bytes
    recv_prefix: bytes
    send_counter: int = 0

    #: Scratch buffer for nonce assembly, reused to avoid per-packet allocation.
    _nonce_buf: bytearray = None  # type: ignore[assignment]

    #: Serializes counter increment and nonce assembly.
    #:
    #: Encryption happens on more than one thread -- the datapath sends acks,
    #: the web/asyncio thread sends control messages, and the Bluetooth thread
    #: sends rumble feedback. ``counter = self.send_counter; self.send_counter
    #: = counter + 1`` is several bytecodes, so the GIL does NOT make it
    #: atomic: two threads can read the same counter, emit the same nonce, and
    #: nonce reuse under ChaCha20-Poly1305 leaks the XOR of both plaintexts and
    #: enables forgery. The shared _nonce_buf is equally unsafe to interleave.
    #:
    #: An uncontended lock costs tens of nanoseconds, which is nothing beside
    #: the 5-15 ms Bluetooth floor.
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if len(self.key) != KEY_SIZE:
            raise ValueError(f"key must be {KEY_SIZE} bytes")
        self._nonce_buf = bytearray(NONCE_SIZE)
        self._lock = threading.Lock()

    @classmethod
    def for_client(cls, session_key: bytes) -> SessionCrypto:
        return cls(key=session_key, send_prefix=_NONCE_PREFIX_C2S,
                   recv_prefix=_NONCE_PREFIX_S2C)

    @classmethod
    def for_server(cls, session_key: bytes) -> SessionCrypto:
        return cls(key=session_key, send_prefix=_NONCE_PREFIX_S2C,
                   recv_prefix=_NONCE_PREFIX_C2S)

    def _nonce(self, prefix: bytes, counter: int) -> bytes:
        self._nonce_buf[0:4] = prefix
        _COUNTER_STRUCT.pack_into(self._nonce_buf, 4, counter)
        return bytes(self._nonce_buf)

    def encrypt(self, plaintext: bytes | bytearray | memoryview) -> bytes:
        """Encrypt one datagram.

        Returns ``SESSION_TAG ‖ counter(8) ‖ ciphertext ‖ tag(16)``.

        The leading SESSION byte makes the outer framing unambiguous. Without
        it the datagram would begin with the counter, whose low byte is
        arbitrary -- a packet with counter 1 would be indistinguishable from a
        plaintext HELLO and would be dispatched to the handshake handler.

        The counter travels in the clear because the receiver needs it to
        rebuild the nonce; it is authenticated as associated data so it cannot
        be tampered with to force a nonce collision.
        """
        # Counter reservation and nonce assembly must be one atomic step -- see
        # the note on _lock. The AEAD call itself is outside the lock: it is
        # pure given (key, nonce, plaintext) and releases the GIL internally,
        # so holding the lock across it would serialize threads for no benefit.
        with self._lock:
            counter = self.send_counter
            if counter > _MAX_COUNTER:
                raise CryptoError("nonce counter exhausted; session must be renegotiated")
            self.send_counter = counter + 1
            counter_bytes = _COUNTER_STRUCT.pack(counter)
            nonce = self._nonce(self.send_prefix, counter)

        ciphertext = nacl.bindings.crypto_aead_chacha20poly1305_ietf_encrypt(
            bytes(plaintext), counter_bytes, nonce, self.key
        )
        return SESSION_TAG + counter_bytes + ciphertext

    def decrypt(self, data: bytes | bytearray | memoryview) -> tuple[int, bytes]:
        """Decrypt one datagram. Returns ``(counter, plaintext)``.

        Accepts the framing produced by :meth:`encrypt`, including the leading
        SESSION tag.

        Raises CryptoError on any failure -- wrong key, tampering, truncation.
        Callers must drop the packet and continue; anyone can send us a
        datagram, so this is an expected condition, not an error path.
        """
        if len(data) < 1 + 8 + TAG_SIZE:
            raise CryptoError("ciphertext too short")

        data = bytes(data)
        if data[0] != SESSION_TAG[0]:
            raise CryptoError("not a session datagram")

        counter_bytes = data[1:9]
        (counter,) = _COUNTER_STRUCT.unpack(counter_bytes)

        # Shares _nonce_buf with encrypt(), so it needs the same guard.
        with self._lock:
            nonce = self._nonce(self.recv_prefix, counter)

        try:
            plaintext = nacl.bindings.crypto_aead_chacha20poly1305_ietf_decrypt(
                data[9:], counter_bytes, nonce, self.key
            )
        except nacl.exceptions.CryptoError as exc:
            raise CryptoError("AEAD authentication failed") from exc

        return counter, plaintext


def new_random(size: int = RANDOM_SIZE) -> bytes:
    """Cryptographically secure random bytes."""
    return secrets.token_bytes(size)


def new_salt() -> bytes:
    return secrets.token_bytes(SALT_SIZE)


def new_client_id() -> bytes:
    """Stable-per-run identifier for a client. 16 bytes."""
    return secrets.token_bytes(16)
