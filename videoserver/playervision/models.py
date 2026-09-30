"""Fetching the models, when -- and only when -- the operator asks.

Player identification needs a detector, and this project ships none. That was
a rule: "nothing is shipped and nothing is downloaded". It is reversed here on
purpose, and only as far as it has to be: nothing is fetched at start-up, on a
setting, or in the background. A download happens because somebody pressed
**Download model** or ran ``python -m videoserver.playervision.models
--download``, having been told the size, the source and the licence.

What it fetches, and why only one
---------------------------------
**YOLOX-Tiny** (Megvii, Apache-2.0) as the detector. A general-purpose
detector at 416x416 -- about 6.5 GFLOP, affordable on a CPU at a few hertz.
It is not trained on game graphics, so whether it finds a given game's
characters is something to *measure*, not assume.

There used to be a second file, MobileNetV2 from the ONNX Model Zoo, as the
appearance model. It is gone because it could not do the job: measured on a
real Mario Kart 64 frame it scored every crop 0.54-0.90 against every player
and rated Peach more like Mario than Mario was. What a character looks like
is a colour signature now, which needs no model -- see
``playervision.signature``. A copy already downloaded is simply left unused.

Pinned by SHA-256, computed from a verified download and checked against the
photograph YOLOX ships as its own demo: the bicycle, the truck and the dog, in
the right places. A file that does not match is refused and removed. Anything
already in the folder that is *not* ours is moved aside rather than
overwritten -- a model the operator supplied is their work.

Stdlib only: ``urllib``, ``hashlib``, ``json``. Nothing here needs the
optional extra, so the download can be offered before it is installed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

__all__ = [
    "MODELS",
    "ModelFile",
    "DownloadError",
    "download",
    "status",
]

#: Bytes read per chunk. Small enough that progress moves and a cancel lands
#: promptly, large enough that a 20 MB file is a few hundred reads.
CHUNK = 256 * 1024

#: How long one read may stall before the download is abandoned.
TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One file to fetch, and everything needed to trust it."""

    #: What it is called in the model folder.
    filename: str
    #: Shown to the operator.
    title: str
    url: str
    sha256: str
    size: int
    licence: str
    source: str
    #: Written beside it as ``<stem>.json`` -- how the model wants its pixels
    #: and how to read what it returns. Declared rather than guessed, because
    #: both mistakes are silent: YOLOX fed 0..1 finds nothing at all.
    sidecar: dict


MODELS: tuple[ModelFile, ...] = (
    ModelFile(
        filename="detector.onnx",
        title="YOLOX-Tiny detector",
        url=(
            "https://github.com/Megvii-BaseDetection/YOLOX/releases/"
            "download/0.1.1rc0/yolox_tiny.onnx"
        ),
        sha256="427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
        size=20_219_662,
        licence="Apache-2.0",
        source="Megvii-BaseDetection/YOLOX, release 0.1.1rc0",
        sidecar={"output": "yolox", "input_range": "0-255", "channels": "bgr"},
    ),
)

NOTICE_NAME = "NOTICE.txt"

#: ``(done_bytes, total_bytes, what)``. Called from the downloading thread.
Progress = Callable[[int, int, str], None]


class DownloadError(RuntimeError):
    """A download that did not produce a trusted file. Nothing was installed."""


def total_size(files: tuple[ModelFile, ...] = MODELS) -> int:
    return sum(item.size for item in files)


def _model_dir() -> Path:
    from .backends.onnx import model_dir

    return model_dir()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def status(
    directory: Path | None = None, files: tuple[ModelFile, ...] = MODELS
) -> dict[str, object]:
    """What is in the model folder, for a status line. Never raises.

    Compares sizes rather than hashing: this is asked from a GUI, and hashing
    20 MB to draw a label is not a price worth paying for a question the size
    answers nearly always. The hash is checked where it matters -- on the way
    in.
    """
    directory = Path(directory) if directory else _model_dir()
    entries = []
    for item in files:
        path = directory / item.filename
        try:
            size = path.stat().st_size if path.is_file() else -1
        except OSError:
            size = -1
        entries.append({
            "file": item.filename,
            "title": item.title,
            "present": size >= 0,
            # "ours" means the file we would download; a present file that is
            # not ours is somebody's own model, which is a perfectly good state.
            "ours": size == item.size,
        })
    return {
        "directory": str(directory),
        "files": entries,
        "detector": entries[0]["present"] if entries else False,
        "complete": all(entry["present"] for entry in entries),
        "download_bytes": total_size(files),
    }


def download(
    directory: Path | None = None,
    *,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    files: tuple[ModelFile, ...] = MODELS,
    opener: Callable | None = None,
) -> list[str]:
    """Fetch every model, verify it, and install it. Returns what was installed.

    Each file goes to a ``.part`` beside its destination and is hashed as it
    arrives; only a verified file is moved into place, with ``os.replace`` so a
    half-written model never exists under the real name. A file already
    installed and matching is left alone, so pressing the button twice costs
    nothing.

    Raises `DownloadError` on anything that is not a clean install -- a network
    failure, a wrong hash, a cancel -- having removed its partial file. Files
    that finished before the failure stay installed: each was verified on its
    own.
    """
    import urllib.request

    directory = Path(directory) if directory else _model_dir()
    directory.mkdir(parents=True, exist_ok=True)
    open_url = opener or (
        lambda url: urllib.request.urlopen(url, timeout=TIMEOUT_S)  # noqa: S310
    )

    total = total_size(files)
    done = 0
    installed: list[str] = []
    for item in files:
        target = directory / item.filename
        if target.is_file() and target.stat().st_size == item.size:
            if _hash_file(target) == item.sha256:
                done += item.size
                if progress:
                    progress(done, total, item.title)
                _write_sidecar(directory, item)
                continue

        part = directory / (item.filename + ".part")
        digest = hashlib.sha256()
        received = 0
        try:
            with open_url(item.url) as response, part.open("wb") as handle:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("cancelled")
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if received > item.size:
                        raise DownloadError(
                            f"{item.title}: more data than expected "
                            f"({received} > {item.size} bytes)"
                        )
                    if progress:
                        progress(done + received, total, item.title)
            if received != item.size:
                raise DownloadError(
                    f"{item.title}: got {received} bytes, expected {item.size}"
                )
            if digest.hexdigest() != item.sha256:
                raise DownloadError(
                    f"{item.title}: the file does not match its pinned SHA-256, "
                    "so it was not installed"
                )
        except DownloadError:
            _remove(part)
            raise
        except Exception as exc:  # noqa: BLE001 -- network, disk, anything
            _remove(part)
            raise DownloadError(f"{item.title}: {exc}") from exc

        _set_aside(target)
        os.replace(part, target)
        _write_sidecar(directory, item)
        done += item.size
        installed.append(item.filename)
        log.info("Installed %s from %s (%s)", item.filename, item.source, item.licence)

    _write_notice(directory, files)
    return installed


def _set_aside(path: Path) -> None:
    """Move an existing file out of the way rather than overwrite it.

    Something already under that name that is not the file being installed is
    most likely the operator's own model. Keeping it costs a rename; losing it
    could cost them a training run.
    """
    if not path.is_file():
        return
    index = 1
    while True:
        spare = path.with_name(f"{path.name}.previous{'' if index == 1 else index}")
        if not spare.exists():
            os.replace(path, spare)
            log.info("Kept the existing %s as %s", path.name, spare.name)
            return
        index += 1


def _write_sidecar(directory: Path, item: ModelFile) -> None:
    sidecar = directory / (Path(item.filename).stem + ".json")
    text = json.dumps(item.sidecar, indent=2) + "\n"
    try:
        if sidecar.is_file() and sidecar.read_text(encoding="utf-8") == text:
            return
    except OSError:
        pass
    _set_aside(sidecar)
    sidecar.write_text(text, encoding="utf-8")


def _write_notice(directory: Path, files: tuple[ModelFile, ...]) -> None:
    lines = [
        "These models were downloaded by remote-bluetooth-game-control at the",
        "operator's request. They are not part of this project and are",
        "distributed under their own licences:",
        "",
    ]
    for item in files:
        lines += [
            f"{item.filename}: {item.title}",
            f"  source:  {item.source}",
            f"  url:     {item.url}",
            f"  licence: {item.licence}",
            f"  sha256:  {item.sha256}",
            "",
        ]
    (directory / NOTICE_NAME).write_text("\n".join(lines), encoding="utf-8")


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="videoserver.playervision.models",
        description="Fetch the player-identification models, or say what is installed.",
    )
    parser.add_argument("--download", action="store_true",
                        help="download and install the models")
    parser.add_argument("--dir", default="", help="model folder (default: the usual one)")
    args = parser.parse_args(argv)

    directory = Path(args.dir) if args.dir else None
    if args.download:
        megabytes = total_size() / 1_000_000
        print(f"Downloading {megabytes:.0f} MB:")
        for item in MODELS:
            print(f"  {item.title} -- {item.source} ({item.licence})")

        def show(done: int, total: int, what: str) -> None:
            print(f"\r  {what}: {done * 100 // max(total, 1)}%   ", end="", flush=True)

        try:
            download(directory, progress=show)
        except DownloadError as exc:
            print(f"\nNot installed: {exc}")
            return 1
        print("\nDone.")

    report = status(directory)
    print(f"Model folder: {report['directory']}")
    for entry in report["files"]:
        state = "missing"
        if entry["present"]:
            state = "installed" if entry["ours"] else "present (not the downloaded one)"
        print(f"  {entry['file']:<14} {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
