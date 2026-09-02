"""Every package in the tree is declared for installation.

`setuptools` takes `[tool.setuptools] packages` literally: a subpackage that
is not listed is silently absent from an installed copy, and the failure lands
as an ImportError on somebody else's machine rather than as anything visible
here. Found the hard way -- `server.bt.ble` had never been listed, so a
`pip install`ed server had no BLE transport at all while the source tree ran
perfectly.
"""

from __future__ import annotations

import pathlib
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Not shipped: the test suite, build output, and the packaging scripts, which
#: are run from a checkout rather than imported from an install.
NOT_SHIPPED = {"tests", "build", "dist", "packaging"}


def _declared() -> set[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["tool"]["setuptools"]["packages"])


def _on_disk() -> set[str]:
    found = set()
    for init in ROOT.rglob("__init__.py"):
        parts = init.parent.relative_to(ROOT).parts
        if not parts or parts[0] in NOT_SHIPPED or parts[0].startswith("."):
            continue
        found.add(".".join(parts))
    return found


def test_every_package_is_declared():
    missing = sorted(_on_disk() - _declared())
    assert not missing, (
        f"not in pyproject's packages list, so absent from an install: {missing}"
    )


def test_nothing_declared_has_gone_away():
    """A renamed package left behind here fails the build, not a test."""
    stale = sorted(_declared() - _on_disk())
    assert not stale, f"declared but not on disk: {stale}"


def test_the_qt_stylesheet_assets_are_package_data():
    """Listing the package is not enough -- the SVGs are data, not modules."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["qtui.assets"]
    assert any(p.endswith(".svg") for p in patterns)
