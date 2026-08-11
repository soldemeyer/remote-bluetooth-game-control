"""TLS for the web GUI.

Covers finding 1 in SECURITY.md: the admin password used to cross the network
in cleartext.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

tls = pytest.importorskip(
    "server.web.tls", reason="TLS module unavailable"
)

if not tls.is_available():
    pytest.skip("cryptography not installed", allow_module_level=True)

from server import config as server_config  # noqa: E402


@pytest.fixture
def certdir(tmp_path) -> Path:
    return tmp_path / "rbgc"


class TestCertificateGeneration:
    def test_generates_a_usable_pair(self, certdir):
        cert, key, fingerprint = tls.ensure_certificate(certdir)

        assert cert.exists() and key.exists()
        assert cert.read_text().startswith("-----BEGIN CERTIFICATE-----")
        assert "PRIVATE KEY" in key.read_text()
        assert fingerprint and fingerprint != "unavailable"

    def test_fingerprint_matches_openssl_format(self, certdir):
        """Must match what a browser shows, or the operator cannot use it to
        verify the certificate is ours."""
        cert, _, fingerprint = tls.ensure_certificate(certdir)

        parts = fingerprint.split(":")
        assert len(parts) == 32, "SHA-256 is 32 bytes"
        assert all(len(p) == 2 for p in parts)
        assert fingerprint == fingerprint.upper()

    def test_reuses_an_existing_certificate(self, certdir):
        """Regenerating every start would retrain the operator to ignore
        fingerprint changes -- exactly the warning that matters."""
        cert1, key1, fp1 = tls.ensure_certificate(certdir)
        original = cert1.read_bytes()

        cert2, key2, fp2 = tls.ensure_certificate(certdir)

        assert cert2.read_bytes() == original
        assert fp1 == fp2

    def test_private_key_is_not_world_readable(self, certdir):
        import os
        import sys

        _, key, _ = tls.ensure_certificate(certdir)

        if sys.platform == "win32":
            pytest.skip("POSIX permission bits do not apply on Windows")

        mode = os.stat(key).st_mode & 0o777
        assert mode == 0o600, f"key mode is {mode:o}, expected 600"

    def test_covers_localhost_and_loopback(self, certdir):
        """The recommended access path is an SSH tunnel to 127.0.0.1, so the
        certificate has to be valid for it."""
        from cryptography import x509

        cert, _, _ = tls.ensure_certificate(certdir)
        certificate = x509.load_pem_x509_certificate(cert.read_bytes())
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value

        dns = set(san.get_values_for_type(x509.DNSName))
        ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}

        assert "localhost" in dns
        assert "127.0.0.1" in ips

    def test_backdated_so_an_unsynced_clock_still_works(self, certdir):
        """A headless Pi has no RTC; before NTP syncs, its clock may be behind.
        A certificate valid only from 'now' would be rejected by its own server."""
        import datetime

        from cryptography import x509

        cert, _, _ = tls.ensure_certificate(certdir)
        certificate = x509.load_pem_x509_certificate(cert.read_bytes())

        now = datetime.datetime.now(datetime.timezone.utc)
        assert certificate.not_valid_before_utc < now

    def test_operator_supplied_paths_must_exist(self, certdir, tmp_path):
        with pytest.raises(tls.TLSError, match="not found"):
            tls.ensure_certificate(
                certdir, tmp_path / "nope.pem", tmp_path / "nope.key"
            )


class TestSSLContext:
    def test_context_refuses_obsolete_tls(self, certdir):
        """TLS 1.0/1.1 are deprecated and broken."""
        cert, key, _ = tls.ensure_certificate(certdir)
        context = tls.build_ssl_context(cert, key)

        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

    def test_compression_disabled(self, certdir):
        """CRIME attack."""
        cert, key, _ = tls.ensure_certificate(certdir)
        context = tls.build_ssl_context(cert, key)

        assert context.options & ssl.OP_NO_COMPRESSION

    def test_bad_certificate_raises_a_clear_error(self, tmp_path):
        bad_cert = tmp_path / "bad.pem"
        bad_key = tmp_path / "bad.key"
        bad_cert.write_text("not a certificate")
        bad_key.write_text("not a key")

        with pytest.raises(tls.TLSError, match="Could not load certificate"):
            tls.build_ssl_context(bad_cert, bad_key)


class TestConfiguration:
    def test_tls_is_on_by_default(self):
        """Encryption should not be something you have to know to ask for."""
        assert server_config.ServerConfig().tls_enabled is True

    def test_certificate_paths_default_to_generated(self):
        cfg = server_config.ServerConfig()
        assert cfg.tls_cert == ""
        assert cfg.tls_key == ""


class TestLiveHandshake:
    """Prove a real client can complete a TLS handshake against the context."""

    async def test_https_serves_and_negotiates_tls(self, certdir):
        import aiohttp
        from aiohttp import web

        cert, key, _ = tls.ensure_certificate(certdir)
        context = tls.build_ssl_context(cert, key)

        async def handler(request):
            return web.json_response({"secure": request.secure})

        app = web.Application()
        app.router.add_get("/", handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()

        port = runner.addresses[0][1]
        try:
            # Self-signed, so the client must not verify -- but the handshake
            # itself, and therefore the encryption, is real.
            client_ctx = ssl.create_default_context()
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://127.0.0.1:{port}/", ssl=client_ctx
                ) as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["secure"] is True
        finally:
            await runner.cleanup()

    async def test_plain_http_client_cannot_talk_to_it(self, certdir):
        """If a plaintext request succeeded, traffic would not be encrypted."""
        import aiohttp
        from aiohttp import web

        cert, key, _ = tls.ensure_certificate(certdir)
        context = tls.build_ssl_context(cert, key)

        app = web.Application()
        app.router.add_get("/", lambda r: web.json_response({}))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()

        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                with pytest.raises(Exception):
                    async with session.get(
                        f"http://127.0.0.1:{port}/",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ):
                        pass
        finally:
            await runner.cleanup()
