"""A pad whose trigger rests at its extent, which is the ordinary Linux case.

**Reported from an N64 pad on Linux**: the walk-through reached Start and bound
something by itself, then reached stick-left, bound that by itself too, and
could not be got past.

One axis explains both. `_deflected_axis` asked only "is this axis near its
extent", on the reasoning that a stick self-centres so the reading alone
answers it. SDL on Linux reports an untouched analog trigger on a *raw*
joystick at full negative -- so such an axis reads as pushed to its extent for
ever. It answered whatever prompt was open the instant the step began, and the
stick step could then never advance, because the return to centre it waits for
was never coming.

The fix disqualifies an axis that was already deflected when the step began,
*until it comes back near centre*. That is what keeps the older stall fixed
too: a player already holding the stick releases it, the axis becomes eligible,
and the next push binds.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client.gui.mapping_dialog import (  # noqa: E402
    _AXIS_CAPTURE_DELTA,
    _AXIS_REARM_LEVEL,
    MappingDialog,
)


class _Dialog:
    """The two attributes `_deflected_axis` reads, and nothing else.

    Called unbound, so this needs no Qt application and no device: what is
    under test is the rule, and the rule is a function of the readings.
    """

    def __init__(self, resting=()):
        self._axis_disqualified = set(resting)
        self._is_keyboard = False
        self._baseline = None


def deflected(dialog, axes):
    """One tick of the stick path's two steps: release check, then look.

    They are separate methods because the release check has to run even when
    the look does not -- the stick path skips the look while it waits for a
    previous push to be let go.
    """
    now = {"axes": list(axes)}
    MappingDialog._release_resting_axes(dialog, now)
    return MappingDialog._deflected_axis(dialog, now)


#: An untouched analog trigger on a raw joystick, as SDL reports it on Linux.
RESTING_TRIGGER = -32768

#: A stick pushed to its extent by a person.
PUSHED = _AXIS_CAPTURE_DELTA + 2000


class TestARestingAxisIsNotAPush:
    def test_it_would_have_been_taken_before(self):
        """The old rule, stated as a control: absolute deflection alone cannot
        tell this from somebody pushing."""
        assert abs(RESTING_TRIGGER) > _AXIS_CAPTURE_DELTA

    def test_it_is_ignored_when_it_was_resting_at_the_start(self):
        dialog = _Dialog(resting={0})

        assert deflected(dialog, [RESTING_TRIGGER, 0]) is None

    def test_a_real_push_on_another_axis_still_binds(self):
        """The disqualification is per axis, not a mode."""
        dialog = _Dialog(resting={0})

        assert deflected(dialog, [RESTING_TRIGGER, PUSHED]) == (1, PUSHED)

    def test_it_never_becomes_eligible_while_it_rests(self):
        """Which is why the stick step could not advance: it waits for a return
        to centre that is not coming."""
        dialog = _Dialog(resting={0})

        for _ in range(50):
            assert deflected(dialog, [RESTING_TRIGGER, 0]) is None

        assert 0 in dialog._axis_disqualified


class TestReleasingMakesAnAxisEligibleAgain:
    """The older stall this must not reintroduce: a player already holding the
    stick when a step opens must be able to release and push again."""

    def test_holding_at_the_start_does_not_bind(self):
        dialog = _Dialog(resting={0})

        assert deflected(dialog, [PUSHED, 0]) is None

    def test_returning_to_centre_clears_it(self):
        dialog = _Dialog(resting={0})

        deflected(dialog, [0, 0])          # released

        assert dialog._axis_disqualified == set()

    def test_and_then_the_next_push_binds(self):
        dialog = _Dialog(resting={0})

        deflected(dialog, [0, 0])          # released
        result = deflected(dialog, [PUSHED, 0])

        assert result == (0, PUSHED)

    def test_part_way_back_is_not_enough(self):
        """The same threshold the stick pair's re-arm uses, so "centred" means
        one thing in this dialog."""
        dialog = _Dialog(resting={0})

        deflected(dialog, [_AXIS_REARM_LEVEL + 100, 0])

        assert dialog._axis_disqualified == {0}

    def test_an_axis_that_vanishes_is_not_kept_disqualified(self):
        """A pad unplugged mid-step reports fewer axes; keeping an index that
        no longer exists would disqualify whatever later takes that number."""
        dialog = _Dialog(resting={3})

        deflected(dialog, [0, 0])

        assert dialog._axis_disqualified == set()


class TestTheRestingSetIsSeededWhereTheBaselineIs:
    """"Already" means "when this step began", so it is recorded at the same
    moment the baseline is."""

    def seeded(self, axes, keyboard=False):
        dialog = _Dialog()
        dialog._is_keyboard = keyboard
        dialog._baseline = {"axes": list(axes)}
        MappingDialog._note_resting_axes(dialog)
        return dialog._axis_disqualified

    def test_an_extreme_axis_is_recorded(self):
        assert self.seeded([RESTING_TRIGGER, 0, 0]) == {0}

    def test_a_centred_one_is_not(self):
        assert self.seeded([0, 0, 0]) == set()

    def test_several_are_recorded(self):
        """Two triggers is the ordinary case on a pad that exposes both raw."""
        assert self.seeded([RESTING_TRIGGER, RESTING_TRIGGER, 0]) == {0, 1}

    def test_the_keyboard_has_no_axes_to_rest(self):
        assert self.seeded([RESTING_TRIGGER], keyboard=True) == set()

    def test_no_baseline_is_survivable(self):
        dialog = _Dialog()
        dialog._baseline = None

        MappingDialog._note_resting_axes(dialog)

        assert dialog._axis_disqualified == set()

    def test_both_capture_paths_record_it(self):
        """A button prompt can be answered with a trigger, so a resting axis
        binds there too -- that is the "Start bound itself" half of the
        report."""
        import inspect

        for method in (MappingDialog._start_capture,
                       MappingDialog._start_axis_capture):
            source = inspect.getsource(method)
            assert "_note_resting_axes()" in source, method.__name__


class TestTheReleaseCheckRunsEvenWhenTheLookDoesNot:
    """**The stick path returns early while it waits for the previous push to
    be released**, so a clearing step that lived inside the look was never
    reached: the axis about to be pushed a second time stayed disqualified for
    ever, and the walk-through stalled at the second half of every stick.
    Caught by the existing wizard tests, as every layout stalling after 600
    ticks."""

    def test_it_is_its_own_step(self):
        assert hasattr(MappingDialog, "_release_resting_axes")

    def test_the_stick_path_calls_it_before_returning_early(self):
        import inspect

        source = inspect.getsource(MappingDialog._capture_stick_half)
        release = source.index("_release_resting_axes")
        rearm = source.index("if not self._axis_rearmed")

        assert release < rearm, (
            "the release check must run before the wait for re-arm returns"
        )

    def test_clearing_does_not_need_the_look(self):
        dialog = _Dialog(resting={0})

        MappingDialog._release_resting_axes(dialog, {"axes": [0, 0]})

        assert dialog._axis_disqualified == set()
