"""Switching a diagnostic on from outside the process.

This exists because the audio investigation twice produced a measurement that
the person holding the fault could not actually take: first a logger with no
way to enable it in a windowed build, then a counter that lived only in a dict
nothing rendered. A diagnostic that cannot be turned on is not a diagnostic, so
the switch itself is worth pinning.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from common.diagnostics import enable_if_asked

ENV = "RBGC_TEST_DIAG"


def file_handlers(logger: logging.Logger) -> list:
    """Only the handlers this module adds.

    pytest attaches its own capture handlers to loggers, so counting
    `logger.handlers` measures the test runner rather than the code -- and the
    same reason means `isEnabledFor` cannot be asserted on either: pytest
    reconfigures the root logger, so the *effective* level is ambient. What
    belongs to `enable_if_asked` is the level it sets on this logger, and the
    file handler it does or does not add.
    """
    return [h for h in logger.handlers if isinstance(h, logging.FileHandler)]


@pytest.fixture
def logger():
    """A logger nobody else holds, torn down so handlers cannot leak."""
    made = logging.getLogger(f"rbgc.test.{uuid.uuid4().hex}")
    made.propagate = False
    yield made
    for handler in list(made.handlers):
        made.removeHandler(handler)
        handler.close()
    made.setLevel(logging.NOTSET)


class TestItStaysOffUnlessAsked:
    def test_an_unset_variable_changes_nothing(self, logger, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)

        assert enable_if_asked(logger, ENV) is False
        assert logger.level == logging.NOTSET
        assert not file_handlers(logger)

    def test_an_empty_variable_is_not_an_instruction(self, logger, monkeypatch):
        """Set-but-blank is how a shell leaves a variable it never filled in."""
        monkeypatch.setenv(ENV, "   ")

        assert enable_if_asked(logger, ENV) is False
        assert logger.level == logging.NOTSET


class TestItTurnsOnWhenAsked:
    @pytest.mark.parametrize("value", ["1", "true", "YES", "On"])
    def test_a_truthy_value_enables_the_logger(self, logger, monkeypatch, value):
        monkeypatch.setenv(ENV, value)

        assert enable_if_asked(logger, ENV) is True
        assert logger.level == logging.INFO

    def test_a_path_is_written_to(self, logger, monkeypatch, tmp_path):
        target = tmp_path / "diag.log"
        monkeypatch.setenv(ENV, str(target))

        assert enable_if_asked(logger, ENV) is True
        logger.info("the line under test")
        for handler in file_handlers(logger):
            handler.flush()

        assert target.exists(), "the path form wrote nothing"
        assert "the line under test" in target.read_text(encoding="utf-8")

    def test_enabling_twice_does_not_double_every_line(
        self, logger, monkeypatch, tmp_path
    ):
        """Both media processes call this from more than one start path.

        A second handler on the same file would duplicate every line, which
        makes a diagnostic look like it is reporting twice the activity -- the
        exact way to get a measurement distrusted.
        """
        target = tmp_path / "diag.log"
        monkeypatch.setenv(ENV, str(target))

        enable_if_asked(logger, ENV)
        enable_if_asked(logger, ENV)
        logger.info("once")
        for handler in file_handlers(logger):
            handler.flush()

        assert len(file_handlers(logger)) == 1
        assert target.read_text(encoding="utf-8").count("once") == 1

    def test_an_unwritable_path_still_enables_the_logger(
        self, logger, monkeypatch, tmp_path
    ):
        """A bad path must not cost the measurement entirely.

        The level is what makes the records exist; the file is only where they
        land. Refusing to enable because one destination failed would turn a
        typo into a silent no-op.
        """
        monkeypatch.setenv(ENV, str(tmp_path / "no-such-dir" / "diag.log"))

        assert enable_if_asked(logger, ENV) is True
        assert logger.level == logging.INFO
        assert not file_handlers(logger)


class TestBothMediaAppsShareOneSwitch:
    def test_the_client_and_the_source_read_the_same_variable(self):
        """One setting has to instrument both ends, or neither is conclusive."""
        pytest.importorskip("av", reason="video extras not installed")

        from client.media.audio import DIAG_ENV as client_env
        from client.media.audio import diag_log as client_log
        from videoserver.encode import DIAG_ENV as source_env
        from videoserver.encode import diag_log as source_log

        assert client_env == source_env == "RBGC_AUDIO_DIAG"
        assert client_log.name == source_log.name
