"""The video panel's markup: the address fields, and the preview box.

Two ways to say the same thing, side by side, with nothing indicating which one
is in force. Selecting a detected server now fills the address fields and locks
them; choosing the blank entry hands them back.

Asserted against the shipped JavaScript because that is where the behaviour
lives -- there is no Python to exercise. Crude, but it catches the two ways
this regresses: the handler being dropped, and the fields never being unlocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "server" / "web" / "static"


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


@pytest.fixture(scope="module")
def index_html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


class TestSelectingADetectedServer:
    def test_the_dropdown_drives_the_address_fields(self, app_js):
        assert "$('video-found').addEventListener('change', applyDetectedSelection)" in app_js

    def test_choosing_one_locks_the_manual_fields(self, app_js):
        assert "host.disabled = !!chosen" in app_js
        assert "port.disabled = !!chosen" in app_js

    def test_the_fields_are_filled_from_the_selection(self, app_js):
        # lastIndexOf, not split(':'), or an IPv6 literal loses everything
        # after its first colon.
        assert "chosen.lastIndexOf(':')" in app_js

    def test_a_fresh_list_re_applies_the_rule(self, app_js):
        """Detect rebuilds the options; the lock must follow the new value."""
        body = app_js[app_js.index("function renderDetectedServers"):]
        body = body[: body.index("\n}") + 2]
        assert "applyDetectedSelection()" in body

    def test_the_blank_entry_says_what_it_does(self, app_js):
        assert "Enter an address manually" in app_js

    def test_there_is_somewhere_to_say_which_is_in_use(self, index_html, app_js):
        assert 'id="video-address-hint"' in index_html
        assert "video-address-hint" in app_js

    def test_a_disabled_field_still_carries_its_value(self, app_js):
        """The connect handler reads the inputs, so filling them is enough.

        A disabled input keeps its value and is still readable from script --
        which is why locking them needs no special case at connect time.
        """
        connect = app_js[app_js.index("action === 'video-connect'"):]
        connect = connect[: connect.index("} else")]
        assert "$('video-host').value" in connect
        assert "$('video-port').value" in connect


class TestThePreviewBox:
    """Two faults an operator meets before anything has even happened."""

    def test_the_image_starts_hidden(self, index_html):
        """Nothing runs on first load, so the markup has to be right itself.

        Neither startPreview nor stopPreview has fired when the page opens,
        so an <img> with no src rendered as a broken-link icon with its alt
        text sitting on top of the hint underneath it.
        """
        marker = index_html.index('id="video-preview-img"')
        start = index_html.rindex('<img', 0, marker)
        tag = index_html[start : index_html.index('>', marker) + 1]
        assert 'class="hidden"' in tag, (
            "the preview image is visible with no source"
        )

    def test_it_is_shown_only_when_a_frame_lands(self, app_js):
        assert "img.classList.remove('hidden')" in app_js

    def test_closing_the_panel_hides_it_again(self, app_js):
        stop = app_js[app_js.index('function stopPreview'):]
        stop = stop[: stop.index('async function fetchPreview')]
        assert "img.classList.add('hidden')" in stop

    def test_the_picture_fills_the_box(self):
        """max-width/max-height left a small preview marooned in black."""
        css = (STATIC / 'style.css').read_text(encoding='utf-8')
        block = css[css.index('.video-preview img'):]
        block = block[: block.index('}')]

        assert 'width: 100%' in block
        assert 'height: 100%' in block
        # contain, not cover: this is a monitoring picture, and cropping
        # would hide the edges of what the capture card is seeing.
        assert 'object-fit: contain' in block
