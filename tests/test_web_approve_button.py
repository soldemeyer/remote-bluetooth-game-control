"""The client card must rebuild after Approve is clicked.

Reported as "clicking Approve does nothing". The click reached the server every
time -- the client really was approved -- but the card never redrew, so the pill
still read PENDING and the Approve button stayed where it was. From the
operator's side that is indistinguishable from a dead button, and the natural
response is to click it again.

The cause is a guard that exists for a good reason. Restructuring a card while
someone is interacting with it eats the interaction, so the renderer refuses to
rebuild while focus is inside the container. But **a clicked button keeps
focus**, and approving is precisely the action that changes the card's
structure -- so the one click that needs a rebuild was the one click that
blocked it, for as long as the button stayed focused.

Buttons are momentary: by the time focus is on one, its click has already been
dispatched and there is nothing left to lose. A <select> being browsed or an
<input> being typed into is different, and those must still be protected.

Asserted against the shipped JavaScript because that is where the behaviour
lives -- there is no Python to exercise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"


def app_source() -> str:
    """Entry plus every module, as one string.

    These assertions are substring searches, so they have to follow the code
    when it moves between files -- otherwise a split silently empties them.
    """
    parts = [(STATIC / "app.js").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((STATIC / "js").rglob("*.js"))]
    return chr(10).join(parts)


@pytest.fixture(scope="module")
def app_js() -> str:
    """The whole client script, entry plus modules.

    `app.js` was one file; it is now an entry point over `js/`. These tests
    assert on the source as *text*, so they have to read the same code
    wherever it lives -- otherwise splitting a file silently empties the
    assertions rather than failing them.
    """
    parts = [(STATIC / "app.js").read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in sorted((STATIC / "js").rglob("*.js"))]
    return chr(10).join(parts)


class TestTheFocusGuardDistinguishesControls:
    def test_the_predicate_exists(self, app_js):
        assert "function holdsUncommittedState(" in app_js

    def test_stateful_controls_are_protected(self, app_js):
        # A half-browsed dropdown or a partly typed field holds something the
        # operator has not committed; rebuilding under it loses their work.
        for tag in ("'SELECT'", "'INPUT'", "'TEXTAREA'"):
            assert tag in app_js, f"{tag} must still block a rebuild"

    def test_the_rebuild_guard_uses_the_predicate(self, app_js):
        # Not bare focus containment, which is what blocked the rebuild.
        assert "holdsUncommittedState(focused)" in app_js

    def test_bare_focus_containment_is_gone(self, app_js):
        # The exact expression that caused it. A focused *button* satisfied
        # this, so approving a client could never redraw the card.
        assert (
            "document.activeElement && container.contains(document.activeElement)"
            not in app_js
        )

    def test_a_held_pointer_still_suspends_rebuilds(self, app_js):
        # Separate protection, and still needed: a redraw between mousedown and
        # mouseup lands the two halves of a click on different nodes, which is
        # what ate button presses before either guard existed.
        assert "pointerDown" in app_js

    def test_per_control_writes_are_still_guarded(self, app_js):
        # busy() protects an individual control from being written to while it
        # is focused. That is a different question from whether the container
        # may be restructured, and this fix must not have removed it.
        assert "function busy(element)" in app_js
        assert "document.activeElement === element" in app_js


class TestTheApproveButtonIsStillWired:
    def test_the_action_is_declared(self, app_js):
        assert 'data-action="approve"' in app_js

    def test_it_posts_to_the_approve_endpoint(self, app_js):
        assert "'/api/approve'" in app_js

    def test_the_card_key_includes_the_state(self, app_js):
        # The rebuild is keyed on client state, so PENDING -> APPROVED is what
        # triggers the redraw in the first place. Without the state in the key
        # the card would never rebuild at all, guard or no guard.
        assert "${c.client_id}:${c.state}" in app_js


class TestTheLiveControllerPreview:
    """The GUI must be able to show what the server actually received.

    No counter answers this. A client can be connected, approved, assigned and
    streaming thousands of packets with zero drops while every one of them
    carries a neutral controller -- and every indicator on the page stays green
    throughout. That happened: a console ignored all input for an evening, and
    the cause turned out to be 1378 byte-identical idle HID reports leaving the
    Pi. The presses never reached the server.

    Seeing a control light up here proves the chain works as far as the server
    and moves the search downstream; seeing nothing proves the opposite. Either
    answer is worth more than every packet counter combined.
    """

    def test_the_slot_snapshot_carries_the_input(self):
        from server.sessions import ControllerSlot

        slot = ControllerSlot(slot=0)
        slot.buttons = 0b101
        slot.left_x = -32768
        slot.right_trigger = 255

        snap = slot.snapshot()["input"]
        assert snap["buttons"] == 0b101
        assert snap["left_x"] == -32768
        assert snap["right_trigger"] == 255

    def test_a_fresh_slot_reports_neutral_rather_than_nothing(self):
        """Absent input and neutral input must be distinguishable, but a slot
        that has simply not been pressed yet is neutral, not missing."""
        from server.sessions import ControllerSlot

        snap = ControllerSlot(slot=0).snapshot()["input"]
        assert snap["buttons"] == 0
        assert all(snap[axis] == 0 for axis in ("left_x", "left_y", "right_x", "right_y"))

    def test_the_datapath_mirrors_input_into_the_slot(self):
        """Pinned by wiring, because the failure mode is a preview that works
        and always reads neutral -- which looks exactly like a broken client."""
        import inspect

        from server import datapath

        source = inspect.getsource(datapath.Datapath._handle_input)
        assert "slot_state.buttons = state.buttons" in source
        assert "slot_state.left_x" in source

    def test_the_gui_maps_every_logical_button(self):
        """Every bit in common.state.Button that the artwork can show must be
        mapped, or a control silently never lights."""
        from common.state import Button

        # The whole module set: the pad preview moved to js/sections/pad.js,
        # and reading only the entry would find nothing and assert nothing.
        js = app_source()
        for name, group in (
            ("A", "c_a"), ("B", "c_b"), ("X", "c_x"), ("Y", "c_y"),
            ("LEFT_BUMPER", "c_lb"), ("RIGHT_BUMPER", "c_rb"),
            ("BACK", "c_back"), ("START", "c_start"), ("GUIDE", "c_guide"),
            ("LEFT_STICK", "c_lstick"), ("RIGHT_STICK", "c_rstick"),
            ("DPAD_UP", "c_dup"), ("DPAD_DOWN", "c_ddown"),
            ("DPAD_LEFT", "c_dleft"), ("DPAD_RIGHT", "c_dright"),
            ("LEFT_TRIGGER", "c_lt"), ("RIGHT_TRIGGER", "c_rt"),
        ):
            bit = int(getattr(Button, name)).bit_length() - 1
            assert f"{group}: 1 << {bit}" in js, (
                f"{group} must map to Button.{name} (bit {bit})"
            )

    def test_the_artwork_is_served_and_carries_the_control_groups(self):
        svg = (STATIC / "controllers" / "logical.svg").read_text(encoding="utf-8")
        for group in ("c_a", "c_b", "c_dup", "c_lstick", "c_rstick", "c_lt", "c_rt"):
            assert f'id="{group}"' in svg


class TestAnUnboundControllerSaysSo:
    """A pad with no bindings must not look like a player holding still.

    This is the failure that cost an evening. The client acquires the pad,
    reports it connected, and streams a perfectly formed neutral state at full
    rate; the server counts thousands received with zero dropped; the router
    assigns it; the console receives byte-identical idle HID reports forever.
    Every indicator in both GUIs is green and the console does nothing.

    The search went to Bluetooth -- descriptors, pairing, GATT, link timeouts --
    because that is where the symptom appeared. The fault was never below the
    client's mapping layer.
    """

    def test_the_flag_exists_and_is_its_own_bit(self):
        from common.protocol import InputFlags

        assert InputFlags.CONTROLLER_UNBOUND == 1 << 2
        # Must not collide with the flags that were already on the wire.
        assert InputFlags.CONTROLLER_UNBOUND != InputFlags.CONTROLLER_DISCONNECTED
        assert InputFlags.CONTROLLER_UNBOUND != InputFlags.REQUEST_ACK

    def test_the_slot_reports_it(self):
        from server.sessions import ControllerSlot

        slot = ControllerSlot(slot=0)
        assert slot.snapshot()["unbound"] is False
        slot.unbound = True
        assert slot.snapshot()["unbound"] is True

    def test_the_datapath_records_it(self):
        import inspect

        from server import datapath

        source = inspect.getsource(datapath.Datapath._handle_input)
        assert "CONTROLLER_UNBOUND" in source
        assert "slot_state.unbound" in source

    def test_an_unbound_pad_is_still_reported_connected(self):
        """It is present and healthy -- it just cannot produce input.

        Marking it disconnected would be a lie that makes the console latch a
        neutral state for a different reason, and would hide the real problem
        behind a second wrong explanation.
        """
        from common.protocol import InputFlags

        assert not (InputFlags.CONTROLLER_UNBOUND & InputFlags.CONTROLLER_DISCONNECTED)

    def test_the_backend_can_be_asked(self):
        from client.input.base import InputBackend

        assert hasattr(InputBackend, "is_bound")

    def test_backends_without_bindings_answer_yes(self):
        """Synthetic and keyboard have no notion of binding, so the default
        must be True or they would all report themselves broken."""
        from client.input.synthetic import SyntheticBackend

        backend = SyntheticBackend(count=1)
        backend.open()
        try:
            devices = backend.list_devices()
            assert backend.is_bound(devices[0].instance_id) is True
        finally:
            backend.close()

    def test_the_gui_explains_it_rather_than_reporting_no_input(self):
        # The whole module set: the pad preview moved to js/sections/pad.js,
        # and reading only the entry would find nothing and assert nothing.
        js = app_source()
        assert "slot.unbound" in js
        assert "no bindings" in js
