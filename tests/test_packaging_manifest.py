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


class TestTheStylesheetAssetsAreBundled:
    """`qtui/assets/*.svg` is data, not code.

    The stylesheet loads them with `image: url(...)` at runtime, so an
    import-analysing bundler cannot see them -- and PyInstaller shipped a
    client without them the first time this theme was built. The bundle then
    starts and looks almost right: checked boxes are blank blue squares and
    combo boxes have no drop-down arrow, because Qt drops an `image:` rule it
    cannot load without saying anything.
    """

    def _spec(self, name: str) -> str:
        return (ROOT / "packaging" / name).read_text(encoding="utf-8")

    def test_the_client_spec_collects_them(self):
        assert '"qtui/assets"' in self._spec("client.spec")

    def test_the_videoserver_spec_collects_them(self):
        assert '"qtui/assets"' in self._spec("videoserver.spec")

    def test_the_appimage_build_collects_them(self):
        script = (ROOT / "packaging" / "linux" / "build-appimage.sh").read_text(
            encoding="utf-8"
        )
        assert "qtui/assets=qtui/assets" in script

    def test_every_asset_the_stylesheet_asks_for_exists(self):
        """The stylesheet and the generator must not drift apart."""
        import re

        from qtui import theme

        for url in re.findall(r"url\(([^)]+)\)", theme.stylesheet()):
            assert url, "an asset resolved to an empty url, so its rule is dead"
            assert pathlib.Path(url).is_file(), url


class TestTheVideoServerStandsAlone:
    """The video server must not import the client.

    It did, for one shared widget, which dragged the whole `client` package
    into its bundle. Worse in principle than in size: the two applications ship
    separately, and a dependency in that direction means the video server's
    build has to know about SDL2, gamepad mapping and the input loop.
    """

    def test_no_videoserver_module_imports_client(self):
        import ast

        offenders = []
        for path in (ROOT / "videoserver").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("client"):
                    offenders.append(f"{path.name}: from {node.module}")
                elif isinstance(node, ast.Import):
                    offenders += [
                        f"{path.name}: import {a.name}"
                        for a in node.names
                        if a.name.startswith("client")
                    ]
        assert not offenders, offenders


class TestQtSvgIsAvailableToBothApplications:
    """Every icon in the theme is rasterised through `QSvgRenderer`.

    Excluding QtSvg does not degrade the icons -- it stops the GUI starting:
    "Could not start the GUI (No module named 'PySide6.QtSvg')". The video
    server's spec excluded it, correctly, right up until it gained a theme.
    """

    def test_neither_spec_excludes_it(self):
        import re

        for name in ("client.spec", "videoserver.spec"):
            text = (ROOT / "packaging" / name).read_text(encoding="utf-8")
            # Only inside the excludes list; a mention in a comment is fine.
            block = re.search(r"excludes = \[(.*?)\]", text, re.S)
            assert block, name
            body = re.sub(r"#[^\n]*", "", block.group(1))
            assert "PySide6.QtSvg" not in body, f"{name} excludes QtSvg"


class TestEveryStylesheetThePageLoadsIsReachable:
    """The login screen renders before a session exists.

    A stylesheet linked from `index.html` but missing from the server's public
    allow-list is fetched, refused, and the sign-in page comes up unstyled --
    which looks like a broken server rather than a missing list entry.
    """

    def test_linked_sheets_are_public(self):
        import re

        html = (ROOT / "server" / "web" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        linked = set(re.findall(r'<link[^>]+href="(/[^"]+\.css)"', html))
        assert linked, "no stylesheets linked at all"

        source = (ROOT / "server" / "web" / "app.py").read_text(encoding="utf-8")
        block = re.search(r"PUBLIC_PATHS = frozenset\((.*?)\n\)", source, re.S)
        assert block
        public = set(re.findall(r'"(/[^"]*)"', block.group(1)))
        assert linked <= public, f"linked but not public: {sorted(linked - public)}"
