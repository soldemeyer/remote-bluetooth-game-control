"""Which addresses a discovery probe is sent to.

**Reported from a Linux client: no search ever found anything**, and connecting
worked only by typing the LAN address by hand.

The cause is one line of hostname resolution. `_broadcast_addresses` derived
each directed broadcast from `getaddrinfo(gethostname())`, which on Windows
returns the machine's LAN address and on Debian and Ubuntu returns
**127.0.1.1** -- the loopback alias those distributions put in `/etc/hosts`.
That is skipped as loopback, so the only target left was the global
255.255.255.255, which the function's own comment already recorded as the one
routers drop. Measured on a Linux client: targets came out as
`['255.255.255.255']` and the real interface at 172.26.132.139/20 was never
probed.

Nothing reported it, either: a failed `sendto` hit a bare `continue` and an
empty result is indistinguishable from "nobody answered".
"""

from __future__ import annotations

import socket
import sys

import pytest

from server import discovery


class TestTheHostnameIsNotTheOnlySource:
    """The bug, in the shape it actually had."""

    def test_a_loopback_hostname_still_yields_a_real_target(self, monkeypatch):
        """Exactly the Debian/Ubuntu case: the hostname maps to 127.0.1.1 and
        nothing else. Before the fix this produced the global address alone."""
        monkeypatch.setattr(socket, "gethostname", lambda: "a-linux-box")
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(None, None, None, "", ("127.0.1.1", 0))],
        )
        monkeypatch.setattr(discovery, "_kernel_broadcast_addresses", set)
        monkeypatch.setattr(discovery, "_primary_address", lambda: "192.168.1.42")

        targets = discovery._broadcast_addresses()

        assert "192.168.1.255" in targets

    def test_the_route_out_is_asked(self, monkeypatch):
        """`connect` on a UDP socket sends nothing -- it only fixes the local
        end -- so this asks the routing table and costs no traffic."""
        monkeypatch.setattr(discovery, "_kernel_broadcast_addresses", set)
        monkeypatch.setattr(socket, "gethostname", lambda: "x")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])

        address = discovery._primary_address()

        assert address is None or not address.startswith("127.")

    def test_the_kernels_answer_is_used_verbatim(self, monkeypatch):
        """It is exact where a /24 guess is not: the machine this was measured
        on is a /20, so the derived 172.26.132.255 is the wrong subnet and the
        kernel's 172.26.143.255 is right."""
        monkeypatch.setattr(
            discovery, "_kernel_broadcast_addresses", lambda: {"172.26.143.255"}
        )
        monkeypatch.setattr(discovery, "_primary_address", lambda: "172.26.132.139")
        monkeypatch.setattr(socket, "gethostname", lambda: "x")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])

        targets = discovery._broadcast_addresses()

        assert "172.26.143.255" in targets

    def test_the_global_address_is_still_probed(self, monkeypatch):
        """It reaches some networks the directed ones do not, and an
        unanswered datagram costs nothing."""
        monkeypatch.setattr(discovery, "_kernel_broadcast_addresses", set)
        monkeypatch.setattr(discovery, "_primary_address", lambda: None)
        monkeypatch.setattr(socket, "gethostname", lambda: "x")
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])

        assert discovery._broadcast_addresses() == ["255.255.255.255"]

    def test_loopback_is_still_skipped(self, monkeypatch):
        """Probing 127.0.0.255 finds nothing and confuses the log."""
        monkeypatch.setattr(discovery, "_kernel_broadcast_addresses", set)
        monkeypatch.setattr(discovery, "_primary_address", lambda: "127.0.1.1")
        monkeypatch.setattr(socket, "gethostname", lambda: "x")
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda *a, **k: [(None, None, None, "", ("127.0.0.1", 0))],
        )

        assert discovery._broadcast_addresses() == ["255.255.255.255"]

    def test_a_hostname_that_will_not_resolve_is_survivable(self, monkeypatch):
        """It raises on a machine with no DNS and no hosts entry, and that must
        not be the end of discovery."""
        def boom(*_args, **_kwargs):
            raise socket.gaierror("no")

        monkeypatch.setattr(discovery, "_kernel_broadcast_addresses", set)
        monkeypatch.setattr(discovery, "_primary_address", lambda: "10.0.0.5")
        monkeypatch.setattr(socket, "gethostname", lambda: "x")
        monkeypatch.setattr(socket, "getaddrinfo", boom)

        assert "10.0.0.255" in discovery._broadcast_addresses()


class TestOnThisMachine:
    """Run against the real stack rather than a monkeypatched one, because the
    fault was in what the real stack returns."""

    def test_there_is_more_than_the_global_address(self):
        targets = discovery._broadcast_addresses()

        assert targets != ["255.255.255.255"], (
            "no directed broadcast was found; a probe would reach only the "
            "address routers drop"
        )

    def test_every_target_is_a_broadcast_address(self):
        for address in discovery._broadcast_addresses():
            assert address.endswith(".255"), address

    @pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="the ioctl is Linux's"
    )
    def test_the_kernel_answers_on_linux(self):
        """The source that made this work at all on the machine it was
        reported from."""
        assert discovery._kernel_broadcast_addresses()

    @pytest.mark.skipif(
        sys.platform.startswith("linux"), reason="the ioctl is Linux's"
    )
    def test_it_asks_no_kernel_elsewhere(self):
        """Empty rather than raising: the other two sources carry it."""
        assert discovery._kernel_broadcast_addresses() == set()


class TestAFailedProbeIsNotSilent:
    def test_the_targets_are_logged(self):
        """"Every send failed" and "nobody answered" are different faults that
        both produced an empty list and one `continue`."""
        import inspect

        source = inspect.getsource(discovery.discover_servers)

        assert "Probe to %s failed" in source
        assert "could not send to any" in source
