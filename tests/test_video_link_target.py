"""Where the video link dials, and the address it must stop destroying.

`video_host` is the operator's answer to one question: *where is the video
server?* Embedded mode used to write `127.0.0.1` into it, because that is where
the link dials when the source is our own subprocess -- and switching back to
external left that behind.

The result is the worst shape of bug this project keeps meeting: the server
dialled **itself**, forever, for an address nobody had typed, and the only
symptom was

    Server did not respond. Check the address, the port, and that the server
    is running.

which sends you to check the video server, the port and the firewall. All three
were fine. Measured on the reference Pi: detection found the real source at
192.168.1.116:47810 while the link reported 346 failed attempts against
127.0.0.1, and the web GUI showed an address the operator had never entered.

So the mode resolves the target, and `video_host` keeps meaning one thing.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from server.config import ServerConfig
from server.videolink import VideoLink
from server.web import app as web_app


def _link(**overrides):
    config = ServerConfig(**overrides)
    return VideoLink(registry=None, datapath=None, config=config), config


class TestTheTargetFollowsTheMode:
    def test_external_dials_what_the_operator_typed(self):
        link, _cfg = _link(
            video_mode="external", video_host="192.168.1.116", video_port=47810)
        assert link.target() == ("192.168.1.116", 47810)

    def test_embedded_dials_loopback(self):
        """The source is our own subprocess on this machine; there is nothing
        for the operator to point at and no password to agree."""
        link, _cfg = _link(
            video_mode="embedded", video_host="192.168.1.116", video_port=47810)
        assert link.target() == ("127.0.0.1", 47810)

    def test_embedded_does_not_disturb_the_stored_address(self):
        """**The regression.** Reading the target must not write anything."""
        link, config = _link(
            video_mode="embedded", video_host="192.168.1.116", video_port=47810)

        link.target()

        assert config.video_host == "192.168.1.116", (
            "the external server's address was destroyed by looking at it"
        )

    def test_switching_back_to_external_returns_to_the_saved_address(self):
        """The sequence that produced 346 failed attempts on the reference Pi:
        embedded, then external, and the address never came back."""
        link, config = _link(
            video_mode="external", video_host="192.168.1.116", video_port=47810)

        config.video_mode = "embedded"
        assert link.target() == ("127.0.0.1", 47810)

        config.video_mode = "external"
        assert link.target() == ("192.168.1.116", 47810)

    def test_it_is_resolved_per_attempt_not_captured_at_construction(self):
        """The mode can change under a running link -- the GUI switches it
        without restarting anything."""
        link, config = _link(video_mode="external", video_host="10.0.0.9")
        first = link.target()
        config.video_mode = "embedded"
        assert link.target() != first


class TestTheModeSwitchLeavesTheAddressAlone:
    def test_starting_embedded_does_not_write_video_host(self):
        """Pinned against the code because the alternative is standing up a
        subprocess supervisor -- `_apply_video_mode` starts a real child. What
        went wrong was a single assignment, and its absence is the property.

        Parsed rather than grepped: the comment where that line used to be
        names it, so a text search matches the explanation of the bug instead
        of the bug.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(web_app._apply_video_mode)))
        written = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "video_host" not in written, (
            "the mode switch is assigning to video_host again; embedded mode "
            "overwrote the external server's address and switching back left it"
        )

    def test_the_generated_password_is_still_only_a_fallback(self):
        """Neighbouring line, same shape of hazard: the invented credential
        must only fill an empty one, never replace what is already there."""
        source = inspect.getsource(web_app._apply_video_mode)
        assert "if not state.config.video_embedded_password:" in source


class TestTheReportedAddressIsTheDialledOne:
    def test_the_snapshot_reports_where_it_is_actually_dialling(self):
        """In embedded mode the stored and dialled addresses differ, and
        reporting the stored one would describe a connection to a machine we
        are not talking to."""
        link, _cfg = _link(
            video_mode="embedded", video_host="192.168.1.116", video_port=47810)
        assert link.snapshot()["host"] == "127.0.0.1"

    def test_external_reports_the_real_one(self):
        link, _cfg = _link(
            video_mode="external", video_host="192.168.1.116", video_port=47810)
        snap = link.snapshot()
        assert (snap["host"], snap["port"]) == ("192.168.1.116", 47810)


class TestTheCredentialFollowsTheModeToo:
    """The same clobber, one field along, and it outlived the first fix.

    Embedded mode invented a password and put it in `video_password` -- the
    **external** server's. So trying embedded once and going back left the link
    presenting a random string to a server whose password had never changed,
    and the GUI said "Incorrect password" with nothing to say why. Measured on
    the reference Pi immediately after the address fix landed.
    """

    def test_external_presents_the_operators_password(self):
        link, _cfg = _link(video_mode="external", video_password="from-the-operator")
        assert link.credential() == "from-the-operator"

    def test_embedded_presents_the_generated_one(self):
        link, _cfg = _link(
            video_mode="embedded",
            video_password="from-the-operator",
            video_embedded_password="invented-for-the-child",
        )
        assert link.credential() == "invented-for-the-child"

    def test_the_operators_password_survives_a_trip_through_embedded(self):
        link, config = _link(video_mode="external", video_password="from-the-operator")

        config.video_mode = "embedded"
        config.video_embedded_password = "invented-for-the-child"
        assert link.credential() == "invented-for-the-child"

        config.video_mode = "external"
        assert link.credential() == "from-the-operator", (
            "the external server's password was replaced by the child's"
        )

    def test_embedded_falls_back_rather_than_presenting_nothing(self):
        """An older config, or one where the generator has not run yet. An
        empty credential makes the link report "no address or password
        configured", which is a different and misleading complaint."""
        link, _cfg = _link(video_mode="embedded", video_password="whatever-was-there")
        assert link.credential() == "whatever-was-there"

    def test_the_mode_switch_writes_only_the_embedded_field(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(web_app._apply_video_mode)))
        written = {
            target.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute)
        }
        assert "video_password" not in written, (
            "embedded mode is overwriting the operator's video password again"
        )
        assert "video_embedded_password" in written, (
            "nothing generates the child's credential"
        )


class TestNeitherSecretReachesDisk:
    def test_the_embedded_password_is_blanked_on_save(self, tmp_path):
        from server import config as server_config

        cfg = server_config.ServerConfig(
            video_password="operator", video_embedded_password="invented")
        target = tmp_path / "server.json"
        server_config.save(cfg, target)

        written = target.read_text(encoding="utf-8")
        assert "operator" not in written
        assert "invented" not in written
