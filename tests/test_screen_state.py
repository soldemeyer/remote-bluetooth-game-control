"""Region assignments: persisted on the adapter, resolved for the client.

Phases 5 and 6. The vocabulary and the merge rules are tested in
``test_screen_regions.py``; this is about the two joins around them -- config
to adapter, and adapter to client.

The persistence tests are the ones worth reading twice. ``upsert_adapter``
copies a fixed list of fields and five places in ``server/bt/adapter.py`` build
a partial ``AdapterConfig`` and hand it over -- pairing a console, forgetting
one, enabling an adapter, changing the profile, allocating a number. A field
none of them mentions is wiped by every one of them, silently, and the operator
finds out when a player is watching the wrong half of the screen.
"""

from __future__ import annotations

import json

import pytest

from common.screen_regions import (
    FULL,
    HORIZONTAL_2,
    LEFT,
    LOWER,
    LOWER_LEFT,
    LOWER_RIGHT,
    QUAD_4,
    RIGHT,
    UPPER,
    UPPER_LEFT,
    UPPER_RIGHT,
    VERTICAL_2,
)
from server import config as server_config
from server.bt.profiles import create_profile
from server.bt.sink import MockSink
from server.config import AdapterConfig, ServerConfig
from server.router import OutputChannel, Router
from server.screen_state import (
    crops_for_client,
    everyone_full_screen,
    regions_for_client,
    regions_message,
)


def router_with(*channels) -> Router:
    router = Router()
    for bd_addr, regions, client in channels:
        channel = OutputChannel(
            bd_addr=bd_addr,
            hci_name="hci0",
            profile=create_profile("generic"),
            sink=MockSink(),
            regions=list(regions),
        )
        if client is not None:
            channel.assigned_client = client
            channel.assigned_slot = 0
        router.add_channel(channel)
    return router


class TestRegionsArePersistedOnTheAdapter:
    def test_they_survive_a_save_and_load(self, tmp_path):
        path = tmp_path / "server.json"
        config = ServerConfig(password="secret")
        config.upsert_adapter(AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", number=1))
        config.set_adapter_regions("AA:BB:CC:DD:EE:01", [UPPER_LEFT, LEFT])
        server_config.save(config, path)

        loaded = server_config.load(path)
        assert loaded.adapter("AA:BB:CC:DD:EE:01").regions == [UPPER_LEFT, LEFT]

    def test_an_old_config_without_the_field_loads(self, tmp_path):
        """Every existing install is one of these."""
        path = tmp_path / "server.json"
        path.write_text(
            json.dumps(
                {
                    "password": "secret",
                    "adapters": [{"bd_addr": "AA:BB:CC:DD:EE:01", "number": 1}],
                }
            ),
            encoding="utf-8",
        )
        loaded = server_config.load(path)
        assert loaded.adapter("AA:BB:CC:DD:EE:01").regions == []

    def test_rubbish_in_the_file_is_dropped_not_fatal(self, tmp_path):
        path = tmp_path / "server.json"
        path.write_text(
            json.dumps(
                {
                    "password": "secret",
                    "adapters": [
                        {
                            "bd_addr": "AA:BB:CC:DD:EE:01",
                            "regions": ["upper_left", "nonsense", None, 7],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert server_config.load(path).adapter("AA:BB:CC:DD:EE:01").regions == [
            UPPER_LEFT
        ]

    def test_they_can_be_cleared(self):
        config = ServerConfig(password="secret")
        config.set_adapter_regions("AA:BB:CC:DD:EE:01", [UPPER_LEFT])
        config.set_adapter_regions("AA:BB:CC:DD:EE:01", [])
        assert config.adapter("AA:BB:CC:DD:EE:01").regions == []

    def test_setting_them_creates_the_adapter_if_it_is_new(self):
        config = ServerConfig(password="secret")
        config.set_adapter_regions("aa:bb:cc:dd:ee:01", [RIGHT])
        entry = config.adapter("AA:BB:CC:DD:EE:01")
        assert entry is not None and entry.regions == [RIGHT]


class TestNothingRoutineWipesThem:
    """The five partial-``AdapterConfig`` sites, driven through ``upsert``.

    Each of these is what some ordinary operator action does. None of them is
    thinking about screen regions, and that is the whole danger.
    """

    @pytest.fixture()
    def config(self):
        config = ServerConfig(password="secret")
        config.upsert_adapter(AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", number=1))
        config.set_adapter_regions("AA:BB:CC:DD:EE:01", [UPPER_LEFT, LEFT])
        return config

    def regions(self, config):
        return config.adapter("AA:BB:CC:DD:EE:01").regions

    def test_pairing_a_console(self, config):
        config.upsert_adapter(
            AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", paired_target="11:22:33:44:55:66")
        )
        assert self.regions(config) == [UPPER_LEFT, LEFT]

    def test_forgetting_a_console(self, config):
        config.upsert_adapter(
            AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", paired_target="")
        )
        assert self.regions(config) == [UPPER_LEFT, LEFT]

    def test_disabling_and_re_enabling(self, config):
        config.upsert_adapter(AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", enabled=False))
        config.upsert_adapter(AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", enabled=True))
        assert self.regions(config) == [UPPER_LEFT, LEFT]

    def test_changing_the_profile(self, config):
        config.upsert_adapter(
            AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", profile="switch_pro")
        )
        assert self.regions(config) == [UPPER_LEFT, LEFT]

    def test_allocating_a_number(self, config):
        config.upsert_adapter(AdapterConfig(bd_addr="AA:BB:CC:DD:EE:01", number=3))
        assert self.regions(config) == [UPPER_LEFT, LEFT]

    # There is deliberately no test asserting the five call sites each carry
    # regions forward. Driving them for real needs D-Bus, and asserting it by
    # reading the source is the grep-the-source anti-pattern this project has
    # already had to unpick once -- it cannot tell an intention from a comment
    # about one. The guard above is what actually protects the field, and it
    # is what these tests exercise; carrying them at the call sites is the
    # second line, valuable precisely because nothing is watching it.


class TestWhatOneClientOwns:
    def test_a_client_with_one_controller(self):
        router = router_with(("A", [UPPER_LEFT], "alice"))
        assert regions_for_client(router, "alice", QUAD_4) == [UPPER_LEFT]

    def test_a_client_with_several_controllers_gets_all_of_them(self):
        """The case the specification calls out. Dropping the second
        controller's region shows a player half of what they own, and looks
        like a rendering bug rather than a routing one."""
        router = router_with(
            ("A", [UPPER_LEFT], "alice"),
            ("B", [LOWER_LEFT], "alice"),
        )
        assert regions_for_client(router, "alice", QUAD_4) == [LOWER_LEFT, UPPER_LEFT]

    def test_other_clients_regions_are_not_included(self):
        router = router_with(
            ("A", [UPPER_LEFT], "alice"),
            ("B", [LOWER_RIGHT], "bob"),
        )
        assert regions_for_client(router, "alice", QUAD_4) == [UPPER_LEFT]
        assert regions_for_client(router, "bob", QUAD_4) == [LOWER_RIGHT]

    def test_an_unassigned_channels_regions_belong_to_nobody(self):
        router = router_with(("A", [UPPER_LEFT], None))
        assert regions_for_client(router, "alice", QUAD_4) == []

    def test_only_the_live_layouts_regions_apply(self):
        """An operator assigns every region a controller might need, and the
        layout on screen picks. That is the design, not a misconfiguration."""
        router = router_with(("A", [UPPER_LEFT, LEFT, UPPER], "alice"))
        assert regions_for_client(router, "alice", QUAD_4) == [UPPER_LEFT]
        assert regions_for_client(router, "alice", VERTICAL_2) == [LEFT]
        assert regions_for_client(router, "alice", HORIZONTAL_2) == [UPPER]
        assert regions_for_client(router, "alice", FULL) == []

    def test_no_client_id_owns_nothing(self):
        router = router_with(("A", [UPPER_LEFT], "alice"))
        assert regions_for_client(router, "", QUAD_4) == []

    def test_the_order_is_stable(self):
        a = router_with(("A", [UPPER_LEFT], "x"), ("B", [LOWER_LEFT], "x"))
        b = router_with(("B", [LOWER_LEFT], "x"), ("A", [UPPER_LEFT], "x"))
        assert regions_for_client(a, "x", QUAD_4) == regions_for_client(b, "x", QUAD_4)


class TestTheCropsAClientIsGiven:
    def test_contiguous_regions_become_one_crop(self):
        router = router_with(
            ("A", [UPPER_LEFT], "alice"), ("B", [LOWER_LEFT], "alice")
        )
        crops = crops_for_client(router, "alice", QUAD_4)
        assert len(crops) == 1
        assert (crops[0].x, crops[0].y, crops[0].width, crops[0].height) == (
            0.0, 0.0, 0.5, 1.0
        )

    def test_non_contiguous_regions_stay_separate(self):
        """The safety property, reached through the real router: a bounding
        box over two opposite quadrants is the whole screen."""
        router = router_with(
            ("A", [UPPER_LEFT], "alice"), ("B", [LOWER_RIGHT], "alice")
        )
        crops = crops_for_client(router, "alice", QUAD_4)
        assert len(crops) == 2
        assert all((c.width, c.height) == (0.5, 0.5) for c in crops)

    def test_owning_everything_is_the_whole_picture(self):
        router = router_with(
            ("A", [UPPER_LEFT], "a"), ("B", [UPPER_RIGHT], "a"),
            ("C", [LOWER_LEFT], "a"), ("D", [LOWER_RIGHT], "a"),
        )
        assert crops_for_client(router, "a", QUAD_4) == []


class TestTheMessageOnTheWire:
    def test_it_carries_the_layout_the_regions_and_the_rectangles(self):
        router = router_with(("A", [RIGHT], "alice"))
        message = regions_message(router, "alice", VERTICAL_2)
        assert message["layout"] == VERTICAL_2
        assert message["regions"] == [RIGHT]
        assert message["crops"] == [{"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}]

    def test_the_rectangles_are_normalised(self):
        """So a client needs no idea what resolution the source is running,
        and a resolution change mid-session does not invalidate them."""
        router = router_with(("A", [LOWER], "alice"))
        for crop in regions_message(router, "alice", HORIZONTAL_2)["crops"]:
            assert all(0.0 <= v <= 1.0 for v in crop.values())

    def test_a_client_that_owns_nothing_is_told_so_explicitly(self):
        """Silence cannot tell a client that *was* cropping to stop."""
        router = router_with(("A", [UPPER_LEFT], "bob"))
        message = regions_message(router, "alice", QUAD_4)
        assert message == {"layout": QUAD_4, "regions": [], "crops": []}

    def test_an_unknown_layout_is_full_screen(self):
        router = router_with(("A", [UPPER_LEFT], "alice"))
        assert regions_message(router, "alice", "QUAD_5") == {
            "layout": FULL, "regions": [], "crops": []
        }

    def test_the_fallback_message_is_full_screen(self):
        assert everyone_full_screen() == {"layout": FULL, "regions": [], "crops": []}

    def test_it_is_json_serialisable(self):
        """It goes into a control message, which is JSON."""
        router = router_with(
            ("A", [UPPER_LEFT], "alice"), ("B", [LOWER_RIGHT], "alice")
        )
        message = regions_message(router, "alice", QUAD_4)
        assert json.loads(json.dumps(message)) == message


class TestTheAdapterCarriesItsOwnRegions:
    """The GUI reads the assignment off the adapter, not off the router's
    channel.

    Both hold the same list today. The adapter is where it is *defined* --
    keyed by BD_ADDR, surviving the channel being torn down and rebuilt --
    and the channel's copy is a mirror maintained by one code path. Reading
    the mirror would show a stale value the moment a second path updates the
    config without it.
    """

    def test_a_fresh_adapter_reports_none(self):
        from server.bt.state import AdapterState

        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:01", hci_name="hci0")
        assert state.snapshot()["regions"] == []

    def test_it_travels_in_the_snapshot(self):
        from server.bt.state import AdapterState

        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:01", hci_name="hci0")
        state.regions = [UPPER_LEFT, LEFT]
        assert state.snapshot()["regions"] == [UPPER_LEFT, LEFT]

    def test_the_snapshot_hands_out_a_copy(self):
        """The GUI must not be able to reach in and change adapter state by
        mutating what it was given."""
        from server.bt.state import AdapterState

        state = AdapterState(bd_addr="AA:BB:CC:DD:EE:01", hci_name="hci0")
        state.regions = [UPPER_LEFT]
        state.snapshot()["regions"].append(LOWER_RIGHT)
        assert state.regions == [UPPER_LEFT]

    # "Does it survive a reconcile" is asserted in
    # tests/test_bt_state.py::test_transient_state_survives_a_sync, beside
    # every other field with the same requirement. One place asserting the
    # object is never reconstructed beats one per field.
