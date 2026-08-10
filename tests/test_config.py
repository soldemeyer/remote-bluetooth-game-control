"""Configuration loading, saving, and validation.

The behaviour that matters most here: a corrupt config must never stop either
program from starting. The server is headless and the client GUI is the only
way most users can fix settings, so a hard failure would leave them stuck.
"""

from __future__ import annotations

import json

import pytest

from client import config as client_config
from server import config as server_config


class TestClientConfig:
    def test_defaults_populate_controller_slots(self):
        cfg = client_config.ClientConfig()
        assert len(cfg.controllers) == client_config.MAX_CONTROLLERS
        assert cfg.client_name

    def test_round_trip(self, tmp_path):
        path = tmp_path / "client.json"
        cfg = client_config.ClientConfig(host="10.0.0.5", port=1234, mode="direct")
        cfg.controller(0).enabled = True
        cfg.controller(0).username = "alice"
        cfg.controller(0).guid = "abc123"

        client_config.save(cfg, path)
        loaded = client_config.load(path)

        assert loaded.host == "10.0.0.5"
        assert loaded.port == 1234
        assert loaded.controller(0).username == "alice"
        assert loaded.controller(0).guid == "abc123"

    def test_password_is_not_persisted_by_default(self, tmp_path):
        """A shared secret in a plaintext file is a bad default even for a
        LAN-party tool."""
        path = tmp_path / "client.json"
        cfg = client_config.ClientConfig(password="secret", save_password=False)

        client_config.save(cfg, path)

        assert "secret" not in path.read_text()
        assert client_config.load(path).password == ""

    def test_password_persisted_when_requested(self, tmp_path):
        path = tmp_path / "client.json"
        client_config.save(
            client_config.ClientConfig(password="secret", save_password=True), path
        )
        assert client_config.load(path).password == "secret"

    def test_missing_file_gives_defaults(self, tmp_path):
        assert client_config.load(tmp_path / "nope.json").port == 47800

    def test_corrupt_file_gives_defaults(self, tmp_path):
        path = tmp_path / "client.json"
        path.write_text("{ this is not json")
        assert client_config.load(path).port == 47800

    def test_unknown_fields_are_ignored(self, tmp_path):
        """Forward compatibility: a newer config must not break an older build."""
        path = tmp_path / "client.json"
        path.write_text(json.dumps({"host": "1.2.3.4", "future_option": True}))
        assert client_config.load(path).host == "1.2.3.4"

    def test_save_is_atomic(self, tmp_path):
        """Written via a temp file and replaced, so a crash cannot truncate it."""
        path = tmp_path / "client.json"
        client_config.save(client_config.ClientConfig(host="a"), path)
        client_config.save(client_config.ClientConfig(host="b"), path)

        assert client_config.load(path).host == "b"
        assert not (tmp_path / "client.tmp").exists()


class TestClientValidation:
    def valid(self) -> client_config.ClientConfig:
        cfg = client_config.ClientConfig(mode="direct", host="10.0.0.1", password="pw")
        cfg.controller(0).enabled = True
        return cfg

    def test_valid_config_has_no_problems(self):
        assert self.valid().validate() == []

    def test_password_is_required(self):
        cfg = self.valid()
        cfg.password = ""
        assert any("password" in p.lower() for p in cfg.validate())

    def test_direct_mode_needs_a_host(self):
        cfg = self.valid()
        cfg.host = ""
        assert any("address" in p.lower() for p in cfg.validate())

    def test_punch_mode_needs_room_and_broker(self):
        cfg = self.valid()
        cfg.mode = "punch"
        problems = " ".join(cfg.validate()).lower()
        assert "room code" in problems
        assert "broker" in problems

    def test_at_least_one_controller_must_be_enabled(self):
        cfg = self.valid()
        cfg.controller(0).enabled = False
        assert any("controller" in p.lower() for p in cfg.validate())

    @pytest.mark.parametrize("poll_hz", [0, -1, 5000])
    def test_poll_rate_bounds(self, poll_hz):
        cfg = self.valid()
        cfg.poll_hz = poll_hz
        assert any("poll rate" in p.lower() for p in cfg.validate())

    def test_port_bounds(self):
        cfg = self.valid()
        cfg.port = 70000
        assert any("port" in p.lower() for p in cfg.validate())


class TestServerConfig:
    def test_round_trip_with_adapters(self, tmp_path):
        path = tmp_path / "server.json"
        cfg = server_config.ServerConfig(port=9999)
        cfg.upsert_adapter(
            server_config.AdapterConfig(
                bd_addr="AA:BB:CC:DD:EE:FF", enabled=True,
                profile="switch_pro", label="Living room",
            )
        )

        server_config.save(cfg, path)
        loaded = server_config.load(path)

        assert loaded.port == 9999
        adapter = loaded.adapter("AA:BB:CC:DD:EE:FF")
        assert adapter is not None
        assert adapter.profile == "switch_pro"
        assert adapter.label == "Living room"

    def test_adapter_lookup_is_case_insensitive(self):
        """BD_ADDRs arrive from different tools with different casing."""
        cfg = server_config.ServerConfig()
        cfg.upsert_adapter(server_config.AdapterConfig(bd_addr="AA:BB:CC:DD:EE:FF"))
        assert cfg.adapter("aa:bb:cc:dd:ee:ff") is not None

    def test_upsert_updates_rather_than_duplicating(self):
        cfg = server_config.ServerConfig()
        cfg.upsert_adapter(server_config.AdapterConfig(bd_addr="AA:BB:CC:DD:EE:FF"))
        cfg.upsert_adapter(
            server_config.AdapterConfig(bd_addr="AA:BB:CC:DD:EE:FF", enabled=False)
        )

        assert len(cfg.adapters) == 1
        assert cfg.adapter("AA:BB:CC:DD:EE:FF").enabled is False

    def test_password_is_never_persisted(self, tmp_path):
        """The server is network-reachable and often runs as root."""
        path = tmp_path / "server.json"
        server_config.save(server_config.ServerConfig(password="topsecret"), path)

        assert "topsecret" not in path.read_text()
        assert server_config.load(path).password == ""

    def test_adapters_without_an_address_are_skipped(self, tmp_path):
        path = tmp_path / "server.json"
        path.write_text(json.dumps({"adapters": [{"enabled": True}, {"bd_addr": "AA:BB:CC:DD:EE:FF"}]}))
        assert len(server_config.load(path).adapters) == 1

    def test_corrupt_file_gives_defaults(self, tmp_path):
        path = tmp_path / "server.json"
        path.write_text("not json")
        assert server_config.load(path).port == server_config.DEFAULT_PORT


class TestServerValidation:
    def valid(self) -> server_config.ServerConfig:
        return server_config.ServerConfig(password="a-good-password")

    def test_valid_config_has_no_problems(self):
        assert self.valid().validate() == []

    def test_password_required_and_length_checked(self):
        cfg = self.valid()
        cfg.password = ""
        assert any("password is required" in p.lower() for p in cfg.validate())

        cfg.password = "abc"
        assert any("6 characters" in p for p in cfg.validate())

    def test_ports_must_differ(self):
        cfg = self.valid()
        cfg.web_port = cfg.port
        assert any("must differ" in p for p in cfg.validate())

    def test_enabled_adapters_capped_at_the_ceiling(self):
        cfg = self.valid()
        for index in range(server_config.MAX_ADAPTERS + 1):
            cfg.upsert_adapter(
                server_config.AdapterConfig(bd_addr=f"AA:BB:CC:DD:EE:{index:02X}")
            )
        assert any("At most" in p for p in cfg.validate())

    def test_duplicate_adapters_are_flagged(self):
        cfg = self.valid()
        # Bypass upsert to construct the invalid state a hand-edited file could.
        cfg.adapters.append(server_config.AdapterConfig(bd_addr="AA:BB:CC:DD:EE:FF"))
        cfg.adapters.append(server_config.AdapterConfig(bd_addr="aa:bb:cc:dd:ee:ff"))
        assert any("Duplicate" in p for p in cfg.validate())
