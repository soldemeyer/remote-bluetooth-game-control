"""One Apply for "What the console sees", and why it cannot just send both.

The card sets two server-wide things: the report layout the console receives,
and who the adapters claim to be. Two Apply buttons for two dropdowns in one
card is one button too many -- but the obvious single button, the one that
posts both every time, is worse than what it replaces.

Neither endpoint checks whether anything changed. `set_identity` renames every
adapter and re-registers the DeviceID, and this card's own text says that needs
a re-pair. So a button that re-sent the identity whenever somebody changed the
*profile* would cost a console its controllers, for nothing, with nothing on
screen to explain it.

It therefore sends only what actually moved -- compared against the live status,
which is the server's own answer and is already arriving ten times a second.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
INDEX = STATIC / "index.html"
APP_JS = STATIC / "app.js"


class TestThereIsOneButton:
    def page(self) -> str:
        return INDEX.read_text(encoding="utf-8")

    def test_the_two_old_ones_are_gone(self):
        page = self.page()
        assert "bt-profile-save" not in page
        assert "bt-identity-save" not in page

    def test_there_is_exactly_one_apply_in_the_card(self):
        page = self.page()
        card = page.split('id="identity-card"', 1)[1].split("</div>\n\n", 1)[0]
        assert card.count("Apply") == 1, "the card has more than one Apply"
        assert 'id="bt-apply"' in card

    def test_both_dropdowns_are_still_there(self):
        page = self.page()
        assert 'id="bt-profile"' in page
        assert 'id="bt-identity"' in page


#: Drives the real click handler. `app.js` is the entry point and registers a
#: great deal at import, so the handler is reached the way the browser reaches
#: it -- by loading the module against a document that has the button in it.
BODY = """
    const nop = () => {};
    const posted = [];
    const banners = [];

    function node(id) {
      const el = { id, value: '', textContent: '', title: '', disabled: false,
                   checked: false, dataset: {}, _classes: new Set(),
                   _listeners: {},
                   addEventListener(type, fn) { el._listeners[type] = fn; },
                   setAttribute: nop, removeAttribute: nop,
                   getAttribute: () => null,
                   querySelector: () => null, querySelectorAll: () => [],
                   focus: nop, closest: () => null };
      el.classList = { add: (c) => el._classes.add(c),
                       remove: (c) => el._classes.delete(c),
                       toggle: nop, contains: (c) => el._classes.has(c) };
      return el;
    }

    const nodes = {};
    // Every id the entry point looks up, invented on demand: `app.js`
    // registers delegated handlers at import and throws on a missing
    // container, so a fixed list would have to track every one of them.
    const get = (id) => (nodes[id] || (nodes[id] = node(id)));

    globalThis.document = {
      documentElement: node('html'), body: node('body'),
      getElementById: (id) => get(id),
      querySelector: () => null, querySelectorAll: () => [],
      addEventListener: nop, dispatchEvent: nop, createElement: () => node('new'),
    };
    globalThis.addEventListener = nop;
    globalThis.location = { protocol: 'http:', host: 'x', reload: nop };
    globalThis.WebSocket = function () { return { close: nop }; };
    globalThis.fetch = (path, init) => {
      if (init && init.method === 'POST') {
        posted.push({ path, body: JSON.parse(init.body || '{}') });
      }
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) });
    };

    const state = await import(BASE + '/js/state.js');
    const api = await import(BASE + '/js/api.js');
    await import(BASE + '/app.js');

    const scenario = JSON.parse(process.env.RBGC_SCENARIO);
    state.setLatest(scenario.status);
    get('bt-profile').value = scenario.profile;
    get('bt-identity').value = scenario.identity;

    const handler = get('bt-apply')._listeners.click;
    await handler({ currentTarget: get('bt-apply') });
    await new Promise((r) => setTimeout(r, 30));

    console.log(JSON.stringify({
      posted, banner: get('banner').textContent,
    }));
"""


def scenario(*, profile, identity, current_profile="generic",
             current_identity="generic"):
    return {
        "profile": profile,
        "identity": identity,
        "status": {
            "adapters": [{"bd_addr": "AA", "profile": current_profile}],
            "identity": current_identity,
            "server": {}, "hardware": [], "clients": [],
            "video": None, "datapath": {},
        },
    }


@needs_node
class TestItSendsOnlyWhatMoved:
    def run(self, case):
        return json.loads(run_node(BODY, {"RBGC_SCENARIO": json.dumps(case)}))

    def paths(self, case):
        return [p["path"] for p in self.run(case)["posted"]]

    def test_changing_the_profile_alone_does_not_touch_the_identity(self):
        """**The reason this is not a two-line button.** Re-applying the
        identity renames every radio and re-registers the DeviceID, which this
        card says needs a re-pair -- so doing it as a side effect of a profile
        change would cost a console its controllers."""
        paths = self.paths(scenario(profile="switch_pro", identity="generic"))
        assert "/api/bluetooth/profile" in paths
        assert "/api/bluetooth/identity" not in paths

    def test_changing_the_identity_alone_does_not_touch_the_profile(self):
        paths = self.paths(scenario(profile="generic", identity="8bitdo"))
        assert "/api/bluetooth/identity" in paths
        assert "/api/bluetooth/profile" not in paths

    def test_changing_both_sends_both(self):
        paths = self.paths(scenario(profile="switch_pro", identity="8bitdo"))
        assert sorted(paths) == [
            "/api/bluetooth/identity", "/api/bluetooth/profile"]

    def test_changing_neither_sends_nothing_and_says_so(self):
        """A button that silently does nothing is indistinguishable from one
        that is broken -- which is the complaint this whole GUI keeps
        answering."""
        out = self.run(scenario(profile="generic", identity="generic"))
        assert out["posted"] == []
        assert "Nothing to change" in out["banner"]

    def test_it_sends_the_value_the_operator_chose(self):
        out = self.run(scenario(profile="switch_pro", identity="8bitdo"))
        bodies = {p["path"]: p["body"] for p in out["posted"]}
        assert bodies["/api/bluetooth/profile"]["profile"] == "switch_pro"
        assert bodies["/api/bluetooth/identity"]["identity"] == "8bitdo"

    def test_an_absent_identity_in_the_status_reads_as_generic(self):
        """A server that has never been told one reports nothing, and picking
        `generic` there must not count as a change."""
        case = scenario(profile="generic", identity="generic")
        del case["status"]["identity"]
        assert self.run(case)["posted"] == []
