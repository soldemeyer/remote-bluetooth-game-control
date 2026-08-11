"""TLS for the web GUI: certificate management and SSL context.

The admin interface authenticates with a password and controls Bluetooth
pairing, so serving it in the clear was the highest-severity finding in
SECURITY.md. This module makes HTTPS the default rather than an expert option.

Self-signed by design. A headless appliance on a home LAN has no route to a
public CA -- no domain name, no inbound port 80 for ACME -- so waiting for
"proper" certificates would mean shipping no encryption at all. A self-signed
certificate stops passive interception, which is the actual threat here
(someone on the same WiFi reading the password off the wire). It does not stop
an active attacker who can substitute their own certificate, which is why the
fingerprint is printed at startup for the operator to check once.

The generated certificate covers localhost, 127.0.0.1, the hostname, and every
detected LAN address, so it keeps working whether the operator connects through
an SSH tunnel or directly by IP.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import os
import socket
import ssl
from pathlib import Path

log = logging.getLogger(__name__)

CERT_FILENAME = "web-cert.pem"
KEY_FILENAME = "web-key.pem"

#: Long-lived on purpose. This is an operator-managed appliance certificate,
#: not a public one -- forcing a renewal dance on a headless box would just
#: produce an expired certificate nobody noticed.
VALIDITY_DAYS = 3650

#: Regenerate this far before expiry so it never lapses silently.
RENEW_BEFORE_DAYS = 30


class TLSError(RuntimeError):
    """TLS was requested but cannot be provided."""


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Load a certificate into a hardened server SSL context.

    TLS 1.2 is the floor: 1.0/1.1 are deprecated and every browser that can
    reach this GUI supports 1.2 or better.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2

    # Forward secrecy only, no RC4/3DES/NULL. TLS 1.3 suites are fixed by the
    # protocol and unaffected by this string.
    try:
        context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!DSS:!3DES")
    except ssl.SSLError as exc:
        log.debug("Could not restrict cipher list: %s", exc)

    context.options |= ssl.OP_NO_COMPRESSION          # CRIME
    context.options |= ssl.OP_CIPHER_SERVER_PREFERENCE

    try:
        context.load_cert_chain(str(cert_path), str(key_path))
    except (OSError, ssl.SSLError) as exc:
        raise TLSError(f"Could not load certificate {cert_path}: {exc}") from exc

    return context


def ensure_certificate(
    config_dir: Path,
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> tuple[Path, Path, str]:
    """Return ``(cert, key, fingerprint)``, generating a certificate if needed.

    An operator-supplied pair is used as-is and never regenerated -- if someone
    installed a real certificate, silently replacing it would be hostile.
    Otherwise a self-signed one is created in the config directory and reused
    until it approaches expiry.
    """
    operator_supplied = cert_path is not None and key_path is not None

    cert = Path(cert_path) if cert_path else config_dir / CERT_FILENAME
    key = Path(key_path) if key_path else config_dir / KEY_FILENAME

    if operator_supplied:
        if not cert.exists() or not key.exists():
            raise TLSError(
                f"Configured certificate or key not found: {cert}, {key}"
            )
        return cert, key, fingerprint(cert)

    if cert.exists() and key.exists() and not _needs_renewal(cert):
        return cert, key, fingerprint(cert)

    _generate_self_signed(cert, key)
    return cert, key, fingerprint(cert)


def _needs_renewal(cert_path: Path) -> bool:
    """True if the certificate is missing, unreadable, or near expiry."""
    try:
        from cryptography import x509
    except ImportError:
        return False  # cannot inspect it; assume the operator knows

    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:
        log.warning("Could not read %s (%s); regenerating", cert_path, exc)
        return True

    expires = certificate.not_valid_after_utc
    remaining = expires - datetime.datetime.now(datetime.timezone.utc)

    if remaining.days < RENEW_BEFORE_DAYS:
        log.info("Web certificate expires in %d days; regenerating", remaining.days)
        return True
    return False


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Create a self-signed certificate covering every way we might be reached."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise TLSError(
            "TLS needs the 'cryptography' package.\n"
            "  Install it with:  pip install -e \".[server]\"\n"
            "  Or start without encryption:  --no-tls  (not recommended over a network)"
        ) from exc

    hostname = _hostname()

    # P-256 rather than RSA: far faster to generate on a Pi, smaller
    # handshakes, and universally supported by browsers.
    private_key = ec.generate_private_key(ec.SECP256R1())

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Remote Bluetooth Game Control"),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        # Backdated a day so a Pi whose clock has not yet synced via NTP -- very
        # common on a headless box without an RTC -- does not reject its own
        # certificate as not-yet-valid.
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(_subject_alt_names(hostname)), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    )

    certificate = builder.sign(private_key, hashes.SHA256())

    cert_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the key first, with restrictive permissions established *before*
    # any bytes land in it -- creating world-readable then chmod-ing leaves a
    # window where the key is exposed.
    key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    _write_private(key_path, key_bytes)

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))

    log.info(
        "Generated a self-signed web certificate for %s, valid %d days",
        hostname,
        VALIDITY_DAYS,
    )


def _write_private(path: Path, data: bytes) -> None:
    """Write a file that only the owner can read, with no exposure window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)

    # Windows ignores the mode argument; best effort there.
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _subject_alt_names(hostname: str) -> list:
    """Every name and address this server might legitimately be reached by.

    Includes each detected LAN address so connecting by IP does not produce an
    additional certificate warning on top of the self-signed one.
    """
    from cryptography import x509

    names: list = [x509.DNSName(hostname), x509.DNSName("localhost")]
    addresses = {"127.0.0.1", "::1"}

    try:
        for info in socket.getaddrinfo(hostname, None):
            addresses.add(info[4][0])
    except (OSError, socket.gaierror):
        pass

    # Also whatever address is used to reach the wider network.
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))   # TEST-NET-1; no packet is sent
            addresses.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass

    for address in sorted(addresses):
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(address.split("%")[0])))
        except ValueError:
            continue

    return names


def _hostname() -> str:
    try:
        return socket.gethostname() or "rbgc-server"
    except OSError:
        return "rbgc-server"


def fingerprint(cert_path: Path) -> str:
    """SHA-256 fingerprint, formatted like a browser shows it.

    Printed at startup so the operator can confirm, once, that the certificate
    their browser warns about is genuinely ours -- which is what turns a
    self-signed certificate from decoration into a real defence against active
    interception.
    """
    import hashlib

    # Fingerprints are taken over the DER encoding -- that is what browsers and
    # openssl display, so a PEM-based digest would not match what the operator
    # sees and would be worse than useless.
    try:
        der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
    except (OSError, ValueError) as exc:
        log.debug("Could not compute certificate fingerprint: %s", exc)
        return "unavailable"

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def is_available() -> bool:
    """True if certificate generation is possible."""
    try:
        import cryptography  # noqa: F401

        return True
    except ImportError:
        return False
