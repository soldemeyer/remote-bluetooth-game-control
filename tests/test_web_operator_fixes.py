"""Three things the operator could see were wrong and had no way around.

Each is small, and each made a control lie or refuse:

  * the adapter dropdown listed controllers in an order that was not their
    numbering, so the entry already assigned to the slot sorted last;
  * the "Tell clients to use" field refilled itself the instant it was
    cleared, so a wrong address could not be removed -- and that address is
    what every client is told to fetch video from;
  * a console taking its controllers back in whatever order the radios came up
    left the operator no way to choose player numbers.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "server" / "web" / "static"
CLIENTS_JS = STATIC / "js" / "sections" / "clients.js"
VIDEO_JS = STATIC / "js" / "sections" / "video.js"
INDEX_HTML = STATIC / "index.html"
APP_JS = STATIC / "app.js"

needs_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; this check is advisory and skips cleanly",
)

# nav.js and dom.js touch the document at module scope. These are stub gaps,
# not faults -- the modules under test do not use any of it.
STUBS = """
globalThis.addEventListener = () => {};
const noop = () => {};
const element = {
  removeAttribute: noop, setAttribute: noop, addEventListener: noop,
  classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
  querySelector: () => null, querySelectorAll: () => [],
  dataset: {}, style: {},
};
globalThis.document = {
  documentElement: element, body: element,
  getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], addEventListener: noop,
  createElement: () => element,
};
globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop };
globalThis.matchMedia = () => ({ matches: false, addEventListener: noop });
globalThis.window = globalThis;
const BASE = 'file://' + process.env.RBGC_STATIC.split('\\\\').join('/');
"""


def run_node(body: str, env: dict) -> str:
    source = STUBS + textwrap.dedent(body).strip()
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "RBGC_STATIC": str(STATIC), **env},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


# ==========================================================================
# 1. The adapter dropdown is ordered by controller number
# ==========================================================================


@needs_node
class TestTheAdapterDropdownIsOrderedByNumber:
    """Reported with a screenshot reading "Controller 2, 3, 4, 1".

    The list came straight from the router's channel order, which is
    assignment-dependent -- so the adapter already assigned to the slot sorted
    last, which is precisely the entry the operator is looking for. The
    numbers are the whole point of those labels.
    """

    BODY = """
        const state = await import(BASE + '/js/state.js');
        const mod = await import(BASE + '/js/sections/clients.js');
        // adapterNumber reads the shared status; without seeding it every
        // adapter is unnumbered and the sort is a no-op that passes vacuously.
        state.setLatest({ hardware: JSON.parse(process.env.RBGC_HARDWARE) });
        const channels = JSON.parse(process.env.RBGC_CHANNELS);
        const sorted = channels.slice().sort(
          (a, b) => mod.adapterNumber(a) - mod.adapterNumber(b));
        console.log(JSON.stringify(sorted.map((c) => mod.adapterName(c))));
    """

    def order(self, hardware, channels):
        return json.loads(run_node(self.BODY, {
            "RBGC_HARDWARE": json.dumps(hardware),
            "RBGC_CHANNELS": json.dumps(channels),
        }))

    def test_it_sorts_by_number_not_by_arrival(self):
        hardware = [
            {"bd_addr": "AA", "number": 2, "display_name": "Controller 2"},
            {"bd_addr": "BB", "number": 3, "display_name": "Controller 3"},
            {"bd_addr": "CC", "number": 4, "display_name": "Controller 4"},
            {"bd_addr": "DD", "number": 1, "display_name": "Controller 1"},
        ]
        # The order the report showed: the assigned one last.
        channels = [{"bd_addr": a, "hci": "hci0"} for a in ("AA", "BB", "CC", "DD")]

        assert self.order(hardware, channels) == [
            "Controller 1", "Controller 2", "Controller 3", "Controller 4",
        ]

    def test_an_unnumbered_adapter_sorts_last(self):
        """Zero would put a half-configured adapter above Controller 1."""
        hardware = [
            {"bd_addr": "AA", "number": 1, "display_name": "Controller 1"},
            {"bd_addr": "BB", "display_name": "", "hci": "hci9"},
        ]
        channels = [{"bd_addr": "BB", "hci": "hci9"}, {"bd_addr": "AA", "hci": "hci0"}]
        assert self.order(hardware, channels)[0] == "Controller 1"


class TestTheDropdownSourceIsSorted:
    """The Node tests exercise the comparator; this pins that the list actually
    rendered goes through it, which a unit test of a helper cannot see."""

    def test_the_option_list_sorts_before_rendering(self):
        source = CLIENTS_JS.read_text(encoding="utf-8")
        statement = source.split("const available =", 1)[1].split("\n\n", 1)[0]
        assert "adapterNumber" in statement, (
            "the dropdown's own list is not ordered by controller number"
        )


# ==========================================================================
# 2. "Tell clients to use" can be emptied
# ==========================================================================


@needs_node
class TestTheAdvertiseFieldsCanBeCleared:
    """**The value here is handed to every client as the video address.**

    The old guard was `value === ''` -- meant to avoid overwriting what the
    operator was typing, and its actual effect was to refill the field the
    instant it was cleared. Status arrives at 10 Hz, so there was no window in
    which to press Save, and a wrong address could not be removed by anyone.
    """

    BODY = """
        const mod = await import(BASE + '/js/sections/video.js');
        const seed = mod.seedOnChange;
        const field = { value: '', dataset: {} };
        const log = [];

        // The server holds an address; it reaches the empty field once.
        seed(field, '192.168.1.20');
        log.push(field.value);

        // The operator clears it, and status keeps arriving unchanged.
        field.value = '';
        for (let i = 0; i < 5; i += 1) seed(field, '192.168.1.20');
        log.push(field.value);

        // Saving makes the server agree; still nothing written.
        for (let i = 0; i < 5; i += 1) seed(field, '');
        log.push(field.value);

        // Something else changes it, and the field follows.
        seed(field, '10.0.0.9');
        log.push(field.value);

        console.log(JSON.stringify(log));
    """

    def test_a_cleared_field_stays_cleared_and_still_follows_the_server(self):
        seeded, after_clear, after_save, after_change = json.loads(
            run_node(self.BODY, {}))

        assert seeded == "192.168.1.20", "the field was never seeded"
        assert after_clear == "", "the field refilled itself; this is the bug"
        assert after_save == ""
        assert after_change == "10.0.0.9", (
            "a change made elsewhere must still reach the field"
        )


class TestTheAdvertiseFieldGuardIsGone:
    def test_the_empty_value_guard_is_not_back(self):
        source = VIDEO_JS.read_text(encoding="utf-8")
        for field in ("video-advertise-host", "video-advertise-port"):
            start = source.find(field)
            assert start >= 0, f"{field} is no longer referenced"
            assert "value === ''" not in source[start:start + 400], (
                f"{field} is refilled whenever it is empty again"
            )


# ==========================================================================
# 3. Sleep when the console disconnects
# ==========================================================================


class TestSleepOnDisconnect:
    """The console assigns player numbers in the order controllers connect,
    and we are the peripheral -- so a bonded console takes back whichever
    adapter it sees advertising, within about a second. After a restart or a
    console power cycle all four race, and the operator can neither choose the
    order nor read the numbers back afterwards.
    """

    def test_the_setting_exists_and_is_off_by_default(self):
        """Off by default: it turns an automatic recovery into one that needs
        an operator, which is the wrong trade for anyone not chasing player
        order."""
        from server.config import ServerConfig

        assert ServerConfig().ble_sleep_on_disconnect is False

    def test_it_survives_a_round_trip_through_the_config_file(self, tmp_path):
        from server import config as config_module

        cfg = config_module.ServerConfig()
        cfg.ble_sleep_on_disconnect = True
        path = tmp_path / "server.json"
        config_module.save(cfg, path)

        assert config_module.load(path).ble_sleep_on_disconnect is True

    def test_the_web_gui_offers_it(self):
        """Every part of a feature working with no control to reach it is the
        failure the split-screen work already recorded."""
        html = INDEX_HTML.read_text(encoding="utf-8")
        assert 'id="bt-sleep-on-disconnect"' in html

        app = APP_JS.read_text(encoding="utf-8")
        assert "bt-sleep-on-disconnect" in app, "the toggle is not wired up"
        assert "ble_sleep_on_disconnect" in app, "nothing is posted"

    def test_the_status_carries_it_so_the_toggle_can_show_its_state(self):
        source = (ROOT / "server" / "web" / "app.py").read_text(encoding="utf-8")
        assert '"ble_sleep_on_disconnect"' in source

    def test_classic_is_unaffected(self):
        """Classic reconnects by paging a host we chose, from our own outgoing
        loop, so the order is already ours and there is nothing to arbitrate.
        Parking an adapter there would only break reconnection."""
        source = (ROOT / "server" / "bt" / "adapter.py").read_text(encoding="utf-8")
        body = source.split("def _sleep_on_disconnect", 1)[1].split("\n    def ", 1)[0]
        assert 'self._transport() != "ble"' in body
        assert "return False" in body

    def test_it_reads_the_live_setting_rather_than_a_cached_one(self):
        """"A setting that only takes effect at startup is a setting that does
        nothing" -- recorded in CLAUDE.md about the broker, and the same shape
        here."""
        source = (ROOT / "server" / "bt" / "adapter.py").read_text(encoding="utf-8")
        body = source.split("def _sleep_on_disconnect", 1)[1].split("\n    def ", 1)[0]
        assert "self._config" in body, (
            "the setting is cached, so the toggle would need a restart"
        )
