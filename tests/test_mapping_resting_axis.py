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

**Reported again, and the first fix was only half of it**: binding an analog
trigger bound the *next* control to that trigger's negative direction. The
latch above was honoured by the stick reader alone, so the two baseline-relative
readers -- the trigger one and the button one, which accepts an axis because a
pad may report its d-pad that way -- never checked it. And the whole rule was
measured from **zero**, while the axis in question rests at −32768: letting go
of a trigger is therefore a full sweep of travel away from a baseline taken
while it was still pulled, and it lands somewhere that reads as a decisive
deflection.

So an axis now has a *rest*, learned while nothing is being captured, and
everything is measured from there. Three things had to change together and the
file covers each separately, because each fails on its own:

* every reader checks the latch, not just the stick one;
* "away from centre" becomes "away from **this axis's** rest";
* an axis that comes home has its **baseline healed** to where it actually
  sits, or it is re-qualified into a reading that still looks like a push.

The two halves cover different timing. A release the dialog happens to observe
arriving home is caught by the healing; one caught mid-travel -- the likelier
case at a 16 ms tick -- is caught only by the latch.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6", reason="client GUI extras not installed")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from client.input.mapping import AXIS_PRESS_THRESHOLD  # noqa: E402
from client.gui.mapping_dialog import (  # noqa: E402
    _AXIS_CAPTURE_DELTA,
    _AXIS_REARM_LEVEL,
    MappingDialog,
)


class _Dialog:
    """The handful of attributes the axis readers touch, and nothing else.

    Called unbound, so this needs no Qt application and no device: what is
    under test is the rule, and the rule is a function of the readings.

    ``rest`` defaults to empty, which `_axis_rest_at` reads as 0 for every
    axis -- so every case in this file that does not pass one is describing an
    axis whose rest has **not** been learned, and pins the behaviour this
    dialog had before rest existed.
    """

    def __init__(self, resting=(), rest=()):
        self._axis_disqualified = set(resting)
        self._is_keyboard = False
        self._baseline = None
        self._axis_rest = list(rest)

    _axis_rest_at = MappingDialog._axis_rest_at
    # `_first_changed_control` releases the latch itself, so the fake needs the
    # real method rather than a stub: what is under test is that the release
    # happens on that path at all.
    _release_resting_axes = MappingDialog._release_resting_axes
    _resting_axes_now = MappingDialog._resting_axes_now


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


class TestRestIsLearnedWhileIdle:
    """Where an axis sits when nobody is being asked for anything.

    That reading is what the dialog never had: every threshold was measured
    from zero, while an untouched analog trigger on a raw joystick sits at full
    negative.
    """

    def dialog(self, readings, keyboard=False):
        dialog = _Dialog()
        dialog._axis_rest_samples = []
        dialog._is_keyboard = keyboard
        supply = iter(readings)
        dialog._snapshot = lambda: {"axes": list(next(supply))}
        return dialog

    def test_one_reading_is_not_enough(self):
        dialog = self.dialog([[RESTING_TRIGGER, 0]])

        MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == []

    def test_two_that_agree_are_adopted(self):
        dialog = self.dialog([[RESTING_TRIGGER, 0]] * 2)

        for _ in range(2):
            MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == [RESTING_TRIGGER, 0]

    def test_a_sample_caught_mid_release_is_not_adopted(self):
        """A trigger travelling from full pull to full negative passes through
        every value on the way. Recording one of those as rest is worse than
        having no reading at all, because every later comparison is then
        measured from somewhere the axis never sits."""
        dialog = self.dialog([[20000, 0], [-5000, 0], [RESTING_TRIGGER, 0]])

        for _ in range(3):
            MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == []

    def test_and_it_settles_once_the_travel_stops(self):
        dialog = self.dialog(
            [[20000, 0], [-5000, 0], [RESTING_TRIGGER, 0], [RESTING_TRIGGER, 0]]
        )

        for _ in range(4):
            MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == [RESTING_TRIGGER, 0]

    def test_a_keyboard_has_no_axes_to_learn(self):
        dialog = self.dialog([[RESTING_TRIGGER]] * 2, keyboard=True)

        for _ in range(2):
            MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == []

    def test_a_replug_restarts_the_window(self):
        """Rather than comparing axis 3 of one device against axis 3 of
        another."""
        dialog = self.dialog([[0] * 6, [0, 0], [0, 0]])

        for _ in range(3):
            MappingDialog._learn_axis_rest(dialog)

        assert dialog._axis_rest == [0, 0]


class TestARestingTriggerIsNotDeflected:
    """Once rest is known, a resting trigger stops answering prompts on its own
    -- without needing to be disqualified at all."""

    def test_it_reads_as_no_deflection(self):
        dialog = _Dialog(rest=[RESTING_TRIGGER, 0])

        assert MappingDialog._deflected_axis(
            dialog, {"axes": [RESTING_TRIGGER, 0]}
        ) is None

    def test_pulling_it_still_is(self):
        dialog = _Dialog(rest=[RESTING_TRIGGER, 0])

        assert MappingDialog._deflected_axis(
            dialog, {"axes": [32767, 0]}
        ) == (0, 32767)

    def test_it_is_not_disqualified_at_a_step_start(self):
        """Which is what lets the baseline-relative readers honour the set:
        disqualifying every resting trigger would make one unbindable."""
        dialog = _Dialog(rest=[RESTING_TRIGGER, 0])
        dialog._baseline = {"axes": [RESTING_TRIGGER, 0]}

        MappingDialog._note_resting_axes(dialog)

        assert dialog._axis_disqualified == set()

    def test_but_a_pulled_one_is(self):
        dialog = _Dialog(rest=[RESTING_TRIGGER, 0])
        dialog._baseline = {"axes": [32767, 0]}

        MappingDialog._note_resting_axes(dialog)

        assert dialog._axis_disqualified == {0}


class TestTheBaselineIsHealedWhenAnAxisComesBackToRest:
    """The load-bearing half of the fix.

    A step that opens while a trigger is still pulled takes its baseline there.
    Re-qualifying the axis at its rest without also moving the baseline leaves
    every baseline-relative reader looking at a full sweep of travel -- which is
    the release being read as a decisive push.
    """

    def test_the_baseline_follows_the_axis_home(self):
        dialog = _Dialog(resting={0}, rest=[RESTING_TRIGGER, 0])
        dialog._baseline = {"axes": [32767, 0]}

        MappingDialog._release_resting_axes(dialog, {"axes": [RESTING_TRIGGER, 0]})

        assert dialog._axis_disqualified == set()
        assert dialog._baseline["axes"] == [RESTING_TRIGGER, 0]

    def test_an_axis_still_displaced_is_left_alone(self):
        dialog = _Dialog(resting={0}, rest=[RESTING_TRIGGER, 0])
        dialog._baseline = {"axes": [32767, 0]}

        MappingDialog._release_resting_axes(dialog, {"axes": [32767, 0]})

        assert dialog._axis_disqualified == {0}
        assert dialog._baseline["axes"] == [32767, 0]

    def test_no_baseline_is_survivable(self):
        dialog = _Dialog(resting={0}, rest=[RESTING_TRIGGER])
        dialog._baseline = None

        MappingDialog._release_resting_axes(dialog, {"axes": [RESTING_TRIGGER]})

        assert dialog._axis_disqualified == set()


class TestReleasingATriggerDoesNotAnswerTheNextPrompt:
    """**The reported bug.** Binding an analog trigger and then letting go of it
    bound the *next* control to that trigger's negative direction.

    Both readers are covered, because both were wrong, in the two ways the
    report can present: the next control being another trigger, and the next
    control being a button.
    """

    #: A step that opened while axis 0 was still pulled, so its baseline was
    #: taken there -- which is what `_finish_capture` into `_wizard_step` does,
    #: in the same tick, before the player has let go of anything.
    def opened_while_held(self):
        dialog = _Dialog(rest=[RESTING_TRIGGER, RESTING_TRIGGER, 0])
        dialog._baseline = {
            "axes": [32767, RESTING_TRIGGER, 0],
            "buttons": [False] * 4,
            "hats": [0],
        }
        MappingDialog._note_resting_axes(dialog)
        assert dialog._axis_disqualified == {0}, "the held trigger must be latched"
        return dialog

    def let_go(self):
        return {
            "axes": [RESTING_TRIGGER, RESTING_TRIGGER, 0],
            "buttons": [False] * 4,
            "hats": [0],
        }

    def test_the_trigger_reader_ignores_it(self):
        """Variant A: the next *trigger* bound to AxisBinding(0, invert=True),
        so it read full pull whenever the first trigger sat at rest."""
        dialog = self.opened_while_held()
        now = self.let_go()

        MappingDialog._release_resting_axes(dialog, now)

        assert MappingDialog._changed_axis(
            dialog, now, require_deflection=True
        ) is None

    def test_the_button_reader_ignores_it(self):
        """Variant B: the next *button* bound to InputSource(AXIS, 0, -1)."""
        dialog = self.opened_while_held()
        now = self.let_go()
        dialog._snapshot = lambda: now

        assert MappingDialog._first_changed_control(dialog) is None

    def test_pulling_the_other_trigger_still_binds(self):
        """The fix must not cost the step it is protecting."""
        dialog = self.opened_while_held()
        MappingDialog._release_resting_axes(dialog, self.let_go())

        pulled = {"axes": [RESTING_TRIGGER, 32767, 0]}

        assert MappingDialog._changed_axis(
            dialog, pulled, require_deflection=True
        ) == (1, 32767, RESTING_TRIGGER)

    def test_and_a_button_press_after_the_release_still_binds(self):
        dialog = self.opened_while_held()
        now = self.let_go()
        dialog._snapshot = lambda: now
        MappingDialog._first_changed_control(dialog)

        now["buttons"] = [False, True, False, False]
        source = MappingDialog._first_changed_control(dialog)

        assert source is not None and source.index == 1


class TestEveryReaderChecksTheLatch:
    """A flag consulted in some paths and not others is worse than no flag: the
    paths that respect it stop the correction that would otherwise show up the
    ones that do not. This subsystem has paid for that shape three times."""

    def test_the_latch_gates_all_three_readers(self):
        import inspect

        for method in (
            MappingDialog._deflected_axis,
            MappingDialog._changed_axis,
            MappingDialog._first_changed_control,
        ):
            source = inspect.getsource(method)
            assert "_axis_disqualified" in source, method.__name__

    def test_every_capture_path_releases_it(self):
        import inspect

        for method in (
            MappingDialog._capture_stick_half,
            MappingDialog._capture_trigger,
            MappingDialog._first_changed_control,
        ):
            source = inspect.getsource(method)
            assert "_release_resting_axes" in source, method.__name__

    def test_rest_is_learned_only_while_nothing_is_captured(self):
        """Learning it mid-capture would record the control being pushed as the
        place it rests, which is the reading this whole rule depends on."""
        import inspect

        source = inspect.getsource(MappingDialog._tick)
        assert "_learn_axis_rest()" in source
        assert source.index("if self._capturing is not None") < source.index(
            "_learn_axis_rest()"
        )


class TestATriggerCaughtMidReleaseIsIgnoredToo:
    """**The half of the report the baseline healing does not cover**, and the
    likelier half in practice.

    The dialog polls every 16 ms and a trigger takes tens of milliseconds to
    travel home, so the reading that arrives after the player lets go is
    usually somewhere in the middle rather than at rest. Such an axis is *not*
    back at its rest, so it stays latched and its baseline is not healed --
    which means the latch is the only thing stopping it, and every reader has
    to check it.

    Measured against the old rule below, so this is a real difference in
    behaviour rather than a restatement of the new code.
    """

    #: Far enough home to be a decisive change from a baseline taken at full
    #: pull, and far enough from rest that the re-arm band has not been reached.
    MID_RELEASE = -20000

    def latched(self):
        dialog = _Dialog(rest=[RESTING_TRIGGER, RESTING_TRIGGER, 0])
        dialog._baseline = {
            "axes": [32767, RESTING_TRIGGER, 0],
            "buttons": [False] * 4,
            "hats": [0],
        }
        MappingDialog._note_resting_axes(dialog)
        return dialog

    def moving(self):
        return {
            "axes": [self.MID_RELEASE, RESTING_TRIGGER, 0],
            "buttons": [False] * 4,
            "hats": [0],
        }

    def test_the_old_rule_would_have_taken_it(self):
        """The control. Both of the old predicates match, which is why the next
        control bound itself to this trigger's negative half."""
        assert abs(self.MID_RELEASE - 32767) > _AXIS_CAPTURE_DELTA
        assert abs(self.MID_RELEASE) > AXIS_PRESS_THRESHOLD

    def test_it_is_still_latched(self):
        """Not yet near its rest, so nothing has re-qualified it and nothing
        has healed the baseline. The latch is all there is."""
        dialog = self.latched()
        now = self.moving()

        MappingDialog._release_resting_axes(dialog, now)

        assert dialog._axis_disqualified == {0}
        assert dialog._baseline["axes"][0] == 32767

    def test_the_trigger_reader_ignores_it(self):
        dialog = self.latched()
        now = self.moving()
        MappingDialog._release_resting_axes(dialog, now)

        assert MappingDialog._changed_axis(
            dialog, now, require_deflection=True
        ) is None

    def test_the_button_reader_ignores_it(self):
        dialog = self.latched()
        now = self.moving()
        dialog._snapshot = lambda: now

        assert MappingDialog._first_changed_control(dialog) is None

    def test_the_stick_reader_ignores_it(self):
        dialog = self.latched()

        assert MappingDialog._deflected_axis(dialog, self.moving()) is None

    def test_a_genuine_pull_of_another_axis_is_unaffected(self):
        dialog = self.latched()
        now = self.moving()
        now["axes"][1] = 32767
        MappingDialog._release_resting_axes(dialog, now)

        assert MappingDialog._changed_axis(
            dialog, now, require_deflection=True
        ) == (1, 32767, RESTING_TRIGGER)
