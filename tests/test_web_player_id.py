"""The web GUI's player-identification controls and developer view.

Two halves, and the second is why this file exists.

The **controls** must mirror what the video server's own window offers, because
in embedded mode there is no such window: the video server is a headless
subprocess of the Bluetooth server, and this page is the only place its
operator can reach any of it. A setting the desktop app has and this page does
not is, in that mode, a setting nobody can change.

The **rendering** is pure functions exercised in Node, the same way
``test_web_region_dnd.py`` covers the region chips -- the one real decision in
each is worth checking rather than eyeballing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "server" / "web" / "static" / "index.html"
APP_JS = ROOT / "server" / "web" / "static" / "app.js"
VIDEO_JS = ROOT / "server" / "web" / "static" / "js" / "sections" / "video.js"

#: Every player-identification field a source actually honours.
#:
#: `player_id_enabled` is the operator asking; the rest shape what it then
#: does. All of them are pushed to the source, so all of them need a control
#: somewhere -- and for an embedded source, "somewhere" is only this page.
SETTINGS = (
    "player_id_enabled",
    "player_id_backend",
    "player_id_confidence",
    "player_id_hz",
    "player_id_debug",
)


class TestTheControlsExist:
    def test_every_setting_has_an_input(self):
        markup = INDEX.read_text(encoding="utf-8")
        ids = {
            "player_id_enabled": "video-player-id",
            "player_id_backend": "video-player-id-backend",
            "player_id_confidence": "video-player-id-confidence",
            "player_id_hz": "video-player-id-hz",
            "player_id_debug": "video-player-id-debug",
        }
        missing = [name for name, el in ids.items() if f'id="{el}"' not in markup]

        assert not missing, f"no control for {missing}"

    def test_every_setting_is_posted(self):
        """A control that is never sent is a control that does nothing.

        This is the failure split-screen already had: every piece of the
        feature worked and there was no way to switch it on, because the field
        was missing from the form's fixed list.
        """
        script = APP_JS.read_text(encoding="utf-8")
        missing = [name for name in SETTINGS if f"'{name}'" not in script]

        assert not missing, f"never posted to /api/video/config: {missing}"

    def test_every_backend_offered_is_one_the_source_accepts(self):
        """An option the source rejects is a control that silently reverts."""
        from common.video import _PLAYER_ID_BACKENDS

        markup = INDEX.read_text(encoding="utf-8")
        select = markup.split('id="video-player-id-backend"')[1].split("</select>")[0]
        offered = {
            chunk.split('"')[0]
            for chunk in select.split('value="')[1:]
        }

        assert offered <= set(_PLAYER_ID_BACKENDS), (
            f"offered but not accepted: {offered - set(_PLAYER_ID_BACKENDS)}"
        )

    def test_every_backend_this_build_implements_is_offered(self):
        """The other direction, and the one that leaves a capability
        unreachable rather than merely reverting.

        Deliberately not "every accepted value": `_PLAYER_ID_BACKENDS` also
        carries `torch`, which nothing implements -- `_backend_class` knows
        heuristic, onnx and none. Offering it would be a control that can only
        report "not a backend this build knows about".
        """
        from videoserver.playervision.service import _backend_class

        markup = INDEX.read_text(encoding="utf-8")
        select = markup.split('id="video-player-id-backend"')[1].split("</select>")[0]

        for name in ("auto", "heuristic", "onnx"):
            assert _backend_class(name) is not None or name == "auto"
            assert f'value="{name}"' in select, f"{name} is implemented but not offered"


class TestTheDebugViewIsReachable:
    def test_the_overlay_table_and_breakdown_are_all_present(self):
        """In embedded mode this page is the only place they can appear."""
        markup = INDEX.read_text(encoding="utf-8")

        assert 'id="player-id-overlay"' in markup
        assert 'id="player-id-table"' in markup
        assert 'id="player-id-why"' in markup

    def test_the_registry_publishes_what_the_view_reads(self):
        """The other half of the Video-tile bug: a renderer reading fields the
        status has never carried cannot report anything but its fallback."""
        from common.video import VideoSettings
        from server.video import MODE_EXTERNAL, VideoRegistry

        registry = VideoRegistry(
            mode=MODE_EXTERNAL, settings=VideoSettings(), configured=True
        )
        snapshot = registry.snapshot()

        for key in ("player_tracks", "player_layout", "player_id_why"):
            assert key in snapshot, f"the view reads {key}; the status has no such key"


pytestmark_node = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node is not installed; this check is advisory and skips cleanly",
)

HARNESS = textwrap.dedent(
    """
    // video.js imports dom.js, which touches window at module scope. Node has
    // no such global; these are stub gaps, not faults.
    globalThis.addEventListener = () => {};
    globalThis.document = {
        querySelectorAll: () => [],
        addEventListener() {},
        getElementById: () => null,
        documentElement: { setAttribute() {}, removeAttribute() {} },
    };
    globalThis.localStorage = { getItem: () => null, setItem() {} };
    globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
    globalThis.window = globalThis;

    const mod = await import('file://' + process.env.RBGC_MODULE.replace(/\\\\/g, '/'));
    const input = JSON.parse(process.env.RBGC_INPUT);
    const svg = { innerHTML: '' };
    mod.drawPlayerOverlay(svg, input.tracks);
    console.log(JSON.stringify({
        svg: svg.innerHTML,
        tones: input.tracks.map((row) => mod.toneFor(row)),
        breakdown: mod.breakdownText(input.why),
    }));
    """
).strip()


def render(tracks=(), why=()):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS],
        capture_output=True, text=True, timeout=60,
        env={
            **os.environ,
            "RBGC_MODULE": str(VIDEO_JS),
            "RBGC_INPUT": json.dumps({"tracks": list(tracks), "why": list(why)}),
        },
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _track(t=1, p=0, c=0.0, s="none", r="", x=0.1, y=0.2, w=0.3, h=0.4):
    return {"t": t, "p": p, "c": c, "s": s, "r": r, "x": x, "y": y, "w": w, "h": h}


@pytestmark_node
class TestTheOverlay:
    def test_a_box_is_drawn_for_every_track_including_the_nameless(self):
        """The refused ones are the most useful thing on screen when the
        question is why nobody is being labelled."""
        out = render(tracks=[_track(t=1, p=2, c=0.9), _track(t=2)])

        assert out["svg"].count("<rect") == 2

    def test_the_three_tones_are_distinguishable(self):
        out = render(tracks=[
            _track(t=1, p=1, c=0.95),      # recognised
            _track(t=2, p=2, c=0.70),      # merely held
            _track(t=3),                   # refused
        ])

        assert out["tones"] == ["identified", "weak", "unidentified"]

    def test_a_box_against_the_top_edge_puts_its_label_below(self):
        """A label drawn above an entity at the top of the frame lands
        outside the picture."""
        high = render(tracks=[_track(y=0.0, h=0.2, p=1, c=0.9)])["svg"]
        low = render(tracks=[_track(y=0.5, h=0.2, p=1, c=0.9)])["svg"]

        def text_y(svg):
            return float(svg.split('<text')[1].split('y="')[1].split('"')[0])

        assert text_y(high) > 0
        assert text_y(low) < 500, "a label below its box would be inside it"

    def test_coordinates_are_clamped_to_the_picture(self):
        out = render(tracks=[_track(x=-0.5, y=-0.5, w=3.0, h=3.0)])

        for attr in ("x=", "y=", "width=", "height="):
            value = float(out["svg"].split(attr)[1].split('"')[1])
            assert 0 <= value <= 1000

    def test_a_name_cannot_inject_markup(self):
        """Region and source names cross the network from a machine the
        operator configured, and this renders with innerHTML."""
        out = render(tracks=[_track(p=1, c=0.9, r="<script>x</script>")])

        assert "<script>" not in out["svg"]


@pytestmark_node
class TestTheBreakdown:
    def test_the_winning_signal_is_marked_and_the_losers_kept(self):
        why = [{
            "track": 4, "player": 2, "confidence": 0.92, "source": "viewport",
            "region": "upper_left", "note": "",
            "scores": [
                {"signal": "viewport", "player": 2, "score": 0.92, "used": True, "note": ""},
                {"signal": "appearance", "player": 0, "score": 0.41, "used": False,
                 "note": "below the 0.60 publishing floor"},
            ],
        }]

        text = render(why=why)["breakdown"]

        assert "Player 2" in text and "->" in text
        assert "0.41" in text and "below the 0.60" in text

    def test_a_refusal_carries_its_reason(self):
        why = [{
            "track": 9, "player": 0, "confidence": 0.0, "source": "none",
            "region": "", "note": "no player map: nobody is playing yet",
            "scores": [],
        }]

        text = render(why=why)["breakdown"]

        assert "no player" in text and "no player map" in text

    def test_nothing_yet_says_so_rather_than_rendering_empty(self):
        assert render(why=[])["breakdown"].strip()
