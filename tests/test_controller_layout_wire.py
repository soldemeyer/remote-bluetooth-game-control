"""The controller type, from the client's dropdown to the adapter card.

The server draws the pad a player is holding on the card that pad is assigned
to. Four identical generic shells say nothing about who is who, and the type
was never on the wire: the client has had it per slot since the type column was
added, and `SET_CONTROLLERS` carried only the username and the device name.

Three properties, and each is a way this goes quietly wrong:

  * **Old clients keep working.** The field is additive and `SET_CONTROLLERS`
    reads keys by name, so a client that has not been updated sends nothing and
    must get an empty string rather than an exception on the datapath thread.
  * **Both clients agree.** The GUI resolves the slot's type, the configuration's
    type, then the default. A headless client with no copy of that logic would
    report nothing and every card would show the generic shell.
  * **The message still fits.** `encode_control` refuses an oversized message
    *whole* -- it does not truncate -- which is the failure `VIDEO_STATUS` hit
    when two variable-length structures grew past the ceiling and every status
    was silently refused.
"""

from __future__ import annotations

import json

from client.config import ClientConfig, ControllerConfig
from common import protocol


class TestTheFieldSurvivesTheRoundTrip:
    def test_a_reported_layout_reaches_the_snapshot(self):
        from server.sessions import ControllerSlot

        slot = ControllerSlot(slot=0)
        slot.layout = "n64"
        assert slot.snapshot()["layout"] == "n64"

    def test_a_client_that_does_not_send_it_gets_an_empty_string(self):
        """An older client, which is every client until it is updated. The GUI
        falls back to the generic shell on empty rather than guessing."""
        from server.sessions import ControllerSlot

        slot = ControllerSlot(slot=0)
        assert slot.snapshot()["layout"] == ""

    def test_the_datapath_reads_it_off_the_message(self):
        import inspect

        from server import datapath

        source = inspect.getsource(datapath.Datapath._handle_control)
        assert 'slot_state.layout = str(entry.get("layout", ""))' in source, (
            "the field is on the wire and nothing unpacks it"
        )

    def test_an_absurd_value_is_truncated_rather_than_stored(self):
        import inspect

        from server import datapath

        source = inspect.getsource(datapath.Datapath._handle_control)
        assert 'entry.get("layout", ""))[:16]' in source


class TestBothClientsResolveItTheSameWay:
    def test_the_slot_choice_wins(self):
        cfg = ClientConfig()
        cfg.controllers = [ControllerConfig(slot=0, layout="switch")]
        assert cfg.controller_layout(0) == "switch"

    def test_the_configuration_is_the_fallback(self):
        cfg = ClientConfig()
        cfg.controllers = [ControllerConfig(slot=0, configuration="Retro")]
        cfg.configurations = [{"name": "Retro", "layout": "snes"}]
        assert cfg.controller_layout(0) == "snes"

    def test_the_slot_still_wins_over_its_configuration(self):
        """Slots share configurations by name, so storing the active type only
        on the configuration meant two slots fought over it."""
        cfg = ClientConfig()
        cfg.controllers = [ControllerConfig(slot=0, configuration="Retro", layout="n64")]
        cfg.configurations = [{"name": "Retro", "layout": "snes"}]
        assert cfg.controller_layout(0) == "n64"

    def test_an_unconfigured_slot_gets_the_default(self):
        from client.config import DEFAULT_LAYOUT

        assert ClientConfig().controller_layout(3) == DEFAULT_LAYOUT

    def test_a_configuration_that_no_longer_exists_does_not_raise(self):
        """Deleting a configuration clears the slots pointing at it, but a
        hand-edited file can still name one that is gone."""
        from client.config import DEFAULT_LAYOUT

        cfg = ClientConfig()
        cfg.controllers = [ControllerConfig(slot=0, configuration="Deleted")]
        assert cfg.controller_layout(0) == DEFAULT_LAYOUT

    def test_both_senders_include_it(self):
        import inspect

        from client import main as headless
        from client.gui import app as gui

        for module in (headless, gui):
            source = inspect.getsource(module)
            assert '"layout": s.layout' in source, (
                f"{module.__name__} does not send the controller type"
            )


class TestTheMessageStillFits:
    def test_a_full_four_slot_payload_has_headroom(self):
        """`encode_control` refuses an oversized message whole -- it does not
        truncate -- so the interesting number is what is *left*, not whether
        today's message happens to fit.

        Worst case: four slots, every string at its server-side cap, and the
        longest family name.
        """
        body = {
            "client_name": "c" * 64,
            "controllers": [
                {
                    "slot": slot,
                    "username": "u" * 32,
                    "device_name": "d" * 64,
                    "layout": "switch2",
                }
                for slot in range(4)
            ],
        }
        encoded = json.dumps(body).encode("utf-8")

        ceiling = protocol.MAX_DATAGRAM
        assert len(encoded) < ceiling, (
            f"the worst case is {len(encoded)} bytes against a {ceiling} ceiling"
        )
        headroom = ceiling - len(encoded)
        assert headroom > 200, (
            f"only {headroom} bytes left for the next field; VIDEO_STATUS was "
            f"refused whole when it ran out"
        )

    def test_the_field_costs_what_it_looks_like(self):
        without = json.dumps({
            "controllers": [
                {"slot": s, "username": "u" * 32, "device_name": "d" * 64}
                for s in range(4)
            ]
        })
        with_it = json.dumps({
            "controllers": [
                {"slot": s, "username": "u" * 32, "device_name": "d" * 64,
                 "layout": "switch2"}
                for s in range(4)
            ]
        })
        assert len(with_it) - len(without) < 100
