"""One design, three applications -- the parts that were quietly two designs.

Stage 8 of the UI work. The palette was already shared; the *backdrop* was not,
and nobody could see that from either side alone.
"""

from __future__ import annotations

import re
from pathlib import Path

from common.design.themes import theme_names
from common.design.tokens import ORBS, VIGNETTE_ALPHA, VIGNETTE_START

ROOT = Path(__file__).resolve().parent.parent
TOKENS_CSS = ROOT / "server" / "web" / "static" / "tokens.css"
STYLE_CSS = ROOT / "server" / "web" / "static" / "style.css"
QT_BACKDROP = ROOT / "qtui" / "backdrop.py"


def test_the_backdrop_recipe_has_one_source() -> None:
    """The Qt painter must read the shared table, not keep its own.

    It kept its own: five orbs with per-orb alpha, a falloff knee and a
    vignette, against the web's four at full strength with none of that. Same
    tokens, same theme, visibly more saturated in the browser -- and the extra
    saturation is what pushed the web's card contrast down toward the AA floor.

    Neither file was wrong on its own, which is why this survived review.
    """
    src = QT_BACKDROP.read_text(encoding="utf-8")
    assert "from common.design.tokens import" in src
    assert "ORBS" in src
    # A local table would shadow the shared one and drift again.
    assert not re.search(r"^_ORBS: tuple", src, re.M), (
        "qtui/backdrop.py has re-declared its own orb table")


def test_every_theme_emits_a_backdrop() -> None:
    """The gradient stack is per theme, because each stop needs a themed alpha.

    CSS has no portable way to add alpha to a custom property -- `color-mix()`
    and relative colour syntax are both newer than this has to run in -- so the
    generator resolves each stop and emits the whole stack per scheme.
    """
    css = TOKENS_CSS.read_text(encoding="utf-8")
    assert css.count("--backdrop-image:") == len(theme_names())


def test_the_web_uses_the_generated_backdrop() -> None:
    """`style.css` must not carry a second, hand-written stack."""
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "var(--backdrop-image)" in css
    body = re.search(r"\nbody \{(.*?)\n\}", css, re.S)
    assert body, "no body rule"
    assert "radial-gradient" not in body.group(1), (
        "body has hand-written orbs again; they belong in the shared table")


def test_the_generated_stack_carries_every_orb() -> None:
    """Five orbs, a vignette and a base -- the same layers QPainter draws."""
    css = TOKENS_CSS.read_text(encoding="utf-8")
    first = css.split("--backdrop-image:", 1)[1].split(";", 1)[0]
    assert first.count("radial-gradient(") == len(ORBS)
    # Vignette first: CSS paints the first layer on top, QPainter the last.
    assert first.lstrip().startswith("linear-gradient(to bottom,")
    assert f"{VIGNETTE_START * 100:.0f}%" in first
    assert f"{VIGNETTE_ALPHA:.3f}" in first


def test_the_orb_radius_follows_the_longer_side() -> None:
    """QPainter sizes each orb against `max(width, height)`.

    Using `vw` alone would squash every orb into an ellipse on a wide window,
    which is exactly the kind of difference that reads as "the web one looks
    off" without anyone being able to say why.
    """
    css = TOKENS_CSS.read_text(encoding="utf-8")
    first = css.split("--backdrop-image:", 1)[1].split(";", 1)[0]
    assert "max(100vw, 100vh)" in first


def test_all_three_applications_share_the_theme_list() -> None:
    """A scheme offered in one application and missing from another is a bug.

    The two desktop applications read `theme_names()` directly; the browser
    cannot, so the generator emits one block per theme and this is where the
    two counts are held together.
    """
    css = TOKENS_CSS.read_text(encoding="utf-8")
    blocks = set(re.findall(r':root\[data-theme="([^"]+)"\]', css))
    # Every theme but the default, which *is* `:root`.
    assert blocks | {"amber"} == set(theme_names())
