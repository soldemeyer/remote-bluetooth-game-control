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
    "player_id_confidence",
    "player_id_hz",
    "player_id_debug",
)


def _controllers_view(markup: str) -> str:
    """The Controllers *section*, not the first thing mentioning it.

    `data-view="controllers"` also appears on the header's summary tile and
    the rail button, both earlier in the page. Splitting on the bare
    attribute took the tile, so "not on the Controllers page" passed
    vacuously -- nothing was on a header button, whatever the layout.
    """
    section = markup.split('<section class="view" data-view="controllers"')[1]
    return section.split('<section class="view"')[0]


class TestTheControlsExist:
    def test_every_setting_has_an_input(self):
        markup = INDEX.read_text(encoding="utf-8")
        ids = {
            "player_id_enabled": "video-player-id",
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
        was missing from the form's fixed list. Two routes now, and each field
        must be on the one its control lives beside: the on/off switch applies
        on change from the Controllers page, and the four that describe how
        this machine runs identification ride the Capture and encoding card's
        Apply.
        """
        script = APP_JS.read_text(encoding="utf-8")
        form = script.split("$('video-config-form').addEventListener")[1]
        form = form.split("});\n});")[0]

        assert "'player_id_enabled'" in script, "the switch is never posted"
        missing = [
            name for name in SETTINGS
            if name != "player_id_enabled" and f"{name}:" not in form
        ]
        assert not missing, f"in the card but not in its Apply: {missing}"

    def test_the_four_live_in_the_capture_card_and_nowhere_else(self):
        """They describe work done on the capture machine, and the Capture
        and encoding card is the one that exists only when that is us. On the
        Controllers page they showed in every mode, which in external mode
        meant a control that reverted on the next status."""
        markup = INDEX.read_text(encoding="utf-8")
        card = markup.split('id="video-config-card"')[1].split("</form>")[0]
        controllers = _controllers_view(markup)

        for element in ("video-model-status", "video-player-id-confidence",
                        "video-player-id-hz", "video-player-id-debug"):
            assert f'id="{element}"' in card, f"{element} is not in the capture card"
            assert f'id="{element}"' not in controllers, (
                f"{element} is still on the Controllers page"
            )

    def test_the_switch_stays_on_the_controllers_page(self):
        """Asking for labels is the Bluetooth server's half, in every mode."""
        markup = INDEX.read_text(encoding="utf-8")
        controllers = _controllers_view(markup)

        assert 'id="video-player-id"' in controllers
        assert 'id="video-player-id-detail"' in controllers

    def test_there_is_no_backend_to_choose(self):
        """The no-model backend is gone; identification is the model. A
        dropdown with one real choice is a control that does nothing -- and
        one still offering `heuristic` would offer something that no longer
        exists."""
        markup = INDEX.read_text(encoding="utf-8")
        assert 'id="video-player-id-backend"' not in markup
        assert "heuristic" not in markup

    def test_the_model_can_be_downloaded_from_the_card(self):
        """This page is the only window an embedded source has, so it is the
        only place the model can be fetched from in that mode."""
        markup = INDEX.read_text(encoding="utf-8")
        card = markup.split('id="video-config-card"')[1].split("</form>")[0]
        assert 'data-action="video-model-download"' in card
        script = APP_JS.read_text(encoding="utf-8")
        assert "'/api/video/player-model/download'" in script


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
    const attrs = {};
    const svg = { innerHTML: '', setAttribute(name, value) { attrs[name] = value; } };
    mod.drawPlayerOverlay(svg, input.tracks, input.width, input.height);
    console.log(JSON.stringify({
        svg: svg.innerHTML,
        viewBox: attrs.viewBox || '',
        tones: input.tracks.map((row) => mod.toneFor(row)),
        breakdown: mod.breakdownText(input.why),
        detail: mod.playerIdDetail(input.video, input.settings),
    }));
    """
).strip()


def render(tracks=(), why=(), width=1280, height=720, video=None, settings=None):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS],
        # UTF-8 explicitly: the status line carries curly quotes and an em
        # dash, and `text=True` alone decodes as cp1252 on Windows -- which
        # has no 0x9d, so the reader thread died and stdout came back None.
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={
            **os.environ,
            "RBGC_MODULE": str(VIDEO_JS),
            "RBGC_INPUT": json.dumps({
                "tracks": list(tracks), "why": list(why),
                "width": width, "height": height,
                "video": video or {}, "settings": settings or {},
            }),
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
        out = render(tracks=[_track(x=-0.5, y=-0.5, w=3.0, h=3.0)],
                     width=1920, height=1080)

        limits = {"x=": 1920, "y=": 1080, "width=": 1920, "height=": 1080}
        for attr, limit in limits.items():
            value = float(out["svg"].split(attr)[1].split('"')[1])
            assert 0 <= value <= limit

    def test_the_overlay_is_in_the_source_s_own_pixels(self):
        """The viewBox is the source resolution, scaled `xMidYMid meet` --
        the same geometry as the preview's `object-fit: contain` -- so a box
        lands on the pixels it describes whatever shape the card is. A
        centred quarter-size box must come out as exactly that."""
        out = render(tracks=[_track(x=0.25, y=0.25, w=0.5, h=0.5, p=1, c=0.9)],
                     width=1280, height=720)

        assert out["viewBox"] == "0 0 1280 720"
        rect = out["svg"].split("<rect")[1]
        assert 'x="320.0"' in rect and 'y="180.0"' in rect
        assert 'width="640.0"' in rect and 'height="360.0"' in rect

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


@pytestmark_node
class TestTheStatusLineNamesTheRightFault:
    """The line under the switch, which was wrong in embedded mode.

    It blamed `playervision_allowed` whenever identification was asked for and
    not running. In embedded mode that consent is given by construction -- the
    child is launched with `--allow-player-id` -- so the sentence sent the
    operator to a setting that does not exist on the machine they were
    looking at. Reported with a screenshot taken in exactly that state.
    """

    def test_embedded_and_not_streaming_says_so(self):
        out = render(
            video={"mode": "embedded", "status": {"streaming": False}},
            settings={"player_id_enabled": True},
        )

        assert "not streaming" in out["detail"]
        assert "playervision_allowed" not in out["detail"]
        assert "Allow the Bluetooth server" not in out["detail"]

    def test_external_points_at_the_video_server_window(self):
        """Named by the checkbox's own words, not the config key: the
        operator reads a label, not a JSON file."""
        out = render(
            video={"mode": "external", "status": {"streaming": True}},
            settings={"player_id_enabled": True},
        )

        assert "Allow the Bluetooth server to identify players" in out["detail"]
        assert "playervision_allowed" not in out["detail"]

    def test_a_loading_worker_is_not_called_running(self):
        """Available and not yet running read as "Running" before `starting`
        existed, because an available backend always had been running by the
        time anybody asked."""
        out = render(
            video={"mode": "external", "status": {"streaming": True, "player_id": {
                "available": True, "starting": True, "backend": "onnx"}}},
            settings={"player_id_enabled": True},
        )

        assert out["detail"].startswith("Starting")
        assert "Running" not in out["detail"]

    def test_a_worker_that_could_not_start_says_why(self):
        out = render(
            video={"mode": "external", "status": {"streaming": True, "player_id": {
                "available": False, "backend": "onnx",
                "reason": "the worker exited before starting (code 2)"}}},
            settings={"player_id_enabled": True},
        )

        assert "code 2" in out["detail"]
        assert "Allow the Bluetooth server" not in out["detail"]

    def test_nothing_is_said_while_the_switch_is_off(self):
        out = render(video={"mode": "embedded", "status": {}},
                     settings={"player_id_enabled": False})

        assert out["detail"] == ""
