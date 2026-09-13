"""The Video view shows only what the chosen source actually has settings for.

A card that cannot apply to the selected mode is not merely noise: it invites
the operator to configure something that will not take. Three modes want three
different pages, and the split follows who owns what.

The expensive one is the preview. `startPreview` refuses while another *view*
is showing, but knows nothing about the mode -- so hiding the card without
stopping the poll leaves a 10 Hz request running against a picture nobody can
see. That request is the one thing on this page that costs the **datapath
thread** real work: slices are decoded and reassembled on a thread with a
sub-millisecond budget.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.webjs import needs_node, run_node

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"
VIDEO_JS = STATIC / "js" / "sections" / "video.js"
INDEX = STATIC / "index.html"

CARDS = ("video-connection", "video-preview-card", "video-config-card")

BODY = """
    const MODE = process.env.RBGC_MODE;
    const CARDS = JSON.parse(process.env.RBGC_CARDS);

    const hidden = {};
    const nodes = {};
    for (const id of CARDS) {
      nodes[id] = {
        classList: {
          toggle(cls, on) { if (cls === 'hidden') hidden[id] = !!on; },
          add(cls) { if (cls === 'hidden') hidden[id] = true; },
          remove(cls) { if (cls === 'hidden') hidden[id] = false; },
          contains() { return false; },
        },
      };
    }
    globalThis.document.getElementById = (id) => nodes[id] || null;

    const mod = await import(BASE + '/js/sections/video.js');
    mod.renderVideoVisibility({ mode: MODE });
    console.log(JSON.stringify(hidden));
"""


def hidden_for(mode: str) -> dict:
    return json.loads(run_node(BODY, {
        "RBGC_MODE": mode,
        "RBGC_CARDS": json.dumps(list(CARDS)),
    }))


@needs_node
class TestEachModeShowsItsOwnSettings:
    def test_off_shows_nothing(self):
        out = hidden_for("off")
        assert out == {c: True for c in CARDS}

    def test_external_shows_the_connection_and_the_preview(self):
        """Not the capture settings: the video server is somebody else's
        machine and owns how the picture is made. Pushing ours over the top is
        what reverted the operator's choices the moment this server
        connected."""
        out = hidden_for("external")
        assert out["video-connection"] is False
        assert out["video-preview-card"] is False
        assert out["video-config-card"] is True

    def test_embedded_shows_the_capture_settings_and_the_preview(self):
        """And not the connection fields: the source is this machine's own
        subprocess, so there is nothing to point at and the password is
        generated for it."""
        out = hidden_for("embedded")
        assert out["video-connection"] is True
        assert out["video-preview-card"] is False
        assert out["video-config-card"] is False


#: Drives the real `startPreview` / `renderVideoVisibility` pair.
#:
#: `startPreview` refuses unless the Video view is showing, so the view has to
#: be genuinely switched -- asserting on a poll that was never started is the
#: vacuous version of this test, and the reason `test_the_poll_really_starts`
#: exists beside it.
POLL_BODY = """
    const IDS = ['video-connection','video-preview-card','video-config-card',
                 'video-preview-img','video-preview-toggle','video-preview-hint'];
    const nop = () => {};
    const nodes = {};
    for (const id of IDS) {
      nodes[id] = {
        dataset: {}, style: {}, textContent: '',
        classList: { add: nop, remove: nop, toggle: nop, contains: () => false },
        getAttribute: () => null, removeAttribute: nop, setAttribute: nop,
      };
    }

    // Enough of a document for showView('video') to take.
    const section = { dataset: { view: 'video' },
      classList: { add: nop, remove: nop, toggle: nop, contains: () => false } };
    const rail = { querySelectorAll: () => [] };
    globalThis.document = {
      documentElement: { setAttribute: nop, removeAttribute: nop },
      getElementById: (id) => (id === 'rail' ? rail : nodes[id] || null),
      querySelector: (sel) => (sel.includes('"video"') ? section : null),
      querySelectorAll: () => [section],
      addEventListener: nop, dispatchEvent: nop,
    };
    globalThis.addEventListener = nop;
    globalThis.CustomEvent = function (t, i) { return { type: t, detail: (i||{}).detail }; };
    // Never settles: we are counting timers, not fetching anything.
    globalThis.fetch = () => new Promise(() => {});

    const nav = await import(BASE + '/js/nav.js');
    const video = await import(BASE + '/js/sections/video.js');

    nav.showView('video');
    video.startPreview();
    const started = video.previewRunning();

    video.renderVideoVisibility({ mode: process.env.RBGC_MODE });
    const after = video.previewRunning();

    video.stopPreview();
    console.log(JSON.stringify({ started, after }));
"""


@needs_node
class TestHidingThePreviewStopsThePoll:
    """**The most expensive mistake available in this change.**

    `startPreview` refuses while another *view* is showing but knows nothing
    about the mode, so hiding the card without stopping the poll leaves a 10 Hz
    request running against a picture nobody can see -- and every preview frame
    is reassembled on the datapath thread, which has a sub-millisecond budget.
    """

    def run(self, mode):
        return json.loads(run_node(POLL_BODY, {"RBGC_MODE": mode}))

    def test_the_poll_really_starts(self):
        """Otherwise everything below passes against a poll that was never
        running."""
        assert self.run("embedded")["started"] is True

    def test_off_stops_it(self):
        out = self.run("off")
        assert out["started"] is True
        assert out["after"] is False, (
            "the preview card was hidden with its 10 Hz poll still running"
        )

    def test_a_mode_that_keeps_the_card_leaves_it_running(self):
        """The other half: stopping it on every render would make the preview
        impossible to keep open, since the status feed calls this ten times a
        second."""
        for mode in ("external", "embedded"):
            assert self.run(mode)["after"] is True, mode

    def test_there_is_one_owner_for_the_connection_panel(self):
        """`renderVideoConnection` used to hide it too. Two rules deciding one
        thing is two rules that can disagree, and the loser is silent."""
        source = VIDEO_JS.read_text(encoding="utf-8")
        block = source.split("export function renderVideoConnection", 1)[1]
        block = block.split("\nexport ", 1)[0]
        assert "classList.add('hidden')" not in block


class TestTheSplitControlsSurviveAVideolessServer:
    def test_they_are_rendered_before_the_early_return(self):
        """`renderVideo` hides the whole section and returns when the server
        was built without video. The split controls are on the *Controllers*
        view, so they would sit at their markup defaults -- reading "off" for
        settings that are simply unknown, and posting to an endpoint that is
        not there."""
        source = VIDEO_JS.read_text(encoding="utf-8")
        body = source.split("export function renderVideo(video) {", 1)[1]
        before_return = body.split("if (!video)", 1)[0]
        assert "renderSplitControls(video)" in before_return

    def test_they_are_disabled_and_explained_rather_than_hidden(self):
        """An operator looking for the control should find it and be told why
        it cannot be used, not conclude the feature is missing."""
        source = VIDEO_JS.read_text(encoding="utf-8")
        block = source.split("export function renderSplitControls", 1)[1]
        block = block.split("\n/*", 1)[0]
        assert "disabled" in block
        assert "split-unavailable-hint" in block

    def test_the_hint_element_exists(self):
        assert 'id="split-unavailable-hint"' in INDEX.read_text(encoding="utf-8")


class TestThePreviewSettingsMovedWithThePicture:
    def test_they_are_in_the_preview_card(self):
        html = INDEX.read_text(encoding="utf-8")
        card = html.split('id="video-preview-card"', 1)[1]
        card = card.split('id="video-config-card"', 1)[0]
        assert 'id="video-preview-width"' in card
        assert 'id="video-preview-fps"' in card

    def test_the_capture_form_no_longer_claims_them(self):
        html = INDEX.read_text(encoding="utf-8")
        form = html.split('id="video-config-form"', 1)[1].split("</form>", 1)[0]
        assert 'id="video-preview-width"' not in form
        assert 'id="video-preview-fps"' not in form

    def test_every_control_in_the_capture_form_is_in_its_submit(self):
        """The literal is a fixed field list, and it fails both ways: a key
        left in it after its control moved is `$(id)` returning null, and the
        TypeError takes the whole handler -- so Apply silently stops saving
        everything."""
        html = INDEX.read_text(encoding="utf-8")
        form = html.split('id="video-config-form"', 1)[1].split("</form>", 1)[0]
        controls = set(re.findall(r'id="(video-[a-z-]+)"', form))

        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        submit = app_js.split("video-config-form", 1)[1].split("});", 1)[0]
        referenced = set(re.findall(r"\$\('(video-[a-z-]+)'\)", submit))

        assert referenced <= controls, (
            "the submit reads controls that are no longer in the form: "
            f"{sorted(referenced - controls)}"
        )
