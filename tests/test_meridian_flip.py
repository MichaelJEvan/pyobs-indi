#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    The meridian flip. Measured on the AM3N 2026-08-31: the mount never
    flips on its own; it tracks ~3.6 minutes past the meridian, stops, and
    refuses TRACK_ON until a goto re-acquires the target from the other
    pier side. The module has to be the one to send that goto.

    Run: python tests/test_meridian_flip.py

###############################################################################################"""

import asyncio
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import astropy.units as u
from astropy.coordinates import EarthLocation
from astropy.time import Time

from pyobs.interfaces.ITrackingMode import TrackingMode
from pyobs.utils.enums import MotionStatus
from pyobs_indi import telescope as tel_mod
from pyobs_indi.telescope import eod_to_j2000

from test_motion_commands import FakeIndi, _scope, _noop_status  # noqa: E402
from test_site_time_tracking import FakeIndiTracking             # noqa: E402


class FakeIndiPier(FakeIndi):
    """FakeIndi that also reports a pier side."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.pier: str | None = "PIER_WEST"

    def switch_on(self, prop: str):
        if prop == "TELESCOPE_PIER_SIDE":
            return self.pier
        return super().switch_on(prop)


# McDonald Observatory again: a public site, never the real one.
SITE = SimpleNamespace(location=EarthLocation(
    lat=30.6797 * u.deg, lon=-104.0247 * u.deg, height=2077 * u.m))


def _target_at_ha(seconds_past: float) -> tuple[float, float]:
    """A J2000 target whose hour angle is `seconds_past` right now."""
    lst = Time.now().sidereal_time("apparent", longitude=SITE.location.lon)
    ra_eod_h = (float(lst.hourangle) - seconds_past / 3600.0) % 24.0
    return eod_to_j2000(ra_eod_h, 40.0)


def _flip_rig(seconds_past: float = 120.0, pier: str | None = "PIER_WEST"):
    t = _scope()
    t._indi = FakeIndiPier()
    t._indi.pier = pier
    t._change_motion_status = _noop_status
    t._observer = SITE
    t._auto_flip = True
    t._flip_attempts = 0
    t._no_pier_warned = False
    t._flip_disarmed = False
    t._flipping = False
    t._target = _target_at_ha(seconds_past)
    t.motion_status = lambda: MotionStatus.TRACKING
    return t


def test_flip_is_due_past_the_meridian_on_the_west_side() -> None:
    t = _flip_rig(seconds_past=120.0)
    past = t._seconds_past_meridian()
    assert past is not None and 110.0 < past < 130.0, past
    assert t._flip_due(), "target 2 min past the meridian, pier west: flip is due"


def test_no_flip_east_of_the_meridian() -> None:
    t = _flip_rig(seconds_past=-600.0)
    t._flip_attempts = 2                          # left over from a past crossing
    assert not t._flip_due()
    assert t._flip_attempts == 0, "an eastern target must re-arm the retry count"


def test_no_flip_once_the_pier_side_says_it_happened() -> None:
    t = _flip_rig(seconds_past=300.0, pier="PIER_EAST")
    assert not t._flip_due(), "flipping twice would slew straight back"


def test_no_flip_inside_the_margin() -> None:
    """Just across the line is not yet due; the firmware is still tracking."""
    t = _flip_rig(seconds_past=5.0)
    assert not t._flip_due()


def test_no_flip_without_a_pier_side_report() -> None:
    t = _flip_rig(pier=None)
    assert not t._flip_due()
    assert t._no_pier_warned, "silently disabling a feature is not allowed"


def test_no_flip_while_slewing_or_parked() -> None:
    t = _flip_rig()
    for status in (MotionStatus.SLEWING, MotionStatus.PARKED,
                   MotionStatus.PARKING, MotionStatus.UNKNOWN):
        t.motion_status = lambda s=status: s
        assert not t._flip_due(), f"tried to flip while {status}"


def test_the_watcher_sends_the_goto() -> None:
    async def run() -> list:
        t = _flip_rig(seconds_past=120.0)
        sent = []
        async def fake_move(ra, dec, **kw):
            sent.append((ra, dec))
            t._indi.pier = "PIER_EAST"            # the goto flips the mount
        t.move_radec = fake_move
        watch = asyncio.create_task(t._meridian_watch())
        try:
            deadline = asyncio.get_running_loop().time() + 2.0
            while not sent and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.02)
        finally:
            watch.cancel()
        return sent

    sent = asyncio.run(run())
    assert len(sent) == 1, f"expected exactly one flip goto, got {sent}"


def test_the_watcher_gives_up_after_enough_failures() -> None:
    async def run() -> int:
        t = _flip_rig(seconds_past=120.0)
        calls = 0
        async def failing_move(ra, dec, **kw):
            nonlocal calls
            calls += 1
            raise RuntimeError("mount said no")
        t.move_radec = failing_move
        watch = asyncio.create_task(t._meridian_watch())
        try:
            await asyncio.sleep(0.5)              # many poll intervals
        finally:
            watch.cancel()
        return calls

    calls = asyncio.run(run())
    assert calls == tel_mod.FLIP_RETRIES, \
        f"expected {tel_mod.FLIP_RETRIES} attempts then silence, got {calls}"


def test_stop_clears_the_flip_target() -> None:
    """A stopped mount must stay stopped; the watcher may not re-slew it."""
    async def run():
        t = _flip_rig()
        t._get_status = lambda: asyncio.sleep(0, result=MotionStatus.IDLE)
        await t.stop_motion()
        return t._target

    assert asyncio.run(run()) is None


def test_abort_stands_the_watcher_down_even_if_target_reappears() -> None:
    """The race that moved the mount after an abort (2026-09-01): stop_motion
    clears _target, but a flip goto in flight re-sets it. The disarm latch
    must win regardless, so _flip_due stays False even with a target set."""
    async def run():
        t = _flip_rig(seconds_past=120.0)
        t._get_status = lambda: asyncio.sleep(0, result=MotionStatus.IDLE)
        await t.stop_motion()                       # user aborts
        t._target = _target_at_ha(120.0)            # a flip goto re-sets it
        return t._flip_disarmed, t._flip_due()

    disarmed, due = asyncio.run(run())
    assert disarmed is True, "abort did not disarm the watcher"
    assert due is False, "watcher re-armed after an abort despite a target"


def test_a_deliberate_slew_rearms_the_watcher() -> None:
    """The latch is not permanent: slewing somewhere new turns flips back on."""
    async def run():
        t = _flip_rig(seconds_past=120.0)
        t._get_status = lambda: asyncio.sleep(0, result=MotionStatus.IDLE)
        t._change_motion_status = _noop_status
        await t.stop_motion()
        assert t._flip_disarmed
        # A user slew (not the watcher's own) must clear the latch.
        await t._move_radec(*_target_at_ha(-30.0), asyncio.Event())
        return t._flip_disarmed

    assert asyncio.run(run()) is False, "a deliberate slew did not re-arm the watcher"


def test_a_refused_tracking_reset_no_longer_raises() -> None:
    """The firmware refuses TRACK_ON past the meridian; the goto that follows
    is the cure, so the refusal must not kill it (it did, twice, 2026-08-31)."""
    async def run():
        t = _scope()
        fake = FakeIndiTracking()
        fake.switches["TELESCOPE_TRACK_STATE"] = "TRACK_OFF"
        real = fake.set_switch
        async def refusing(prop, element):
            if prop == "TELESCOPE_TRACK_STATE":
                fake.sent.append((prop, element))  # heard, refused, no change
                return
            await real(prop, element)
        fake.set_switch = refusing
        t._indi = fake
        t._tracking_modes = [TrackingMode.SIDEREAL, TrackingMode.OFF]
        states = []
        async def set_state(iface, state): states.append(state)
        t._comm = SimpleNamespace(set_state=set_state)
        await t.set_tracking_mode(TrackingMode.SIDEREAL)
        return states

    states = asyncio.run(run())
    assert states, "no state published after the refusal"
    assert states[-1].mode == TrackingMode.OFF, \
        "published the requested mode instead of the mount's real one"


if __name__ == "__main__":
    tel_mod.POSITION_INTERVAL = 0.01
    tel_mod.UNPARK_TIMEOUT = 0.2
    tel_mod.RESPONSE_TIMEOUT = 0.3
    tel_mod.FLIP_POLL = 0.02
    tel_mod.FLIP_RETRY_DELAY = 0.02
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
