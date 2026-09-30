"""Every detection knob, reachable -- and on the machine that runs detection.

Asked for from the field: the split and identification methods expose their
settings in the video server app, and in the Bluetooth server's Video tab when
the capture card is on this machine. Three things make that more than a form:

* **The block cannot ride VIDEO_CONFIG.** That message was measured at 1010 of
  its 1200 bytes with four tickets, and these add about 290; it would be
  refused whole the moment a fourth player joined. So it is `DetectionTuning`,
  on DETECT_TUNING, and only to an embedded source.
* **The split detector's own settings become the capture machine's**, so its
  window can edit them without a Bluetooth server reverting them.
* **Every control posts, and every field has a control** -- checked against the
  dataclasses rather than a copied list, because a list copied into a test
  drifts exactly as the code does.
"""

from __future__ import annotations

import json
import shutil
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from common.protocol import MAX_DATAGRAM, ControlOp, encode_control
from common.video import DetectionTuning, VideoSettings

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "server" / "web" / "static" / "index.html"
APP_JS = ROOT / "server" / "web" / "static" / "app.js"
VIDEO_JS = ROOT / "server" / "web" / "static" / "js" / "sections" / "video.js"

node_required = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _node(script: str) -> dict:
    harness = textwrap.dedent(
        """
        globalThis.addEventListener = () => {};
        globalThis.document = {
            querySelectorAll: () => [], addEventListener() {},
            getElementById: () => null,
            documentElement: { setAttribute() {}, removeAttribute() {} },
        };
        globalThis.localStorage = { getItem: () => null, setItem() {} };
        globalThis.matchMedia = () => ({ matches: false, addEventListener() {} });
        globalThis.window = globalThis;
        const mod = await import('file://' + process.env.RBGC_MODULE.replace(/\\\\/g, '/'));
        const input = JSON.parse(process.env.RBGC_INPUT || '{}');
        """
    ) + script
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "RBGC_MODULE": str(VIDEO_JS),
             "RBGC_INPUT": json.dumps(_node.input)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


_node.input = {}


def _tables() -> dict:
    return _node(
        "console.log(JSON.stringify({tuning: mod.TUNING_FIELDS, split: mod.SPLIT_FIELDS,"
        " tuningDefaults: mod.TUNING_DEFAULTS, splitDefaults: mod.SPLIT_DEFAULTS}));"
    )


def _card() -> str:
    markup = INDEX.read_text(encoding="utf-8")
    return markup.split('id="video-config-card"')[1].split("</form>")[0]


def _controllers() -> str:
    markup = INDEX.read_text(encoding="utf-8")
    section = markup.split('<section class="view" data-view="controllers"')[1]
    return section.split('<section class="view"')[0]


def _apply_literal() -> str:
    script = APP_JS.read_text(encoding="utf-8")
    form = script.split("$('video-config-form').addEventListener")[1]
    return form.split("});\n});")[0]


# -- the block itself -------------------------------------------------------------


class TestTheBlock:
    def test_it_round_trips(self):
        tuning = DetectionTuning(pid_anchor_y=0.8, split_hold_auto=False)
        assert DetectionTuning.from_dict(tuning.to_dict()).clamped() == tuning

    def test_clamped_names_every_field(self):
        """A field forgotten in `clamped` is dropped on every load and every
        push, silently and permanently -- the trap `VideoSettings` guards."""
        odd = DetectionTuning(**{
            name: (False if isinstance(getattr(DetectionTuning(), name), bool) else 0.5)
            for name in DetectionTuning.__dataclass_fields__
        })
        clamped = odd.clamped()
        for name in DetectionTuning.__dataclass_fields__:
            assert hasattr(clamped, name)

    @pytest.mark.parametrize("raw", [None, "junk", {"split_hold": "x"},
                                     {"pid_anchor_x": float("nan")},
                                     {"split_edge_delta": 10_000}])
    def test_rubbish_is_made_safe(self, raw):
        tuning = DetectionTuning.from_dict(raw).clamped()
        assert 0.05 <= tuning.split_hold <= 0.95
        assert 0.0 <= tuning.pid_anchor_x <= 1.0
        assert 4 <= tuning.split_edge_delta <= 96

    def test_it_is_not_part_of_video_settings(self):
        """VIDEO_CONFIG carries every VideoSettings field and has no room."""
        assert not set(DetectionTuning.__dataclass_fields__) & set(
            VideoSettings.__dataclass_fields__
        )

    def test_its_own_message_fits_with_room_to_spare(self):
        body = encode_control(
            4294967295, ControlOp.DETECT_TUNING,
            {"tuning": DetectionTuning().to_dict(), "reset_learning": True},
        )
        assert MAX_DATAGRAM - len(body) > 400, f"{len(body)} bytes"

    def test_video_config_did_not_grow(self):
        """The measurement that decided all this, pinned: VideoSettings with
        four tickets and an ordinary password must still leave its headroom."""
        import secrets

        body = encode_control(4294967295, ControlOp.VIDEO_CONFIG, {
            "cfg_seq": 99999, "broker": "rbgc-broker.example.net:47900",
            "room": "ABCD1234", "preview_wanted": True,
            "tickets": [secrets.token_urlsafe(24) for _ in range(4)],
            "config": VideoSettings().clamped().to_dict(),
            "viewer_password": "a-typical-pass16",
        })
        assert MAX_DATAGRAM - len(body) > 150, f"{len(body)} bytes"


class TestWhoOwnsWhat:
    def test_the_split_detectors_measuring_settings_are_the_capture_machines(self):
        from server.video import SOURCE_OWNED_FIELDS

        for name in ("split_detect_hz", "split_detect_width", "split_detect_confidence",
                     "split_detect_activate", "split_detect_deactivate",
                     "split_detect_tolerance"):
            assert name in SOURCE_OWNED_FIELDS

    def test_what_decides_each_players_crop_stays_ours(self):
        from server.video import SOURCE_OWNED_FIELDS

        for name in ("split_detect_enabled", "split_override", "split_crop_bars",
                     "player_id_enabled", "player_id_debug"):
            assert name not in SOURCE_OWNED_FIELDS


# -- the web page -------------------------------------------------------------------


@node_required
class TestTheWebCard:
    def test_every_tuning_field_has_a_control_in_the_capture_card(self):
        tables = _tables()
        assert set(tables["tuning"]) == set(DetectionTuning.__dataclass_fields__)
        card = _card()
        for key, (element, _kind) in tables["tuning"].items():
            assert f'id="{element}"' in card, f"{key} has no control"

    def test_every_split_setting_this_machine_owns_is_in_the_card(self):
        from server.video import SOURCE_OWNED_FIELDS

        tables = _tables()
        owned = {name for name in SOURCE_OWNED_FIELDS if name.startswith("split_")}
        assert set(tables["split"]) == owned
        card = _card()
        for key, element in tables["split"].items():
            assert f'id="{element}"' in card, f"{key} has no control"

    def test_none_of_it_is_on_the_controllers_page(self):
        """In external mode it belongs to the capture machine; there the card
        that holds it is hidden, and the Controllers page is always shown."""
        tables = _tables()
        controllers = _controllers()
        elements = [e for e, _ in tables["tuning"].values()] + list(tables["split"].values())
        for element in elements:
            assert f'id="{element}"' not in controllers, element

    def test_every_split_setting_is_posted_by_apply(self):
        literal = _apply_literal()
        for key in _tables()["split"]:
            assert f"{key}:" in literal, f"{key} is in the card but not posted"

    def test_the_tuning_rides_the_same_apply(self):
        assert "tuning: collectTuning()" in _apply_literal()

    def test_restore_defaults_matches_the_dataclasses(self):
        """A drifted copy would 'restore' something nobody chose."""
        tables = _tables()
        assert tables["tuningDefaults"] == pytest.approx(DetectionTuning().to_dict())
        defaults = VideoSettings()
        assert tables["splitDefaults"] == pytest.approx(
            {key: getattr(defaults, key) for key in tables["split"]}
        )

    def test_the_bluetooth_servers_switches_stay_on_the_controllers_page(self):
        controllers = _controllers()
        for element in ("video-split-detect", "video-split-override", "video-split-crop-bars"):
            assert f'id="{element}"' in controllers


@node_required
class TestTheReadouts:
    def _learned(self, video, what, settings=None):
        _node.input = {"video": video, "what": what, "settings": settings or {}}
        try:
            return _node(
                "console.log(JSON.stringify({text: mod.learnedText(input.video, input.what, input.settings)}));"
            )["text"]
        finally:
            _node.input = {}

    def _model(self, model):
        _node.input = {"model": model}
        try:
            return _node("console.log(JSON.stringify({text: mod.modelText(input.model)}));")["text"]
        finally:
            _node.input = {}

    def test_learning_is_an_ordinary_state(self):
        text = self._learned({"learned": {"split": {"hold": None, "seam_samples": 7}}}, "hold")
        assert "Learning" in text and "7 of 30" in text

    def test_a_learned_value_and_the_one_in_force(self):
        text = self._learned(
            {"learned": {"split": {"hold": 0.31, "hold_in_force": 0.31}}}, "hold"
        )
        assert "0.31" in text

    def test_the_leave_delay_is_also_said_in_seconds(self):
        text = self._learned(
            {"learned": {"split": {"leave_in_force": 12, "longest_dip": 6}}},
            "leave", {"split_detect_hz": 2},
        )
        assert "12 checks" in text and "6.0 s" in text

    def test_learned_anchors_are_listed(self):
        text = self._learned(
            {"learned": {"identity": {"anchors": {"upper_left": [0.5, 0.78]}}}}, "anchor"
        )
        assert "upper left 0.50, 0.78" in text

    def test_silence_when_nothing_has_been_said(self):
        for what in ("hold", "leave", "anchor", "score"):
            assert self._learned({}, what) == ""

    def test_the_model_line_offers_the_download(self):
        text = self._model({"runtime": True, "detector": False,
                            "download_bytes": 34_183_233, "directory": "/x"})
        assert "Download model" in text and "34 MB" in text

    def test_a_missing_runtime_is_named(self):
        assert "onnxruntime" in self._model({"runtime": False})

    def test_progress_is_shown_while_downloading(self):
        text = self._model({"runtime": True, "download": {
            "running": True, "done": 50, "total": 100, "what": "YOLOX-Tiny detector"}})
        assert "50%" in text


# -- the server's handlers ----------------------------------------------------------


class _FakeLink:
    def __init__(self):
        self.pushes = []

    def request_tuning_push(self, *, reset_learning=False):
        self.pushes.append(reset_learning)

    def request_config_push(self):
        pass

    def snapshot(self):
        return {}


@pytest.fixture
async def embedded():
    from aiohttp.test_utils import TestClient, TestServer

    from server import config as server_config
    from server.datapath import Datapath
    from server.router import Router
    from server.sessions import SessionManager
    from server.video import MODE_EMBEDDED, VideoRegistry
    from server.web.app import create_app

    cfg = server_config.ServerConfig(
        password="client-password", admin_password="tuning-admin-password",
        tls_enabled=False, video_mode=MODE_EMBEDDED,
    )
    router = Router()
    sessions = SessionManager(cfg.password, auto_approve=True)
    registry = VideoRegistry(mode=MODE_EMBEDDED)
    datapath = Datapath(sessions, router, bind_host="127.0.0.1", bind_port=0,
                        realtime=False, video_registry=registry)
    app = create_app(cfg, sessions, router, datapath, video_registry=registry)
    client = TestClient(TestServer(app))
    await client.start_server()
    state = client.app["state"]
    state.video_link = _FakeLink()
    state._persist_calls = 0
    response = await client.post("/api/login", json={"password": "tuning-admin-password"})
    assert response.status == 200
    yield client, state
    await client.close()


class TestTheHandlers:
    async def test_apply_saves_the_tuning_and_pushes_it(self, embedded, monkeypatch):
        import server.web.app as web_app

        monkeypatch.setattr(web_app, "_persist", lambda state: None)
        client, state = embedded
        response = await client.post("/api/video/config", json={
            "tuning": {"pid_anchor_y": 0.8, "split_hold_auto": False},
        })
        assert response.status == 200
        assert state.config.video_tuning["pid_anchor_y"] == pytest.approx(0.8)
        assert state.config.video_tuning["split_hold_auto"] is False
        assert state.video_link.pushes == [False]

    async def test_it_is_merged_not_replaced(self, embedded, monkeypatch):
        """Posting one field must not reset the rest to defaults."""
        import server.web.app as web_app

        monkeypatch.setattr(web_app, "_persist", lambda state: None)
        client, state = embedded
        await client.post("/api/video/config", json={"tuning": {"pid_edge_margin": 0.2}})
        await client.post("/api/video/config", json={"tuning": {"pid_anchor_x": 0.3}})
        assert state.config.video_tuning["pid_edge_margin"] == pytest.approx(0.2)
        assert state.config.video_tuning["pid_anchor_x"] == pytest.approx(0.3)

    async def test_external_mode_does_not_take_it(self, embedded, monkeypatch):
        """In external mode the capture machine owns it; pushing ours would
        revert what its operator set."""
        import server.web.app as web_app
        from server.video import MODE_EXTERNAL

        monkeypatch.setattr(web_app, "_persist", lambda state: None)
        client, state = embedded
        state.video.mode = MODE_EXTERNAL
        await client.post("/api/video/config", json={"tuning": {"pid_anchor_y": 0.9}})
        assert state.config.video_tuning == {}
        assert state.video_link.pushes == []

    async def test_reset_learning_rides_the_next_push(self, embedded):
        client, state = embedded
        response = await client.post("/api/video/tuning/reset", json={})
        assert response.status == 200
        assert state.video_link.pushes == [True]

    async def test_the_status_carries_tuning_and_the_model_folder(self, embedded):
        _client, state = embedded
        video = state.build_status()["video"]
        assert video["tuning"] == DetectionTuning().to_dict()
        assert "detector" in video["model"] and "download" in video["model"]

    async def test_downloading_is_refused_outside_embedded_mode(self, embedded):
        from server.video import MODE_EXTERNAL

        client, state = embedded
        state.video.mode = MODE_EXTERNAL
        response = await client.post("/api/video/player-model/download", json={})
        assert response.status == 409


# -- the wire, both ends -------------------------------------------------------------


class _Transport:
    def __init__(self):
        self.sent = []

    def queue_control(self, op, body):
        self.sent.append(("reliable", op, body))

    def queue_control_replacing(self, op, body):
        self.sent.append(("replacing", op, body))


def _link(mode: str, tuning: dict | None = None):
    from server.videolink import VideoLink

    class Config:
        video_mode = mode
        video_tuning = tuning or {}
        video_port = 47830
        video_host = "192.168.1.116"
        video_password = "x" * 8
        video_embedded_password = "y" * 8
        server_name = "t"
        password = "p" * 8

    link = VideoLink(registry=None, datapath=None, config=Config())
    link.connected = True
    link._transport = _Transport()
    return link


class TestTheLinkPushesOnlyToItsOwnSubprocess:
    def test_embedded_pushes_the_operators_tuning(self):
        link = _link("embedded", {"pid_anchor_y": 0.8})
        link.request_tuning_push()
        [(_kind, op, body)] = link._transport.sent
        assert op == ControlOp.DETECT_TUNING
        assert body["tuning"]["pid_anchor_y"] == pytest.approx(0.8)

    def test_external_pushes_nothing(self):
        link = _link("external", {"pid_anchor_y": 0.8})
        link.request_tuning_push()
        link._push_tuning(link._transport, force=True)
        assert link._transport.sent == []

    def test_a_reset_is_sent_once_and_reliably(self):
        link = _link("embedded")
        link.request_tuning_push(reset_learning=True)
        link.request_tuning_push()
        kinds = [(kind, body.get("reset_learning", False)) for kind, _op, body in link._transport.sent]
        assert kinds == [("reliable", True), ("replacing", False)]

    def test_learned_values_reach_the_registry(self):
        from server.video import VideoRegistry

        registry = VideoRegistry(mode="embedded")
        link = _link("embedded")
        link._registry = registry
        link._on_control({"op": ControlOp.DETECT_LEARNED, "split": {"hold": 0.3},
                          "identity": "not a dict"})
        learned = registry.snapshot()["learned"]
        assert learned == {"split": {"hold": 0.3}}


class TestTheSourceSide:
    def _responder(self):
        from videoserver.control import ControlResponder

        applied = []

        class App:
            tuning = DetectionTuning()

            def apply_tuning(self, tuning):
                applied.append(tuning)
                App.tuning = tuning

            def reset_learning(self):
                applied.append("reset")

            class net:
                @staticmethod
                def control_session():
                    return None

        responder = ControlResponder.__new__(ControlResponder)
        responder._app = App()
        responder._tuning_peer = None
        responder._last_learned_ns = 0
        return responder, applied

    def test_it_applies_only_what_differs(self):
        """It arrives every few seconds; re-applying rebuilds the detector and
        throws its averaging away."""
        responder, applied = self._responder()

        class Session:
            client_id = "bt"

        body = {"op": ControlOp.DETECT_TUNING, "tuning": {"pid_anchor_y": 0.8}}
        responder._apply_tuning(Session(), body)
        responder._apply_tuning(Session(), body)
        assert len([a for a in applied if a != "reset"]) == 1

    def test_a_reset_is_honoured(self):
        responder, applied = self._responder()

        class Session:
            client_id = "bt"

        responder._apply_tuning(Session(), {"tuning": {}, "reset_learning": True})
        assert "reset" in applied

    def test_what_it_learned_fits_whatever_it_learned(self):
        """Anchors are per region, and there are eight region names in all."""
        from videoserver.control import encode_learned

        regions = ["upper_left", "upper_right", "lower_left", "lower_right",
                   "upper", "lower", "left", "right"]
        worst = {
            "split": {"hold": 0.3123, "leave": 40, "seam_samples": 240,
                      "noise_samples": 240, "longest_dip": 99,
                      "hold_in_force": 0.31, "leave_in_force": 40},
            "identity": {"anchors": {r: [0.523, 0.812] for r in regions},
                         "anchor_samples": {r: 240 for r in regions},
                         "score_floor": 0.123, "score_samples": 240,
                         "appearance_floor": 0.873, "impostor_samples": 240,
                         "score_floor_in_force": 0.123},
        }
        body = encode_control(4294967295, ControlOp.DETECT_LEARNED, encode_learned(worst))
        assert MAX_DATAGRAM - len(body) > 400, f"{len(body)} bytes"


class TestPersistence:
    def test_the_video_server_keeps_its_tuning(self, tmp_path):
        from videoserver.config import VideoServerConfig, load, save

        config = VideoServerConfig(password="abcdef")
        config.tuning = DetectionTuning(pid_anchor_x=0.4, split_leave_auto=False)
        save(config, tmp_path / "video.json")
        back = load(tmp_path / "video.json")
        assert back.tuning == config.tuning

    def test_the_bluetooth_server_keeps_the_embedded_tuning(self, tmp_path):
        from server import config as server_config

        config = server_config.ServerConfig(password="client-password")
        config.video_tuning = {"pid_anchor_x": 0.4}
        server_config.save(config, tmp_path / "server.json")
        back = server_config.load(tmp_path / "server.json")
        assert back.video_tuning == {"pid_anchor_x": 0.4}

    def test_the_embedded_child_starts_with_it(self, monkeypatch):
        """Not defaults until the first push a few seconds later."""
        import io as _io
        import sys

        from videoserver import main as video_main
        from videoserver.config import VideoServerConfig

        document = json.dumps({"mode": "embedded", "settings": {},
                               "tuning": {"pid_anchor_y": 0.85}})
        monkeypatch.setattr(sys, "stdin", _io.StringIO(document))
        config = VideoServerConfig(password="abcdef")
        video_main._read_stdin_settings(config)
        assert config.tuning.pid_anchor_y == pytest.approx(0.85)
