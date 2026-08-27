"""Turning a diagnostic on from outside the process.

Both media apps normally run **windowed, with no console** -- the client and the
video server are both built with ``console=False`` -- so a measurement that can
only be switched on by editing code, or by passing ``-v`` to a program nobody
starts from a terminal, is a measurement the person holding the fault cannot
produce. That is not a hypothetical: it is why the audio path went so long with
no numbers attached to it.

One environment variable, read at startup by whichever subsystem cares:

    RBGC_AUDIO_DIAG=1                     enable, to wherever logging goes
    RBGC_AUDIO_DIAG=C:/temp/audio.log     enable, and write it to that file

The file form is the one that matters for a windowed build, because there is
nowhere for a stream handler to write that anybody can read afterwards -- and
because a file can be sent to whoever is diagnosing it.

Stdlib only, and no import of anything else in this repo, so both sides can use
it without dragging in each other's dependencies.
"""

from __future__ import annotations

import logging
import os

_TRUTHY = ("1", "true", "yes", "on")

_FILE_FORMAT = "%(asctime)s %(message)s"
_STREAM_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_TIME_FORMAT = "%H:%M:%S"


def enable_if_asked(
    logger: logging.Logger,
    env_var: str,
    *,
    level: int = logging.INFO,
) -> bool:
    """Switch ``logger`` on if ``env_var`` is set. Returns whether it was.

    Safe to call more than once: a second call with the same file target would
    otherwise stack a second handler and duplicate every line, which is exactly
    the sort of thing that makes a diagnostic distrusted.
    """
    setting = os.environ.get(env_var, "").strip()
    if not setting:
        return False

    logger.setLevel(level)

    if setting.lower() not in _TRUTHY:
        target = os.path.abspath(setting)
        already = any(
            getattr(handler, "baseFilename", None) == target
            for handler in logger.handlers
        )
        if not already:
            try:
                handler = logging.FileHandler(target, encoding="utf-8")
                handler.setFormatter(
                    logging.Formatter(_FILE_FORMAT, datefmt=_TIME_FORMAT)
                )
                logger.addHandler(handler)
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "Could not open %s for diagnostics: %s", target, exc
                )

    # A record is dropped if nothing can emit it. A windowed build has no
    # handlers at all, and logging's last-resort handler only passes WARNING
    # and above -- so without this the line would be enabled and still
    # invisible, which is the worst of both.
    if not logging.getLogger().handlers and not logger.handlers:
        logging.basicConfig(
            level=logging.WARNING,
            format=_STREAM_FORMAT,
            datefmt=_TIME_FORMAT,
        )

    return True
