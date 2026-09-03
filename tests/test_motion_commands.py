#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    abort, park and init; the three that were written but never run. All three were exercised for the first time on 2026-08-30, against the simulator, and two of them were wrong.

    Run: python tests/test_motion_commands.py

###############################################################################################"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pyobs_indi import telescope as tel_mod
from pyobs_indi.telescope import IndiTelescope
from pyobs_indi.device import STATE_BUSY, STATE_OK
from pyobs.utils.enums import MotionStatus


class FakeIndi:
    """Stands in for IndiDevice, with a mount that takes time to move."""

    def __init__(self, travel_ticks: int = 5) -> None:
        self.connected = True
        self.sent: list[tuple[str, str]] = []
        self.numbers_sent: dict[str, dict[str, float]] = {}
        self._ra, self._dec = 5.0, 10.0
        self._target: tuple[float, float] | None = None
        self._ticks = travel_ticks
        self.messages: list[str] = []
        self.parked = False
        # A mount that takes the command and does not move; a limit, a
        # refusal, an out-of-range target. INDI has no way to report this:
        # no error, no message, no state change. Silence is the whole
        # problem, which is why arrival is judged by distance, not by status.
        self.refuse = False
        # Sidereal creep. A mount that is not slewing still moves: a parked
        # one is fixed in hour angle, so its right ascension advances about
        # 15 arcsec a second, reported at full precision.
        self.creep_hours = 0.0
        self.ack_delay = 0.15                     # how long the driver takes to agree
        self.park_ticks = 5                       # how far the park position is
        # What the driver privately believes, which can differ from what we
        # last heard. INDI publishes on change, so if it is already in the
        # state you ask for it says so in a message and publishes nothing.
        self.truth_parked = False
        self.refreshes = 0
        self.rev = 0                              # bumps on every driver update
        self.timeout_attr = None                  # driver-declared per-prop timeout

    # -- what the telescope module reads -------------------------------
    def _advance(self) -> None:
        """Creep towards the target, one step per look."""
        self._ra += self.creep_hours
        if self._target is None or self._ticks <= 0:
            return
        self._ticks -= 1
        if self._ticks == 0:
            self._ra, self._dec = self._target
            self._target = None
        else:
            self._ra += 0.1

    def revision(self, prop: str) -> int:
        return self.rev

    def prop_timeout(self, prop: str):
        return self.timeout_attr

    def numbers(self, prop: str):
        if prop != "EQUATORIAL_EOD_COORD":
            return None
        self._advance()
        return {"RA": self._ra, "DEC": self._dec}

    def state(self, prop: str):
        if prop == "EQUATORIAL_EOD_COORD":
            self._advance()
            return STATE_BUSY if self._target is not None else STATE_OK
        return STATE_OK

    def switch_on(self, prop: str):
        if prop == "TELESCOPE_PARK":
            return "PARK" if self.parked else "UNPARK"
        if prop == "TELESCOPE_TRACK_STATE":
            return "TRACK_ON"
        return None

    # -- what the telescope module writes ------------------------------
    async def set_switch(self, prop: str, element: str) -> None:
        self.sent.append((prop, element))
        if prop == "TELESCOPE_PARK":
            # The driver does not answer instantly. Flipping self.parked
            # synchronously would make every test pass regardless, and hide
            # exactly the race that shipped: init read the status before the
            # unpark had landed and reported "parked".
            want = element == "PARK"
            if want == self.truth_parked:
                # No change: a message, and no property update at all.
                self.messages.append(
                    f"Telescope already {'parked' if want else 'unparked'}.")
            else:
                self.truth_parked = want
                asyncio.get_running_loop().call_later(
                    self.ack_delay, setattr, self, "parked", want)
            if element == "PARK":                 # parking is a slew
                self._target, self._ticks = (0.0, 90.0), self.park_ticks
        if prop == "TELESCOPE_ABORT_MOTION":
            self._target = None                   # stops where it stands

    async def set_numbers(self, prop: str, values) -> None:
        self.numbers_sent[prop] = values
        if prop == "EQUATORIAL_EOD_COORD":
            if not self.refuse:
                self.rev += 1
                self._target, self._ticks = (values["RA"], values["DEC"]), 5

    async def refresh(self, prop: str) -> None:
        """Re-announce a property: the cache catches up with the truth."""
        self.refreshes += 1
        if prop == "TELESCOPE_PARK":
            self.parked = self.truth_parked

    async def wait_until(self, check, timeout: float) -> bool:
        """Same contract as IndiDevice.wait_until: poll, bounded, honest."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not check():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True

    async def open(self) -> None: ...
    async def close(self) -> None: ...


def _scope(travel_ticks: int = 5) -> IndiTelescope:
    t = IndiTelescope.__new__(IndiTelescope)      # no pyobs comm needed
    t._indi = FakeIndi(travel_ticks)
    t._settle_time = 0.0
    t._arrival_tolerance = tel_mod.ARRIVAL_TOLERANCE
    t._log_path = None
    t._target = None
    t._cached = None
    t._aborted = False
    t._parking = False
    t._flip_disarmed = False
    t._flipping = False
    t._status = None
    t._eod_rev = -1
    t._stale_polls = 0
    t._link_stale = False
    return t


async def _noop_status(status, *a, **k):
    return None


def test_park_waits_for_the_mount_to_arrive() -> None:
    """The one that bit on 2026-08-30."""
    async def run() -> tuple:
        t = _scope(travel_ticks=4)
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        await t.park()
        return t._indi.numbers("EQUATORIAL_EOD_COORD"), t._indi._target

    tel_mod.POSITION_INTERVAL = 0.01
    where, still_moving = asyncio.run(run())
    assert still_moving is None, "returned while the mount was still travelling"
    assert abs(where["DEC"] - 90.0) < 0.001, \
        f"park returned with the mount at Dec {where['DEC']}, not the park position"


def test_init_waits_for_the_mount_to_say_it_is_unparked() -> None:
    """Seen 2026-08-30: the first init reported `parked`, a second was needed."""
    async def run() -> str:
        t = _scope(travel_ticks=1)
        t._indi.parked = True
        seen: list = []
        async def capture(status, *a, **k):
            seen.append(status)
        t._change_motion_status = capture
        t._get_status = lambda: asyncio.sleep(0, result=(
            MotionStatus.PARKED if t._indi.parked else MotionStatus.IDLE))
        await t.init()
        return str(seen[-1])

    assert asyncio.run(run()) == str(MotionStatus.IDLE), \
        "init returned while the mount still read as parked"


def test_an_aborted_slew_does_not_report_arrival() -> None:
    """The serious one."""
    async def run() -> str:
        t = _scope(travel_ticks=50)               # long slew, plenty of time
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        slew = asyncio.create_task(t._move_radec(80.0, 20.0, asyncio.Event()))
        await asyncio.sleep(0.05)
        await t.stop_motion()                     # somebody else stops it
        try:
            await asyncio.wait_for(slew, 15.0)
        except InterruptedError:
            return "aborted"
        except asyncio.TimeoutError:
            return "hung"
        return "claimed success"

    assert asyncio.run(run()) == "aborted"


def test_a_no_op_command_does_not_hang() -> None:
    """Michael's call, 2026-08-30: it goes wrong on repeated commands."""
    async def run() -> tuple:
        t = _scope()
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        t._indi.truth_parked = False              # the driver: already unparked
        t._indi.parked = True                     # our cache: stale, thinks parked
        await asyncio.wait_for(t.init(), 10.0)
        return t._indi.parked, t._indi.refreshes

    parked, refreshes = asyncio.run(run())
    assert parked is False, "never resynced; still believes the mount is parked"
    assert refreshes >= 1, "gave up without asking the driver"


def test_a_park_can_be_aborted() -> None:
    """A park is a slew like any other and has to be interruptible. Seen 2026-08-30: abort during a park reached the driver; it logged "Parking/Unparking aborted" -- and park() sailed straight past it and announced the mount parked."""
    async def run() -> str:
        t = _scope()
        t._indi.park_ticks = 10_000               # a park that takes for ever
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        park = asyncio.create_task(t.park())
        await asyncio.sleep(0.3)
        await t.stop_motion()
        try:
            await asyncio.wait_for(park, 10.0)
        except InterruptedError:
            return "aborted"
        except asyncio.TimeoutError:
            return "hung"
        return "claimed parked"

    assert asyncio.run(run()) == "aborted"


def test_parking_is_not_reported_as_parked() -> None:
    """The switch flips on acceptance; the mount arrives later. Measured 2026-08-30: park command at 16:55:11.5, driver said "Mount is parked" at 16:55:25.3."""
    async def run() -> str:
        t = _scope(travel_ticks=10_000)
        t._change_motion_status = _noop_status
        t._parking = True                         # mid-park
        t._indi.parked = True                     # switch already flipped
        return str(await t._get_status())

    assert asyncio.run(run()) == str(MotionStatus.PARKING), \
        "reported PARKED while the mount was still travelling"


def test_stopping_short_is_not_an_arrival() -> None:
    """A mount that halts a long way off did not arrive, whatever it says."""
    async def run() -> str:
        t = _scope(travel_ticks=1)
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        t._arrival_tolerance = 60.0               # one arcminute
        t._indi.refuse = True                     # takes the order, does not move
        t._indi._ra, t._indi._dec = 5.0, 10.0     # nowhere near the target
        try:
            await t._move_radec(80.0, 20.0, asyncio.Event())
        except Exception as err:
            return type(err).__name__
        return "claimed arrival"

    assert asyncio.run(run()) == "IndiError"


def test_a_creeping_mount_counts_as_still() -> None:
    """The hang of 2026-08-30."""
    async def run() -> bool:
        t = _scope(travel_ticks=1)
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        t._indi._target = None                    # not slewing...
        t._indi.creep_hours = 15.0 / 3600 / 15    # ...but tracking, 15 arcsec/read
        return await t._wait_until_still(3.0)

    assert asyncio.run(run()) is True, "a tracking mount was never called still"


def test_a_slewing_mount_does_not_count_as_still() -> None:
    """The other half: real motion must not read as settled."""
    async def run() -> bool:
        t = _scope(travel_ticks=10_000)           # travels for ever
        t._change_motion_status = _noop_status
        t._indi._target = (99.0, 45.0)
        t._indi._ticks = 10_000
        return await t._wait_until_still(0.5)

    assert asyncio.run(run()) is False, "called a slewing mount settled"


def test_a_normal_slew_still_reports_arrival() -> None:
    """The guard must not reject good slews."""
    async def run() -> str:
        t = _scope(travel_ticks=3)
        t._change_motion_status = _noop_status
        t._get_status = lambda: asyncio.sleep(0, result=None)
        try:
            await t._move_radec(80.0, 20.0, asyncio.Event())
        except Exception as err:
            return f"{type(err).__name__}: {err}"
        return "arrived"

    assert asyncio.run(run()) == "arrived"


# -- sim-only park-to-pole on startup, and its guards ------------------

def _cold_sim():
    """A scope whose fake reports the simulator, cold-booted parked at 0/0."""
    t = _scope()
    t._park_to_pole_on_start = True
    t._indi.device = "Telescope Simulator"
    t._indi.parked = True
    t._indi.truth_parked = True
    t._cached = (0.0, 0.0)
    return t


def _park_switches(t) -> list:
    return [e for e in t._indi.sent if e[0] == "TELESCOPE_PARK"]


def test_sim_park_to_pole_fires_from_cold_boot() -> None:
    """The whole point: a sim cold-booted parked at 0/0 is sent to the pole."""
    t = _cold_sim()
    asyncio.run(t._park_sim_to_pole_if_cold_booted())
    sent = _park_switches(t)
    assert ("TELESCOPE_PARK", "UNPARK") in sent and ("TELESCOPE_PARK", "PARK") in sent, \
        f"cold-booted sim was not sent to the pole; sent={sent}"


def test_park_to_pole_refuses_a_real_mount() -> None:
    """Gate 2: the device-name check must refuse a real mount even if the flag
    somehow reached it. This is the one that must never fail."""
    t = _cold_sim()
    t._indi.device = "ZWO AM5"                 # a real mount
    asyncio.run(t._park_sim_to_pole_if_cold_booted())
    assert _park_switches(t) == [], "moved a real mount -- the device gate failed"


def test_park_to_pole_off_by_default() -> None:
    """Gate 1: with the flag off, nothing happens even on the sim device."""
    t = _cold_sim()
    t._park_to_pole_on_start = False
    asyncio.run(t._park_sim_to_pole_if_cold_booted())
    assert _park_switches(t) == [], "fired with the flag off"


def test_park_to_pole_leaves_a_tracking_sim() -> None:
    """Gate 3: a module restart onto a *tracking* sim must not yank it to the
    pole -- this is the reconnect-mid-track hazard."""
    t = _cold_sim()
    t._indi.parked = False
    t._indi.truth_parked = False
    t._cached = (12.0, 45.0)                   # tracking somewhere, not parked at 0/0
    asyncio.run(t._park_sim_to_pole_if_cold_booted())
    assert _park_switches(t) == [], "yanked a tracking sim to the pole"


def test_park_to_pole_leaves_an_already_poled_sim() -> None:
    """Gate 3b: parked but already at the pole (not the 0/0 cold-boot spot) is
    left alone, so a restart does not re-park a sim that is already home."""
    t = _cold_sim()
    t._cached = (0.0, 90.0)                     # already at the pole
    asyncio.run(t._park_sim_to_pole_if_cold_booted())
    assert _park_switches(t) == [], "re-parked a sim that was already at the pole"


# -- link liveness: a frozen driver must not read as healthy ----------

def test_liveness_healthy_link_is_never_stale() -> None:
    """A driver that keeps re-publishing (revision advancing every poll, as a
    real one does even at rest) is never flagged stale."""
    t = _scope()
    for i in range(1, tel_mod.STALE_UPDATE_POLLS * 2):
        t._indi.rev = i               # a fresh publish each poll
        t._update_liveness()
    assert t._link_stale is False, "healthy link (advancing revision) flagged stale"


def test_liveness_frozen_revision_overrides_cached_park() -> None:
    """A frozen revision (mount stopped answering) must report UNKNOWN even
    though the cached PARK switch still says PARKED -- the overnight bug."""
    async def run():
        t = _scope()
        t._indi.parked = True             # cached PARK still says parked
        t._indi.rev = 7; t._eod_rev = 7   # was healthy at rev 7, now frozen
        for _ in range(tel_mod.STALE_UPDATE_POLLS):
            t._update_liveness()
        return t._link_stale, str(await t._get_status())
    stale, status = asyncio.run(run())
    assert stale is True, "frozen revision did not latch stale"
    assert status == str(MotionStatus.UNKNOWN), \
        f"stale link reported {status}, not UNKNOWN (false-healthy PARKED)"


def test_liveness_recovers_when_updates_resume() -> None:
    """Once the driver publishes again, the stale latch clears and the real
    state returns."""
    async def run():
        t = _scope()
        t._indi.parked = True
        t._indi.rev = 7; t._eod_rev = 7   # was healthy at rev 7, now frozen
        for _ in range(tel_mod.STALE_UPDATE_POLLS):
            t._update_liveness()
        assert t._link_stale, "setup: should be stale first"
        t._indi.rev = 8                    # a fresh publish
        t._update_liveness()
        return t._link_stale, str(await t._get_status())
    stale, status = asyncio.run(run())
    assert stale is False, "did not recover after updates resumed"
    assert status == str(MotionStatus.PARKED), \
        f"after recovery reported {status}, not the real PARKED"


if __name__ == "__main__":
    tel_mod.POSITION_INTERVAL = 0.01
    tel_mod.UNPARK_TIMEOUT = 1.0
    tel_mod.RESPONSE_TIMEOUT = 0.3
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
