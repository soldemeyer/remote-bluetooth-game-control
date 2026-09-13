"""The header's Video tile, and the fields it used to invent.

Reported as the tile reading *Waiting* over a video server that was connected
and streaming to a viewer.

It asked the status for `video.streaming`, `video.clients` and `video.error` --
none of which `VideoRegistry.snapshot()` has ever carried -- and for
`video.source.available`, where `source` is a *string* like
"192.168.1.116:47810" and the property is therefore always undefined. Every
branch fell to the same side, so the tile could not have reported anything else.

This is the second time in this file's history: the Bluetooth tile read
`status.adapters` (the router's channels) for an `enabled` field that lives on
`status.hardware`. Both produce a confidently wrong display rather than a
missing one, which is the harder kind to notice.

So there are two tests here, and the second is the one that matters: the tile's
own behaviour, and a check that the fields it reads still exist on the object it
reads them from.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
SUMMARY_JS = STATIC / "js" / "sections" / "summary.js"


#: Renders one status through the real tile code and reports what it shows.
BODY = """
    const nop = () => {};
    const nodes = {};
    function node(id) {
      const el = { id, textContent: '', dataset: {},
                   classList: { add: nop, remove: nop, toggle: nop,
                                contains: () => false } };
      return el;
    }
    const get = (id) => (nodes[id] || (nodes[id] = node(id)));
    const cards = {};
    globalThis.document = {
      getElementById: (id) => get(id),
      querySelector: (sel) => {
        const m = /data-summary="([^"]+)"/.exec(sel);
        if (!m) return null;
        return cards[m[1]] || (cards[m[1]] = node(m[1]));
      },
      querySelectorAll: () => [], addEventListener: nop,
    };
    globalThis.addEventListener = nop;

    const mod = await import(BASE + '/js/sections/summary.js');
    const status = JSON.parse(process.env.RBGC_STATUS);
    mod.renderHeaderSummary(status);

    console.log(JSON.stringify({
      value: get('ov-video-value').textContent,
      detail: get('ov-video-detail').textContent,
      state: (cards.video || {}).dataset ? cards.video.dataset.state : null,
    }));
"""


def status(video):
    return {
        "server": {}, "hardware": [], "adapters": [], "clients": [],
        "datapath": {}, "video": video,
    }


def tile(video):
    return json.loads(run_node(BODY, {"RBGC_STATUS": json.dumps(status(video))}))


#: A snapshot shaped like the real one, taken from the reference Pi while a
#: source was connected and streaming to one viewer.
LIVE = {
    "mode": "external", "connected": True, "live": True, "stale": False,
    "source": "192.168.1.116:47810",
    "status": {"streaming": True, "clients": 1, "width": 640, "height": 480,
               "errors": []},
}


@needs_node
class TestItReportsWhatIsActuallyHappening:
    def test_a_streaming_source_is_live(self):
        """The reported bug: this said "Waiting"."""
        out = tile(LIVE)
        assert out["value"] == "Live"
        assert out["state"] == "good"

    def test_it_counts_the_viewers(self):
        assert tile(LIVE)["detail"] == "1 watching"

    def test_video_off_says_off(self):
        out = tile({"mode": "off", "connected": False, "live": False,
                    "status": {}})
        assert out["value"] == "Off"
        assert out["state"] == "idle"

    def test_no_source_yet_says_waiting(self):
        out = tile({"mode": "external", "connected": False, "live": False,
                    "status": {}})
        assert out["value"] == "Waiting"
        assert out["detail"] == "no source"

    def test_attached_but_not_streaming_is_its_own_state(self):
        """Not the same as nothing arriving: the source is there and something
        is wrong with what it is doing, which is where to look."""
        out = tile({"mode": "external", "connected": True, "live": True,
                    "stale": False, "status": {"streaming": False, "clients": 0}})
        assert out["value"] == "Connected"
        assert out["state"] == "warn"

    def test_a_source_that_has_gone_quiet_says_so(self):
        """`connected` is only "attached"; `live` adds "and still reporting".
        A source that stopped talking is exactly the case worth telling apart,
        and reading only `connected` would call it fine."""
        out = tile({"mode": "external", "connected": True, "live": False,
                    "stale": True, "status": {"streaming": True, "clients": 1}})
        assert out["value"] == "Connected"
        assert out["detail"] == "not reporting"

    def test_an_error_is_surfaced_rather_than_swallowed(self):
        out = tile({"mode": "external", "connected": False, "live": False,
                    "status": {"errors": ["Could not open the capture device"]}})
        assert out["state"] == "bad"
        assert "capture device" in out["detail"]

    def test_a_missing_video_block_does_not_throw(self):
        """A server built without video reports `null` here."""
        out = tile(None)
        assert out["value"] == "Off"


class TestTheFieldsItReadsActuallyExist:
    """**The half that would have caught the bug.**

    The tile's behaviour was self-consistent and wrong, because it read fields
    the status has never had. Pinning the *source* of each one is what makes
    that impossible to reintroduce -- and it is the same guard
    `test_web_glass.py` already carries for the Bluetooth tile's use of
    `status.hardware`.
    """

    def snapshot(self) -> dict:
        from server.video import MODE_EXTERNAL, VideoRegistry

        registry = VideoRegistry(mode=MODE_EXTERNAL)
        registry.attach_source_endpoint("192.168.1.116", 47810)
        registry.update_status_from_link(
            {"cfg_seq": 0, "media_port": 47810,
             "status": {"streaming": True, "clients": 1}})
        return registry.snapshot()

    def test_the_top_level_fields_are_there(self):
        snap = self.snapshot()
        for field in ("mode", "connected", "live", "stale", "status"):
            assert field in snap, f"the tile reads video.{field}, which is gone"

    def test_the_stream_fields_live_under_status(self):
        snap = self.snapshot()
        assert snap["status"]["streaming"] is True
        assert snap["status"]["clients"] == 1

    def test_source_is_still_a_string_not_an_object(self):
        """The precise shape of the original mistake: `video.source.available`
        on a string is `undefined`, so the tile's only path to "Live" was one
        that could never be taken."""
        assert isinstance(self.snapshot()["source"], str)

    def test_the_invented_fields_are_still_not_there(self):
        """If any of these ever appears, the old code would start *working* --
        and this test should be revisited deliberately rather than someone
        reinstating a read that happens to have become valid."""
        snap = self.snapshot()
        for invented in ("streaming", "clients", "error"):
            assert invented not in snap, (
                f"video.{invented} exists now; the tile's fields want rechecking"
            )

    def test_the_tile_no_longer_reads_them(self):
        source = SUMMARY_JS.read_text(encoding="utf-8")
        block = source.split("const video = status.video", 1)[1]
        block = block.split("setSummary('video'", 1)[0]
        assert "video.streaming" not in block
        assert "video.clients" not in block
        assert "video.source" not in block
