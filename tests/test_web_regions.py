"""The operator setting a controller's split-screen region, over HTTP.

Driven through the real handler and a real router rather than by reading the
source, because the interesting parts are all behavioural: whether it persists,
whether it reaches the live channel, and whether it works in mock mode -- which
is the one configuration anybody can run without Bluetooth hardware, and
therefore the one where a silent no-op would go unnoticed longest.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from common.screen_regions import LEFT, LOWER_RIGHT, QUAD_4, UPPER, UPPER_LEFT
from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.datapath import Datapath
from server.router import OutputChannel, Router
from server.sessions import SessionManager
from server.video import MODE_OFF, VideoRegistry
from server.web.app import create_app

ADMIN_PASSWORD = "admin-password"
ADDR = "00:00:00:00:00:01"


@pytest.fixture
async def client(tmp_path):
    cfg = server_config.ServerConfig(
        password="client-password",
        admin_password=ADMIN_PASSWORD,
        tls_enabled=False,
    )
    router = Router()
    router.add_channel(
        OutputChannel(
            bd_addr=ADDR,
            hci_name="mock0",
            profile=create_profile("generic"),
            sink=MockSink(name="mock0"),
        )
    )
    sessions = SessionManager(cfg.password, auto_approve=True)
    registry = VideoRegistry(mode=MODE_OFF)
    datapath = Datapath(
        sessions, router, bind_host="127.0.0.1", bind_port=0,
        realtime=False, video_registry=registry,
    )

    app = create_app(
        cfg, sessions, router, datapath,
        video_registry=registry, config_path=tmp_path / "server.json",
    )
    test_client = TestClient(TestServer(app))
    await test_client.start_server()

    response = await test_client.post("/api/login", json={"password": ADMIN_PASSWORD})
    assert response.status == 200

    yield test_client, cfg, router

    await test_client.close()


async def set_regions(test_client, regions):
    return await test_client.post(
        "/api/adapter/regions", json={"bd_addr": ADDR, "regions": regions}
    )


class TestSettingThem:
    async def test_it_reaches_the_config_and_the_live_channel(self, client):
        """Both, because either alone is a silent half-failure: the config
        alone means nothing happens until a restart, and the channel alone
        means it is lost at the next one."""
        test_client, cfg, router = client

        response = await set_regions(test_client, [UPPER_LEFT, LEFT])
        assert response.status == 200
        assert (await response.json())["ok"] is True

        assert cfg.adapter(ADDR).regions == [UPPER_LEFT, LEFT]
        assert router.channel(ADDR).regions == [UPPER_LEFT, LEFT]

    async def test_it_is_written_to_disk(self, client, tmp_path):
        test_client, _cfg, _router = client
        await set_regions(test_client, [UPPER_LEFT])

        reloaded = server_config.load(tmp_path / "server.json")
        assert reloaded.adapter(ADDR).regions == [UPPER_LEFT]

    async def test_clearing_them_works(self, client):
        """The whole screen has to be reachable from the dropdown, or an
        operator who assigns a region by mistake cannot undo it."""
        test_client, cfg, router = client
        await set_regions(test_client, [LOWER_RIGHT])
        await set_regions(test_client, [])

        assert cfg.adapter(ADDR).regions == []
        assert router.channel(ADDR).regions == []

    async def test_setting_them_twice_is_idempotent(self, client):
        """The GUI sends the whole set on every change, so a repeated or
        reordered request must not accumulate anything."""
        test_client, cfg, _router = client
        await set_regions(test_client, [UPPER_LEFT, LEFT])
        await set_regions(test_client, [LEFT, UPPER_LEFT])
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT, LEFT]

    async def test_unknown_names_are_dropped_rather_than_refused(self, client):
        test_client, cfg, _router = client
        response = await set_regions(test_client, [UPPER_LEFT, "nonsense", None])
        assert response.status == 200
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]


class TestBadRequests:
    async def test_no_adapter_is_an_error(self, client):
        test_client, _cfg, _router = client
        response = await test_client.post(
            "/api/adapter/regions", json={"regions": [UPPER_LEFT]}
        )
        assert response.status == 400

    async def test_regions_must_be_a_list(self, client):
        """A bare string would iterate to characters and quietly resolve to
        nothing, which looks like the setting not taking."""
        test_client, _cfg, _router = client
        response = await test_client.post(
            "/api/adapter/regions", json={"bd_addr": ADDR, "regions": UPPER_LEFT}
        )
        assert response.status == 400

    async def test_an_unknown_adapter_is_remembered_anyway(self, client):
        """An adapter that is unplugged right now still has an assignment
        worth keeping -- it is the same dongle when it comes back, and losing
        the setting because it was briefly absent is the failure the whole
        persist-by-BD_ADDR design exists to avoid."""
        test_client, cfg, _router = client
        response = await test_client.post(
            "/api/adapter/regions",
            json={"bd_addr": "00:00:00:00:00:09", "regions": [QUAD_4 and UPPER_LEFT]},
        )
        assert response.status == 200
        assert cfg.adapter("00:00:00:00:00:09").regions == [UPPER_LEFT]


class TestItSurvivesTheOperatorsOtherActions:
    async def test_reassigning_the_adapter_keeps_the_region(self, client):
        """The region belongs to the adapter, not to whoever is holding it."""
        test_client, cfg, router = client
        await set_regions(test_client, [UPPER_LEFT])

        router.assign(ADDR, "some-client", 0, "alice")
        router.unassign(ADDR)

        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]
        assert router.channel(ADDR).regions == [UPPER_LEFT]


class TestTheOperatorCanTurnDetectionOn:
    """The gap this class exists to close: every piece of split-screen worked
    and there was no control anywhere to switch it on, so the whole feature
    was unreachable from the GUI. Nothing else noticed, because each half was
    tested against the other rather than against the operator.
    """

    async def test_the_config_endpoint_accepts_the_split_settings(self, client):
        test_client, cfg, _router = client
        response = await test_client.post(
            "/api/video/config",
            json={"split_detect_enabled": True, "split_override": "QUAD_4"},
        )
        assert response.status == 200
        assert cfg.video_config["split_detect_enabled"] is True
        assert cfg.video_config["split_override"] == "QUAD_4"

    async def test_it_merges_rather_than_replacing(self, client):
        """The form posts one field list; anything not in it must survive."""
        test_client, cfg, _router = client
        await test_client.post("/api/video/config", json={"bitrate_kbps": 6000})
        await test_client.post(
            "/api/video/config", json={"split_detect_enabled": True}
        )
        assert cfg.video_config["bitrate_kbps"] == 6000
        assert cfg.video_config["split_detect_enabled"] is True

    async def test_a_bad_override_does_not_take(self, client):
        test_client, cfg, _router = client
        response = await test_client.post(
            "/api/video/config", json={"split_override": "QUAD_5"}
        )
        assert response.status == 200
        assert cfg.video_config["split_override"] == "auto"


class TestTheControlsAreOnThePage:
    """Asserted against the shipped page, because the page *is* the artefact.

    This is not the grep-the-source anti-pattern the project warns about: the
    question is whether a control exists in the HTML the operator is served
    and whether the form sends it, and there is nothing behind the markup to
    ask instead. It is the same idiom the other web tests use.
    """

    def read(self, name):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
        return (root / name).read_text(encoding="utf-8")

    def test_the_detection_toggle_exists_and_is_labelled(self):
        page = self.read("index.html")
        assert 'id="video-split-detect"' in page
        assert "Detect split screen automatically" in page

    def test_every_layout_can_be_forced(self):
        page = self.read("index.html")
        assert 'id="video-split-override"' in page
        for value in ("auto", "FULL", "VERTICAL_2", "HORIZONTAL_2", "QUAD_4"):
            assert f'value="{value}"' in page, value

    def test_the_form_actually_sends_them(self):
        """The control existing is not enough -- the form posts a fixed field
        list, so one left out of it is a switch that does nothing."""
        app_js = self.read("app.js")
        assert "split_detect_enabled: $('video-split-detect').checked" in app_js
        assert "split_override: $('video-split-override').value" in app_js

    def test_the_test_pattern_caveat_is_stated(self):
        """--test-source reads as a two-player split, and it is the documented
        no-hardware workflow, so the first thing somebody sees when they try
        this feature that way is a false positive."""
        assert "reads as a two-player split" in self.read("index.html")


class TestTheOperatorCanSeeWhatWasDetected:
    """Switching detection on and watching nothing visibly change is
    indistinguishable from it being broken. The readout is the only feedback
    there is -- the crop happens in every player's window, not here."""

    def page(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
        return root

    def test_the_status_card_has_a_layout_row(self):
        page = (self.page() / "index.html").read_text(encoding="utf-8")
        assert 'id="video-layout"' in page
        assert "Screen layout" in page

    def test_it_is_filled_in_from_the_status(self):
        script = (self.page() / "js" / "sections" / "video.js").read_text(encoding="utf-8")
        assert "setText($('video-layout'), describeLayout(video))" in script

    def test_the_three_states_are_told_apart(self):
        """Off, detected, and forced look identical from a player's seat and
        want completely different actions from the operator."""
        script = (self.page() / "js" / "sections" / "video.js").read_text(encoding="utf-8")
        assert "detection off" in script
        assert "forced" in script
        assert "confidence" in script


class TestDroppingAndRemovingOneRegion:
    """What the drag-and-drop palette actually sends.

    `add` and `remove` name a single region and are computed against what the
    server has stored -- not against a set the browser sent back. That is what
    stops a drop landing while a 10 Hz status update is in flight from
    carrying the browser's stale idea of the other assignments over somebody
    else's change.
    """

    async def add(self, test_client, region):
        return await test_client.post(
            "/api/adapter/regions", json={"bd_addr": ADDR, "add": region}
        )

    async def remove(self, test_client, region):
        return await test_client.post(
            "/api/adapter/regions", json={"bd_addr": ADDR, "remove": region}
        )

    async def test_dropping_one_assigns_it(self, client):
        test_client, cfg, router = client
        assert (await self.add(test_client, UPPER_LEFT)).status == 200
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]
        assert router.channel(ADDR).regions == [UPPER_LEFT]

    async def test_one_of_each_layout_can_be_held_at_once(self):
        """The stated requirement: a controller works whichever way the game
        splits, so it holds a quadrant, a half and a stripe together."""
        from server.config import ServerConfig

        cfg = ServerConfig(password="secret")
        for region in (UPPER_LEFT, LEFT, UPPER):
            cfg.add_adapter_region(ADDR, region)
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT, UPPER, LEFT]

    async def test_dropping_a_second_region_of_one_layout_replaces_it(self, client):
        """Each layout has one slot on the card, so a second region of the
        same layout would be stored, applied, and invisible -- and
        unremovable, since there is nothing on screen to click."""
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        await self.add(test_client, LOWER_RIGHT)
        assert cfg.adapter(ADDR).regions == [LOWER_RIGHT]

    async def test_replacing_one_layout_leaves_the_others_alone(self, client):
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        await self.add(test_client, LEFT)
        await self.add(test_client, LOWER_RIGHT)
        assert cfg.adapter(ADDR).regions == [LOWER_RIGHT, LEFT]

    async def test_the_x_removes_only_that_one(self, client):
        test_client, cfg, router = client
        await self.add(test_client, UPPER_LEFT)
        await self.add(test_client, LEFT)

        assert (await self.remove(test_client, UPPER_LEFT)).status == 200
        assert cfg.adapter(ADDR).regions == [LEFT]
        assert router.channel(ADDR).regions == [LEFT]

    async def test_removing_the_last_one_is_the_whole_screen(self, client):
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        await self.remove(test_client, UPPER_LEFT)
        assert cfg.adapter(ADDR).regions == []

    async def test_removing_something_not_held_is_a_no_op(self, client):
        """Two clicks on an X, or a stale card, must not disturb the rest."""
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        assert (await self.remove(test_client, LOWER_RIGHT)).status == 200
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]

    async def test_adding_the_same_one_twice_is_idempotent(self, client):
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        await self.add(test_client, UPPER_LEFT)
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]

    async def test_a_nonsense_region_changes_nothing(self, client):
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        assert (await self.add(test_client, "nonsense")).status == 200
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT]

    async def test_it_is_computed_against_the_server_not_the_request(self, client):
        """Two drops in flight at once: neither carries a set, so neither can
        undo the other."""
        test_client, cfg, _router = client
        await self.add(test_client, UPPER_LEFT)
        await self.add(test_client, LEFT)
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT, LEFT]

    async def test_the_full_set_form_still_works(self, client):
        """Kept for API callers and for the tests above it. The GUI no longer
        uses it, which is precisely why it needs a test of its own."""
        test_client, cfg, _router = client
        response = await test_client.post(
            "/api/adapter/regions", json={"bd_addr": ADDR, "regions": [UPPER_LEFT, LEFT]}
        )
        assert response.status == 200
        assert cfg.adapter(ADDR).regions == [UPPER_LEFT, LEFT]
