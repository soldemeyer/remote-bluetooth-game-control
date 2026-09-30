"""Fetching the models, and trusting what arrived.

No network here: every download goes through a fake opener serving bytes the
test chose, so the pinned-hash rule, the atomic install and the care taken
with somebody's own model are all checked without touching GitHub.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading

import pytest

from videoserver.playervision import models
from videoserver.playervision.models import DownloadError, ModelFile


def fake(filename: str, payload: bytes, **over) -> ModelFile:
    values = dict(
        filename=filename,
        title=f"test {filename}",
        url=f"https://example.invalid/{filename}",
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        licence="Apache-2.0",
        source="a test",
        sidecar={"output": "yolox"},
    )
    values.update(over)
    return ModelFile(**values)


class Server:
    """A fake ``urlopen``: serves whatever the test put at each URL."""

    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.requests: list[str] = []

    def __call__(self, url):
        self.requests.append(url)
        return io.BytesIO(self.contents[url])


DETECTOR = b"d" * 700_000
EMBEDDER = b"e" * 300_000


@pytest.fixture
def pair():
    files = (fake("detector.onnx", DETECTOR), fake("embedder.onnx", EMBEDDER,
                                                    sidecar={"size": 224}))
    server = Server({item.url: payload for item, payload in zip(files, (DETECTOR, EMBEDDER))})
    return files, server


class TestItInstalls:
    def test_both_files_land_with_their_sidecars_and_a_notice(self, tmp_path, pair):
        files, server = pair
        installed = models.download(tmp_path, files=files, opener=server)

        assert installed == ["detector.onnx", "embedder.onnx"]
        assert (tmp_path / "detector.onnx").read_bytes() == DETECTOR
        assert json.loads((tmp_path / "detector.json").read_text()) == {"output": "yolox"}
        assert json.loads((tmp_path / "embedder.json").read_text()) == {"size": 224}
        notice = (tmp_path / "NOTICE.txt").read_text()
        assert "Apache-2.0" in notice and files[0].sha256 in notice
        assert not list(tmp_path.glob("*.part"))

    def test_pressing_it_twice_downloads_nothing_the_second_time(self, tmp_path, pair):
        files, server = pair
        models.download(tmp_path, files=files, opener=server)
        server.requests.clear()
        assert models.download(tmp_path, files=files, opener=server) == []
        assert server.requests == []

    def test_progress_climbs_to_the_total(self, tmp_path, pair):
        files, server = pair
        seen: list[tuple[int, int]] = []
        models.download(
            tmp_path, files=files, opener=server,
            progress=lambda done, total, what: seen.append((done, total)),
        )
        dones = [done for done, _ in seen]
        assert dones == sorted(dones)
        assert seen[-1] == (len(DETECTOR) + len(EMBEDDER),) * 2


class TestItRefusesWhatItCannotTrust:
    def test_a_wrong_hash_is_not_installed(self, tmp_path):
        item = fake("detector.onnx", DETECTOR, sha256="0" * 64)
        with pytest.raises(DownloadError, match="SHA-256"):
            models.download(tmp_path, files=(item,), opener=Server({item.url: DETECTOR}))
        assert not (tmp_path / "detector.onnx").exists()
        assert not list(tmp_path.glob("*.part")), "the partial file was left behind"

    def test_a_short_file_is_not_installed(self, tmp_path):
        item = fake("detector.onnx", DETECTOR)
        with pytest.raises(DownloadError, match="expected"):
            models.download(
                tmp_path, files=(item,), opener=Server({item.url: DETECTOR[:1000]})
            )
        assert not (tmp_path / "detector.onnx").exists()

    def test_a_file_larger_than_promised_is_abandoned(self, tmp_path):
        item = fake("detector.onnx", DETECTOR)
        with pytest.raises(DownloadError, match="more data"):
            models.download(
                tmp_path, files=(item,), opener=Server({item.url: DETECTOR + b"x" * 400_000})
            )

    def test_a_network_failure_says_which_file(self, tmp_path):
        item = fake("detector.onnx", DETECTOR)

        def broken(url):
            raise OSError("connection reset")

        with pytest.raises(DownloadError, match="test detector.onnx: connection reset"):
            models.download(tmp_path, files=(item,), opener=broken)

    def test_cancel_stops_it_and_installs_nothing(self, tmp_path):
        item = fake("detector.onnx", DETECTOR)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(DownloadError, match="cancelled"):
            models.download(
                tmp_path, files=(item,), opener=Server({item.url: DETECTOR}), cancel=cancel
            )
        assert not (tmp_path / "detector.onnx").exists()

    def test_a_file_that_finished_stays_when_a_later_one_fails(self, tmp_path, pair):
        """Each was verified on its own."""
        files, server = pair
        server.contents[files[1].url] = b"tampered"
        with pytest.raises(DownloadError):
            models.download(tmp_path, files=files, opener=server)
        assert (tmp_path / "detector.onnx").read_bytes() == DETECTOR


class TestTheOperatorsOwnModelIsKept:
    def test_a_different_detector_is_moved_aside_not_overwritten(self, tmp_path, pair):
        files, server = pair
        (tmp_path / "detector.onnx").write_bytes(b"my own fine-tuned model")
        (tmp_path / "detector.json").write_text('{"output": "yolo"}')

        models.download(tmp_path, files=files, opener=server)

        assert (tmp_path / "detector.onnx.previous").read_bytes() == b"my own fine-tuned model"
        assert (tmp_path / "detector.json.previous").read_text() == '{"output": "yolo"}'
        assert (tmp_path / "detector.onnx").read_bytes() == DETECTOR

    def test_a_second_set_aside_does_not_clobber_the_first(self, tmp_path, pair):
        files, server = pair
        (tmp_path / "detector.onnx").write_bytes(b"first")
        (tmp_path / "detector.onnx.previous").write_bytes(b"older still")
        models.download(tmp_path, files=files, opener=server)
        assert (tmp_path / "detector.onnx.previous").read_bytes() == b"older still"
        assert (tmp_path / "detector.onnx.previous2").read_bytes() == b"first"


class TestStatus:
    def test_an_empty_folder_needs_the_download(self, tmp_path):
        report = models.status(tmp_path)
        assert report["detector"] is False and report["complete"] is False
        assert report["download_bytes"] == models.total_size()

    def test_it_tells_ours_from_somebody_elses(self, tmp_path, pair):
        files, server = pair
        models.download(tmp_path, files=files, opener=server)
        (tmp_path / "embedder.onnx").write_bytes(b"a different embedder")
        entries = {e["file"]: e for e in models.status(tmp_path, files=files)["files"]}
        assert entries["detector.onnx"]["ours"] is True
        assert entries["embedder.onnx"]["present"] is True
        assert entries["embedder.onnx"]["ours"] is False

    def test_a_missing_folder_does_not_raise(self, tmp_path):
        assert models.status(tmp_path / "nope")["complete"] is False


class TestTheManifest:
    """The real entries. Checked for shape here; the files themselves were
    verified against YOLOX's own demo photograph when they were pinned."""

    @pytest.mark.parametrize("item", models.MODELS, ids=lambda item: item.filename)
    def test_every_entry_is_pinned_and_licensed(self, item):
        assert item.url.startswith("https://")
        assert re.fullmatch(r"[0-9a-f]{64}", item.sha256)
        assert item.size > 1_000_000
        assert item.licence == "Apache-2.0"
        assert item.source

    def test_the_detector_declares_how_it_wants_its_pixels(self):
        """Fed 0..1, YOLOX-Tiny finds nothing at all -- measured. The sidecar
        is the only thing standing between the download and that."""
        detector = next(i for i in models.MODELS if i.filename == "detector.onnx")
        assert detector.sidecar["output"] == "yolox"
        assert detector.sidecar["input_range"] == "0-255"

    def test_the_filenames_are_the_ones_the_backend_loads(self):
        """And nothing it does not. An ImageNet embedder downloaded and never
        loaded would be 14 MB of nothing; see `playervision.signature` for why
        it is no longer loaded."""
        from videoserver.playervision.backends.onnx import DETECTOR_NAME

        assert [i.filename for i in models.MODELS] == [DETECTOR_NAME]

    def test_it_needs_nothing_beyond_the_standard_library(self):
        """So the download can be offered before the extra is installed."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(models))
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level == 0
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imported & {"numpy", "onnxruntime", "av", "requests"}
