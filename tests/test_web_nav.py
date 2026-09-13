"""Navigation, and the blank page a rename would otherwise have caused.

`showView` finds its section by `data-view` and used to return silently when
there was no match. That is fine while nothing is ever renamed. Six tabs became
four -- Overview's cards moved into the header, and Bluetooth and Clients merged
into Controllers -- and every existing install has one of the retired names
sitting in `localStorage['rbgc.view']` from the last time it was used.

So the first load after deploying would have been an empty shell: the header,
the rail, and no section active at all, with nothing anywhere to say why. Not a
crash, not a console error, and not something a fresh browser profile would
ever reproduce -- which is what every test and every local check runs in.

Two defences, and both are here because the alias map alone only covers the
three names known today. The next rename is the one nobody remembers to add.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
INDEX = STATIC / "index.html"
NAV = STATIC / "js" / "nav.js"


def views() -> set[str]:
    return set(re.findall(
        r'<section class="view" data-view="([^"]+)"',
        INDEX.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Driven through the real showView, with a DOM built for it
# ---------------------------------------------------------------------------

#: Enough of a document for `showView` to do its work and report the result:
#: a rail of buttons and a set of sections, both looked up by attribute.
BODY = """
    const VIEWS = JSON.parse(process.env.RBGC_VIEWS);
    const STORED = process.env.RBGC_STORED;

    // `noop` is already declared by the shared prelude in tests/webjs.py.
    const nop = () => {};
    function makeEl(view, cls) {
      return {
        dataset: { view },
        _classes: new Set([cls]),
        classList: {
          add(c) { this._el._classes.add(c); },
          remove(c) { this._el._classes.delete(c); },
          toggle(c, on) { on ? this._el._classes.add(c) : this._el._classes.delete(c); },
          contains(c) { return this._el._classes.has(c); },
        },
        setAttribute: nop, removeAttribute: nop,
      };
    }

    const sections = VIEWS.map((v) => makeEl(v, 'view'));
    const items = VIEWS.map((v) => makeEl(v, 'rail-item'));
    for (const el of [...sections, ...items]) el.classList._el = el;

    const rail = {
      querySelectorAll: () => items,
      dataset: {}, classList: { toggle: nop, add: nop, remove: nop },
    };

    globalThis.localStorage = {
      _v: STORED,
      getItem(k) { return k === 'rbgc.view' ? this._v : null; },
      setItem(k, v) { if (k === 'rbgc.view') this._v = v; },
    };

    globalThis.document = {
      documentElement: { setAttribute: nop, removeAttribute: nop },
      getElementById: (id) => (id === 'rail' ? rail : null),
      querySelector: (sel) => {
        const m = /data-view="([^"]+)"/.exec(sel);
        return m ? sections.find((s) => s.dataset.view === m[1]) || null : null;
      },
      querySelectorAll: (sel) => (sel === '.view' ? sections : []),
      addEventListener: nop,
      dispatchEvent: nop,
    };
    globalThis.addEventListener = nop;
    globalThis.dispatchEvent = nop;
    globalThis.CustomEvent = function (type, init) {
      return { type, detail: (init || {}).detail };
    };

    // Importing runs restorePreferences(), which is the path under test:
    // it reads the stored name and has to land on a real view.
    const nav = await import(BASE + '/js/nav.js');

    const active = sections.filter((s) => s.classList.contains('active'))
      .map((s) => s.dataset.view);
    console.log(JSON.stringify({ active, current: nav.activeView() }));
"""


def restore(stored: str, view_names: list[str] | None = None) -> dict:
    return json.loads(run_node(BODY, {
        "RBGC_STORED": stored,
        "RBGC_VIEWS": json.dumps(view_names or sorted(views())),
    }))


@needs_node
class TestARetiredViewNameLandsOnSomething:
    def test_overview_lands_on_a_real_view(self):
        out = restore("overview")
        assert out["active"], "no section is showing -- this is the blank page"
        assert out["current"] in views()

    def test_clients_lands_on_controllers(self):
        assert restore("clients")["current"] == "controllers"

    def test_adapters_lands_on_controllers(self):
        assert restore("adapters")["current"] == "controllers"

    def test_a_name_nobody_thought_of_still_lands(self):
        """The alias map covers what was retired *this* time. The fallback in
        `showView` covers the next rename, which is the one with no alias."""
        out = restore("some-view-from-the-future")
        assert out["active"], "no section is showing"
        assert len(out["active"]) == 1

    def test_a_current_name_is_left_alone(self):
        assert restore("video")["current"] == "video"

    def test_exactly_one_view_is_ever_active(self):
        for stored in ("overview", "clients", "video", "nonsense"):
            assert len(restore(stored)["active"]) == 1, stored


@needs_node
class TestTheFallbackDoesNotRecurse:
    def test_a_document_with_no_views_at_all_terminates(self):
        """`showView` falls back to the default view, which is itself found by
        the same lookup. Without a guard that is infinite recursion, and the
        page dies with a stack overflow instead of a missing section."""
        out = json.loads(run_node(BODY, {
            "RBGC_STORED": "overview",
            "RBGC_VIEWS": json.dumps([]),
        }))
        assert out["active"] == []


class TestTheSourceKeepsBothDefences:
    def test_the_storage_key_was_not_bumped(self):
        """Bumping it would also avoid the blank page, by throwing away the
        tab the operator was last on. The aliases keep the intent."""
        assert "'rbgc.view'" in NAV.read_text(encoding="utf-8")

    def test_every_alias_target_exists(self):
        nav = NAV.read_text(encoding="utf-8")
        block = re.search(r"VIEW_ALIASES = \{(.*?)\}", nav, re.S)
        assert block, "the alias map is gone"
        targets = set(re.findall(r":\s*'([^']+)'", block.group(1)))
        assert targets <= views(), sorted(targets - views())
