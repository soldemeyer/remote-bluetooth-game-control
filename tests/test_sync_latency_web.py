"""The operator's half of sync latency: the switch, the ceiling, and the report.

Every part of a feature working with nothing on screen to reach it is a failure
this project has already recorded once, when split-screen detection shipped
complete and unreachable. The other half is `test_sync_latency.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.webjs import needs_node, run_node

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "server" / "web" / "static" / "index.html"
APP_JS = ROOT / "server" / "web" / "static" / "app.js"
CLIENTS_JS = ROOT / "server" / "web" / "static" / "js" / "sections" / "clients.js"
WEB_APP = ROOT / "server" / "web" / "app.py"


class TestTheSettingItself:
    def test_it_is_off_by_default(self):
        """It trades *everyone's* latency for fairness, and only the operator
        knows whether that is what the group wants."""
        from server.config import ServerConfig

        assert ServerConfig().sync_latency_enabled is False

    def test_there_is_a_ceiling_by_default(self):
        """Matching a player on a 300 ms link makes the game unplayable for
        everybody, and at that point the honest answer is that the connection is
        too bad to play against."""
        from server.config import ServerConfig
        from server.sync_latency import DEFAULT_CAP_MS

        assert ServerConfig().sync_latency_cap_ms == DEFAULT_CAP_MS

    def test_both_survive_a_round_trip_through_the_config_file(self, tmp_path):
        from server import config as config_module

        cfg = config_module.ServerConfig()
        cfg.sync_latency_enabled = True
        cfg.sync_latency_cap_ms = 90.0
        path = tmp_path / "server.json"
        config_module.save(cfg, path)

        loaded = config_module.load(path)
        assert loaded.sync_latency_enabled is True
        assert loaded.sync_latency_cap_ms == 90.0

    def test_it_is_persisted_not_runtime_only(self):
        """Unlike `auto_approve`, which is runtime-only because a server that
        silently resumed admitting strangers after a reboot is a security
        posture nobody chose. This is a preference about how a group plays, it
        is visible in the GUI and reported per client, and reverting it on
        restart would hand everybody an unfair game with nothing to say why."""
        source = WEB_APP.read_text(encoding="utf-8")
        branch = source.split('if "sync_latency_enabled" in body', 1)[1]
        branch = branch.split('if "auto_approve" in body', 1)[0]
        assert "state.config.sync_latency_enabled" in branch
        assert "_persist(state)" in branch

    def test_it_reaches_the_datapath_at_startup(self):
        source = (ROOT / "server" / "main.py").read_text(encoding="utf-8")
        assert "sync_latency_enabled=cfg.sync_latency_enabled" in source
        assert "sync_latency_cap_ms=cfg.sync_latency_cap_ms" in source


class TestTheWebGuiOffersIt:
    def test_the_controls_exist(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="sync-latency"' in html
        assert 'id="sync-latency-cap"' in html

        app = APP_JS.read_text(encoding="utf-8")
        assert "sync-latency" in app, "the toggle is not wired up"
        assert "sync_latency_enabled" in app, "nothing is posted"
        assert "sync_latency_cap_ms" in app, "the ceiling posts nothing"

    def test_they_sit_with_the_clients_they_govern(self):
        """It is a property of the connections, not of the adapters, and it
        belongs beside the per-player latency figures it changes."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        controllers = html.split(
            '<section class="view" data-view="controllers"', 1
        )[1].split("</section>", 1)[0]
        assert 'id="sync-latency"' in controllers

        # Positional: between the Clients heading and the cards it governs.
        heading = controllers.split(">Clients ", 1)
        assert len(heading) == 2, "the Clients heading moved"
        group = heading[1].split('id="clients"', 1)[0]
        assert 'id="sync-latency"' in group, (
            "the toggle is not in the Clients heading row"
        )
        assert 'id="sync-latency-cap"' in group

    def test_the_listeners_are_guarded(self):
        """A TypeError at module scope takes every listener registered after it,
        leaving a GUI whose buttons silently do nothing. These elements are
        newer than deployed pages."""
        app = APP_JS.read_text(encoding="utf-8")
        assert "$('sync-latency').addEventListener" not in app
        assert "$('sync-latency-cap').addEventListener" not in app

    def test_the_ceiling_is_seeded_on_change_not_when_empty(self):
        """"Only fill it when it is empty" refills a field the instant it is
        cleared, and at 10 Hz there is no window in which to finish typing."""
        app = APP_JS.read_text(encoding="utf-8")
        assert "seedOnChange($('sync-latency-cap')" in app

    def test_the_status_carries_the_state_so_the_switch_can_show_it(self):
        source = WEB_APP.read_text(encoding="utf-8")
        assert '"sync_latency_enabled"' in source
        assert '"sync_latency_cap_ms"' in source
        assert '"sync_latency"' in source

    def test_the_clients_table_shows_what_each_player_is_given(self):
        clients = CLIENTS_JS.read_text(encoding="utf-8")
        assert "syncCell" in clients
        assert 'data-field="sync"' in clients
        assert "<th>Sync</th>" in clients

    def test_a_client_still_being_measured_is_named_as_such(self):
        """Otherwise a player who is genuinely not being levelled looks
        identical to one who needs no levelling."""
        clients = CLIENTS_JS.read_text(encoding="utf-8")
        assert "measuring" in clients

    def test_the_note_says_who_is_setting_the_pace(self):
        """The operator's question is not "is it on" -- the switch says that --
        but "who is holding everybody up, and is the ceiling in the way"."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="sync-latency-note"' in html

        clients = CLIENTS_JS.read_text(encoding="utf-8")
        assert "sync-latency-note" in clients
        assert "report.pacer" in clients
        assert "report.capped" in clients

    def test_the_info_copy_says_what_it_costs(self):
        """A control whose description only says what it does invites somebody
        to switch it on expecting it to make things faster."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        card = html.split('id="sync-latency"', 1)[0]
        card = card.rsplit("toggle-cell", 1)[1]
        assert "lag" in card.lower()
        assert "not faster" in card.lower() or "fair, not faster" in card.lower()

    def test_it_says_that_video_is_not_levelled(self):
        """The feature levels the controller path. Per-client video latency is
        not equalised, and "level playing field" would otherwise overclaim."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        card = html.split('id="sync-latency"', 1)[0].rsplit("toggle-cell", 1)[1]
        assert "video" in card.lower()


class TestTheApiHandler:
    """Driven through the real handler rather than read out of the source, so a
    field that stops being applied fails rather than merely looking present."""

    @pytest.fixture
    def state(self, tmp_path):
        from types import SimpleNamespace

        from server.config import ServerConfig
        from server.sync_latency import SyncGovernor

        class FakeDatapath:
            def __init__(self):
                self.sync_latency_enabled = False
                self._sync = SyncGovernor()

            def set_sync_latency(self, enabled, *, cap_ms=None):
                if cap_ms is not None:
                    self._sync.cap_ms = cap_ms
                self.sync_latency_enabled = bool(enabled)

            @property
            def sync_latency_cap_ms(self):
                return self._sync.cap_ms

        path = tmp_path / "server.json"
        return SimpleNamespace(
            config=ServerConfig(),
            config_path=path,
            datapath=FakeDatapath(),
            broadcast=_noop_async,
        )

    async def post(self, state, body):
        from server.web import app as web_app

        class Request:
            def __init__(self, payload):
                self._payload = payload
                self.app = {"state": state}

            async def json(self):
                return self._payload

        return await web_app.handle_settings(Request(body))

    @pytest.mark.asyncio
    async def test_turning_it_on_reaches_the_datapath(self, state):
        await self.post(state, {"sync_latency_enabled": True})

        assert state.datapath.sync_latency_enabled is True
        assert state.config.sync_latency_enabled is True

    @pytest.mark.asyncio
    async def test_and_is_written_to_disk(self, state):
        await self.post(state, {"sync_latency_enabled": True})

        saved = json.loads(state.config_path.read_text(encoding="utf-8"))
        assert saved["sync_latency_enabled"] is True

    @pytest.mark.asyncio
    async def test_the_ceiling_is_clamped(self, state):
        """It arrives from a browser number box, and the ring the delay line
        preallocates is sized from it."""
        from server.sync_latency import MAX_CAP_MS

        await self.post(state, {"sync_latency_cap_ms": 100000})

        assert state.config.sync_latency_cap_ms == MAX_CAP_MS
        assert state.datapath.sync_latency_cap_ms == MAX_CAP_MS

    @pytest.mark.asyncio
    async def test_nonsense_keeps_the_previous_value(self, state):
        state.config.sync_latency_cap_ms = 45.0

        await self.post(state, {"sync_latency_cap_ms": "not a number"})

        assert state.config.sync_latency_cap_ms == 45.0

    @pytest.mark.asyncio
    async def test_the_ceiling_alone_does_not_turn_it_on(self, state):
        await self.post(state, {"sync_latency_cap_ms": 30})

        assert state.datapath.sync_latency_enabled is False


async def _noop_async(*_args, **_kwargs):
    return None


@needs_node
class TestTheSyncCellIsExecutedNotGrepped:
    """`syncCell` decides what a player is told about being held back, and the
    three cases it has to keep apart all read as "nothing" if it gets them
    wrong. Run rather than read: a grep-the-source test cannot tell an intention
    from a behaviour, which this repo has already paid for once."""

    def cell(self, client, slot):
        return run_node(f"""
            const {{ syncCell }} = await import(BASE + '/js/sections/clients.js');
            console.log(syncCell({client}, {slot}));
        """)

    def test_a_levelled_client_is_told_how_much(self):
        out = self.cell(
            '{sync: {added_ms: 26.2, state: "levelled"}}',
            '{slot: 0, added_delay_ms: 26.2}',
        )
        assert "+26.2 ms" in out
        assert "ceiling" not in out

    def test_the_ceiling_is_named_when_it_binds(self):
        """Otherwise "the delay stopped growing" is indistinguishable from "the
        connections got better"."""
        out = self.cell(
            '{sync: {added_ms: 10, state: "capped"}}',
            '{slot: 0, added_delay_ms: 10}',
        )
        assert "+10.0 ms" in out
        assert "ceiling" in out

    def test_a_client_still_being_measured_says_so(self):
        """Not "-": a player who is genuinely not being levelled yet must not
        look identical to one who needs no levelling."""
        out = self.cell(
            '{sync: {added_ms: 0, state: "measuring"}}',
            '{slot: 0, added_delay_ms: 0}',
        )
        assert "measuring" in out

    def test_nothing_added_reads_as_nothing(self):
        out = self.cell(
            '{sync: {added_ms: 0, state: "off"}}', '{slot: 0, added_delay_ms: 0}'
        )
        assert "ms" not in out

    def test_an_older_server_without_the_field_does_not_break_the_row(self):
        """`build_status` grew these keys; a browser pointed at a server that
        predates them must still render."""
        out = self.cell("{}", "{slot: 0}")
        assert "ms" not in out
