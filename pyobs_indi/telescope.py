"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    pyobs telescope module for INDI mounts.

    Structure follows pyobs_alpaca.AlpacaTelescope: a thin mapping from the
    pyobs interfaces onto a protocol client (device.py) that knows nothing
    about pyobs.

    Coordinate systems: INDI's EQUATORIAL_EOD_COORD is equinox-of-date with
    RA in hours; pyobs uses J2000 degrees. Both conversions are done in this
    file and nowhere else.

###############################################################################################"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

import astropy.units as u
from astropy.coordinates import FK5, SkyCoord
from astropy.time import Time

from pyobs.interfaces import IPointingRaDec, ITrackingMode
from pyobs.interfaces.IPointingRaDec import RaDecState
from pyobs.interfaces.ITrackingMode import (TrackingMode, TrackingModeCapabilities,
                                            TrackingModeState)
from pyobs.utils import exceptions as exc
from pyobs.modules.telescope import BaseTelescope
from pyobs.utils.enums import MotionStatus

from .device import STATE_ALERT, STATE_BUSY, IndiDevice, IndiError

log = logging.getLogger(__name__)

POSITION_INTERVAL = 1.0     # matches typical INDI driver publish rate
STALE_UPDATE_POLLS = 5      # consecutive position polls with a frozen revision
                            # AND fresh driver errors before the link is treated
                            # as dead (see _update_liveness). A frozen revision
                            # alone is not enough: the INDI simulator publishes
                            # only on change and freezes while healthily holding
                            # a static target, so the driver's error flood is
                            # what tells a dead link apart. 5 clears timing skew.
RECONNECT_ATTEMPTS = 3      # auto-reconnect tries after a stale link is
                            # detected, before escalating to manual help
RECONNECT_PAUSE = 3.0       # seconds between the DISCONNECT and the CONNECT
RECONNECT_SETTLE = 20.0     # wait after CONNECT for the link to re-establish;
                            # the AM5's handshake took ~16 s (2026-08-31), so
                            # give it room before counting the attempt failed
MOVE_TIMEOUT = 300.0        # max wait for a park to finish moving
UNPARK_TIMEOUT = 30.0       # max wait for the park switch to change
RESPONSE_TIMEOUT = 60.0     # fallback wait for the driver's reply to a slew
                            # command, used only when the driver did not send
                            # its own per-property timeout (the spec-preferred
                            # source)
ARRIVAL_TOLERANCE = 3600.0  # arcsec; beyond this a "finished" slew is a failure
STILL_TOLERANCE = 30.0      # arcsec between reads to count as not moving.
                            # Sidereal creep is ~15 arcsec/s, a slew is
                            # thousands; this sits between them.
FLIP_POLL = 5.0             # meridian watcher check interval
FLIP_MARGIN = 30.0          # seconds past the meridian before the flip goto
                            # goes out. The AM5 firmware keeps tracking about
                            # 3.6 minutes past the line before stopping
                            # (measured 2026-08-31), so this fires while the
                            # mount is still tracking and no time is lost.
FLIP_RETRIES = 3            # failed flip attempts per crossing before giving up
FLIP_RETRY_DELAY = 60.0     # seconds between those attempts

LOG_COLUMNS = ["time", "event", "ra_j2000", "dec_j2000", "ra_eod_h", "dec_eod",
               "ra_reported_h", "dec_reported", "arcsec_off", "note"]


def eod_to_j2000(ra_hours: float, dec_deg: float) -> tuple[float, float]:
    """INDI equinox-of-date (RA in hours) -> J2000 degrees."""
    c = SkyCoord(ra=ra_hours * u.hourangle, dec=dec_deg * u.deg,
                 frame=FK5(equinox=Time.now()))
    j = c.transform_to(FK5(equinox="J2000"))
    return float(j.ra.deg), float(j.dec.deg)


def j2000_to_eod(ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """J2000 degrees -> INDI equinox-of-date (RA in hours)."""
    now = Time.now()
    c = SkyCoord(ra=ra_deg * u.deg, dec=dec_deg * u.deg, frame=FK5(equinox="J2000"))
    e = c.transform_to(FK5(equinox=now))
    return float(e.ra.hourangle), float(e.dec.deg)


class IndiTelescope(BaseTelescope, IPointingRaDec, ITrackingMode):
    """pyobs telescope module driving an INDI mount.

    IPointingRaDec is declared explicitly: BaseTelescope only provides
    ITelescope and IFitsHeaderBefore, and without the declaration the
    published RaDecState is never looked up by clients (pyobs-gui showed
    position as N/A until this was added). The dummy telescopes in pyobs-core
    declare it the same way.
    """

    __module__ = "pyobs_indi"

    def __init__(self, host: str, device: str, port: int = 7624,
                 settle_time: float = 1.0, arrival_tolerance: float = ARRIVAL_TOLERANCE,
                 log_file: str | None = "analysis/indi-log.csv",
                 auto_flip: bool = True,
                 park_to_pole_on_start: bool = False,
                 **kwargs: Any) -> None:
        """
        Args:
            host: machine running indiserver.
            device: INDI device name, e.g. "Telescope Simulator" or "ZWO AM5".
            port: indiserver port.
            settle_time: seconds to wait after a slew reports complete.
            auto_flip: perform the meridian flip automatically. The mount
                never flips on its own (see _meridian_watch); with this off,
                a tracked target is dropped a few minutes past the meridian
                until someone re-slews by hand.
            arrival_tolerance: arcsec. A slew ending further than this from
                its target is treated as a failure (limit hit, refusal, or an
                untracked stop). Default is loose because an unmodelled real
                mount can be arcminutes off.
            log_file: CSV log of every slew, arrival and refusal; None
                disables it. Exists mainly to validate the epoch conversion,
                which produces plausible-looking coordinates even when wrong.
                Relative paths resolve against the package directory, not the
                CWD, so the log location does not depend on where pyobs was
                launched.
        """
        super().__init__(motion_status_interfaces=["ITelescope"], **kwargs)
        self._indi = IndiDevice(host, device, port)
        self._settle_time = settle_time
        self._arrival_tolerance = arrival_tolerance
        self._log_path = self._resolve_log(log_file)
        self._target: tuple[float, float] | None = None
        self._cached: tuple[float, float] | None = None
        # Set by stop_motion, cleared at slew start. Distinguishes an abort
        # from an arrival inside _move_radec (both end the Busy state).
        self._aborted = False
        self._parking = False
        # OFF is required: INDI's abort restores the pre-move state, so an
        # aborted mount usually resumes tracking. TRACK_OFF is the only way
        # to make it hold still.
        self._tracking_modes = [TrackingMode.SIDEREAL, TrackingMode.SOLAR,
                                TrackingMode.LUNAR, TrackingMode.OFF]
        # Liveness: a dead serial link leaves the cached position and switches
        # looking valid (numbers()/switch_on() read from cache), so a parked
        # mount whose link died kept reporting PARKED all night while
        # unreachable (2026-09-03). Track the driver's update revision instead:
        # if it stops advancing, the mount has stopped answering.
        self._eod_rev = -1
        self._stale_polls = 0
        self._errs_at_fresh = 0
        self._errs_prev = 0
        self._err_polls = 0
        self._link_stale = False
        # Auto-reconnect: on a detected stale link, try to recover the driver's
        # connection (disconnect->reconnect re-opens the serial). Single-flight,
        # capped, and it gives up rather than hammering a link that stays dead.
        self._reconnecting = False
        self._reconnect_attempts = 0
        self._reconnect_gave_up = False
        self._auto_flip = auto_flip
        # SIM-ONLY, off by default. The INDI Telescope Simulator cold-boots
        # parked at RA0/Dec0 (below the horizon); with this set it is sent to
        # the pole on startup. Triple-guarded so it can never move a real
        # mount -- see _park_sim_to_pole_if_cold_booted.
        self._park_to_pole_on_start = park_to_pole_on_start
        self._flip_attempts = 0
        self._no_pier_warned = False
        # A manual stop stands the auto-flip watcher down until the next
        # deliberate slew. Without this, aborting a slew past the meridian
        # races the watcher: stop_motion clears _target, but the watcher's
        # own flip goto re-sets _target at slew start, so it re-armed and
        # re-slewed a mount the user had just stopped -- two or three times
        # (seen on the sim 2026-09-01, and it would move real iron back to
        # life after an abort). This latch does not depend on that ordering.
        self._flip_disarmed = False
        self._flipping = False        # True only while the watcher's own goto runs
        # Re-send site/time after every reconnect, not just the first
        # connect: a restarted driver resets to LAT=0/LONG=0. The same hook
        # re-adopts the tracked target, so a module restart does not disarm
        # the meridian-flip watcher (see _on_connected).
        self._indi.on_connected = self._on_connected
        self.add_background_task(self._update_position)
        self.add_background_task(self._meridian_watch)

    @property
    def _position_radec(self) -> tuple[float, float] | None:
        return self._cached

    @staticmethod
    def _resolve_log(log_file: str | None) -> pathlib.Path | None:
        """Resolve the CSV path against the package dir; None disables logging."""
        if not log_file:
            return None
        path = pathlib.Path(log_file).expanduser()
        if not path.is_absolute():
            path = pathlib.Path(__file__).resolve().parent.parent / path
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _record(self, event: str, **fields: Any) -> None:
        """Append one CSV row. Logging failures must never affect the mount."""
        if self._log_path is None:
            return
        try:
            new = not self._log_path.exists()
            with self._log_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=LOG_COLUMNS, extrasaction="ignore")
                if new:
                    w.writeheader()
                row = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                       "event": event}
                row.update({k: (f"{v:.6f}" if isinstance(v, float) else v)
                            for k, v in fields.items()})
                w.writerow(row)
        except Exception as err:
            # Warn once and disable rather than retrying every row.
            log.warning("indi       could not write %s (%s); disabling the log",
                        self._log_path, err)
            self._log_path = None

    async def open(self) -> None:
        await BaseTelescope.open(self)
        await self._indi.open()
        # Publish state before returning: pyobs verifies after open() that
        # every stateful interface has published something, and leaving it to
        # the background task's first tick is a race (lost once, 2026-08-30).
        # _indi.open() has already waited for the coordinates.
        await self._publish_position()
        await self.comm.set_capabilities(
            ITrackingMode, TrackingModeCapabilities(modes=self._tracking_modes))
        await self.comm.set_state(
            ITrackingMode, TrackingModeState(mode=self._tracking_mode_now()))
        await self._change_motion_status(await self._get_status())
        await self._park_sim_to_pole_if_cold_booted()

    async def _park_sim_to_pole_if_cold_booted(self) -> None:
        """SIM ONLY: send the simulator to the pole if it cold-booted at 0/0.

        The INDI Telescope Simulator has no way to set its start position and
        always cold-boots parked at RA0/Dec0, below the horizon. When enabled
        (indi-sim.yaml only), unpark->park sends it to its pole park position.

        Triple-guarded so this can NEVER move a real mount or a tracking sim:
          1. opt-in flag, off by default, set ONLY in indi-sim.yaml;
          2. the device name must be exactly 'Telescope Simulator' -- a real
             mount ('ZWO AM5') is refused even if the flag somehow reached it;
          3. it fires only from the cold-boot state (parked AND sitting at
             ~Dec 0), so a module restart onto a tracking or already-poled sim
             leaves it untouched.
        This is transient park/unpark to the *existing* park position -- it
        writes no mount setting (see the never-change-internal-settings rule).
        """
        if not self._park_to_pole_on_start:
            return
        if self._indi.device != "Telescope Simulator":
            log.warning("indi       park_to_pole_on_start ignored: device is %r, "
                        "not the simulator -- refusing to move it", self._indi.device)
            return
        if self._indi.switch_on("TELESCOPE_PARK") != "PARK":
            return                      # only the cold-boot parked state qualifies
        pos = self._position_radec
        if pos is None or abs(pos[1]) > 1.0:
            return                      # not at the ~Dec 0 cold-boot spot; leave it
        log.info("indi       sim cold-booted parked at the horizon; unpark->park to the pole")
        await self._indi.set_switch("TELESCOPE_PARK", "UNPARK")
        await self._settled_on_switch("TELESCOPE_PARK", "UNPARK")
        await self._indi.set_switch("TELESCOPE_PARK", "PARK")

    async def close(self) -> None:
        await self._indi.close()
        await BaseTelescope.close(self)

    async def _get_status(self) -> MotionStatus:
        """Derive motion status from the coordinate vector state and switches.

        Vector state: Idle before a slew, Busy during, Ok after. Park is a
        separate switch and takes precedence over the vector.
        """
        if self._link_stale:
            # The driver has stopped updating the position: the mount is not
            # answering, even though cached switches (e.g. PARK) still read
            # valid. Report UNKNOWN so a dead link cannot masquerade as PARKED
            # or TRACKING. Checked first so it overrides every cached state.
            return MotionStatus.UNKNOWN
        if not self._indi.connected or self._indi.state("EQUATORIAL_EOD_COORD") is None:
            # Connected but with an empty cache (e.g. right after reconnect)
            # must be UNKNOWN, not IDLE: we have not heard from the mount yet.
            return MotionStatus.UNKNOWN
        if self._parking:
            # The park switch flips on command *acceptance*, ~14 s before the
            # mount actually stops (measured 2026-08-30). Hold PARKING until
            # park() finishes waiting.
            return MotionStatus.PARKING
        if self._indi.switch_on("TELESCOPE_PARK") == "PARK":
            return MotionStatus.PARKED
        state = self._indi.state("EQUATORIAL_EOD_COORD")
        if state == STATE_BUSY:
            return MotionStatus.SLEWING
        if state == STATE_ALERT:
            return MotionStatus.ERROR
        if self._indi.switch_on("TELESCOPE_TRACK_STATE") == "TRACK_ON":
            return MotionStatus.TRACKING
        return MotionStatus.IDLE

    async def _publish_position(self) -> bool:
        """Publish the reported position, or clear it if contact is lost."""
        nums = self._indi.numbers("EQUATORIAL_EOD_COORD")
        if nums and "RA" in nums:
            ra, dec = eod_to_j2000(nums["RA"], nums["DEC"])
            self._cached = (ra, dec)
            await self.comm.set_state(IPointingRaDec, RaDecState(ra=ra, dec=dec))
            return True
        if self._cached is not None:
            # Device cache was emptied -> link is down. Clear our copy too, or
            # FITS headers etc. would keep quoting the stale position.
            log.warning("indi       position unknown; no contact with the mount")
            self._cached = None
        return False

    def _update_liveness(self) -> None:
        """Track whether the mount has stopped answering.

        Two measured death modes, and neither a frozen revision nor an error
        burst alone tells them apart from health:

        1. Frozen feed. The link drops and position updates stop. But a driver
           that publishes only on change (the INDI simulator) also freezes while
           perfectly tracking a static target, yet is healthy -- so a frozen
           revision counts as dead only if the driver is ALSO erroring.
        2. Erroring feed. Mount powered off with USB still attached (AM5,
           measured 2026-09-03): the driver keeps re-publishing stale
           coordinates on its timer, so the revision advances every poll and
           mode 1 never fires -- yet it floods "Serial write error" / "Error
           reading RA/DEC" the whole time. Detect this on the error stream:
           new errors every poll for STALE_UPDATE_POLLS in a row.

        A healthy mount (sim or real) brings zero of these errors, and a one-off
        serial blip cannot sustain the run, so neither test false-fires. When
        either latches, _get_status reports UNKNOWN instead of a stale state.
        """
        rev = self._indi.revision("EQUATORIAL_EOD_COORD")
        errs = self._indi.error_count

        # Mode 2: count consecutive polls that each brought new errors.
        if errs > self._errs_prev:
            self._err_polls += 1
        else:
            self._err_polls = 0
        self._errs_prev = errs
        erroring = self._err_polls >= STALE_UPDATE_POLLS

        if rev != self._eod_rev:
            # Feed is advancing. Note the error count now so a later frozen
            # stretch can tell new errors from ones already seen.
            self._eod_rev = rev
            self._stale_polls = 0
            self._errs_at_fresh = errs
            # Recovery: link answering AND no longer erroring -> clear.
            if self._link_stale and not erroring:
                self._link_stale = False
                self._reconnect_attempts = 0
                self._reconnect_gave_up = False
                log.info("indi       mount is answering again; position updating")
        else:
            self._stale_polls += 1

        # Mode 1: frozen revision long enough AND fresh errors since it froze.
        frozen_dead = (self._stale_polls >= STALE_UPDATE_POLLS
                       and errs > self._errs_at_fresh)
        if (erroring or frozen_dead) and not self._link_stale:
            self._link_stale = True
            why = ("driver erroring every poll while position still updates"
                   if erroring else
                   "no position update in %d polls and the driver is erroring"
                   % self._stale_polls)
            log.warning("indi       mount stopped answering: %s; link may be "
                        "down, reporting UNKNOWN", why)

    async def _update_position(self) -> None:
        """Background task: publish position and motion status once a second."""
        while True:
            await self._publish_position()
            self._update_liveness()
            if (self._link_stale and not self._reconnecting
                    and not self._reconnect_gave_up):
                asyncio.create_task(self._auto_reconnect())
            status = await self._get_status()
            if status != self.motion_status():
                await self._change_motion_status(status)
            await asyncio.sleep(POSITION_INTERVAL)

    async def _auto_reconnect(self) -> None:
        """Recover a stale link by reconnecting the driver.

        DISCONNECT then CONNECT re-opens the serial and re-handshakes -- what
        recovered the AM3 by hand 2026-09-03. Recovers a wedged session; a link
        truly gone (USB removed, mount powered off) will not come back this way,
        so it is capped at RECONNECT_ATTEMPTS and then escalates rather than
        hammering. It only re-establishes comms and reads state -- it never
        commands motion, so the mount is not moved. _update_liveness clears
        _link_stale (and resets these counters) once updates resume.
        """
        if self._reconnecting:
            return
        self._reconnecting = True
        try:
            while (self._link_stale
                   and self._reconnect_attempts < RECONNECT_ATTEMPTS):
                self._reconnect_attempts += 1
                log.warning("indi       auto-reconnect attempt %d/%d",
                            self._reconnect_attempts, RECONNECT_ATTEMPTS)
                with contextlib.suppress(Exception):
                    await self._indi.set_switch("CONNECTION", "DISCONNECT")
                    await asyncio.sleep(RECONNECT_PAUSE)
                    await self._indi.set_switch("CONNECTION", "CONNECT")
                await asyncio.sleep(RECONNECT_SETTLE)
            if self._link_stale and not self._reconnect_gave_up:
                self._reconnect_gave_up = True
                log.error("indi       auto-reconnect failed after %d attempts; "
                          "link still down -- needs manual help (power / USB / "
                          "cabling). Staying UNKNOWN.", RECONNECT_ATTEMPTS)
        finally:
            self._reconnecting = False

    def _seconds_past_meridian(self) -> float | None:
        """Seconds since the tracked target crossed the meridian; negative before.

        Hour angle of the target (not of the mount): LST minus the target's
        equinox-of-date RA. Uses sidereal seconds, which differ from clock
        seconds by 0.3%; irrelevant at this precision.
        """
        if self._target is None or self._observer is None:
            return None
        ra_h, _ = j2000_to_eod(*self._target)
        lst = Time.now().sidereal_time("apparent",
                                       longitude=self._observer.location.lon)
        ha_h = (float(lst.hourangle) - ra_h + 12.0) % 24.0 - 12.0
        return ha_h * 3600.0

    def _flip_due(self) -> bool:
        """Decide whether the meridian-flip goto should go out now.

        Requires: a tracked target past the meridian by FLIP_MARGIN, the
        mount still on the west pier side (i.e. not yet flipped), and no
        slew or park in progress. TRACKING or IDLE both qualify: the
        firmware stops tracking a few minutes past the line, so a mount
        that already gave up still needs the flip.
        """
        if (not self._auto_flip or self._flip_disarmed
                or self._target is None or not self._indi.connected):
            return False
        if self.motion_status() not in (MotionStatus.TRACKING, MotionStatus.IDLE):
            return False
        pier = self._indi.switch_on("TELESCOPE_PIER_SIDE")
        if pier is None:
            if not self._no_pier_warned:
                log.warning("indi       driver does not report pier side; "
                            "automatic meridian flip is off")
                self._no_pier_warned = True
            return False
        past = self._seconds_past_meridian()
        if past is None:
            return False
        if pier != "PIER_WEST" or past < 0.0:
            self._flip_attempts = 0      # crossing over, or not begun: re-arm
            return False
        if self._flip_attempts >= FLIP_RETRIES:
            return False                 # gave up on this crossing
        return past >= FLIP_MARGIN

    async def _meridian_watch(self) -> None:
        """Background task: perform the meridian flip the mount never will.

        Measured on the AM3N 2026-08-31, and confirmed by the INDI forum's
        AM5 threads: the firmware tracks ~3.6 minutes past the meridian,
        stops, and refuses TRACK_ON until a goto re-acquires the target from
        the other pier side. It never flips by itself; the flip is always
        the client's job (Ekos and NINA both send a goto to the current
        target at HA 0). This is that client. The goto goes through the
        normal move_radec path, so the altitude check, motion status, events
        and the CSV log all behave as for any other slew.
        """
        while True:
            await asyncio.sleep(FLIP_POLL)
            if not self._flip_due():
                continue
            ra, dec = self._target
            log.info("indi       target crossed the meridian; flipping the mount")
            try:
                # Mark this goto as the watcher's own, so move_radec does not
                # treat it as a user slew and re-arm the watcher it fired.
                self._flipping = True
                try:
                    await self.move_radec(ra, dec)
                finally:
                    self._flipping = False
                log.info("indi       meridian flip complete; tracking resumed")
                self._flip_attempts = 0
            except Exception as err:
                self._flip_attempts += 1
                if self._flip_attempts >= FLIP_RETRIES:
                    log.error("indi       meridian flip failed %d times (%s); "
                              "giving up until the next crossing",
                              self._flip_attempts, err)
                else:
                    log.warning("indi       meridian flip failed (%s); "
                                "retrying in %.0f s", err, FLIP_RETRY_DELAY)
                    await asyncio.sleep(FLIP_RETRY_DELAY)

    async def _move_radec(self, ra: float, dec: float, abort_event: asyncio.Event) -> None:
        """Slew and wait for arrival.

        The target is validated locally because the driver ignores
        out-of-range targets without any error or state change.
        """
        if not -90.0 <= dec <= 90.0:
            self._record("error", ra_j2000=ra, dec_j2000=dec,
                         note="declination out of range, refused before sending")
            raise IndiError(f"declination {dec:.4f} is outside +/-90; INDI would ignore it silently")

        ra_h, dec_eod = j2000_to_eod(ra, dec)
        self._target = (ra, dec)
        self._aborted = False
        # A deliberate slew (user, scheduler, or a re-slew after an abort)
        # re-arms the watcher; the watcher's own flip goto does not.
        if not self._flipping:
            self._flip_disarmed = False
        self._record("slew", ra_j2000=ra, dec_j2000=dec, ra_eod_h=ra_h, dec_eod=dec_eod)

        await self._indi.set_switch("ON_COORD_SET", "TRACK")
        rev0 = self._indi.revision("EQUATORIAL_EOD_COORD")
        await self._indi.set_numbers("EQUATORIAL_EOD_COORD", {"RA": ra_h, "DEC": dec_eod})

        # Wait for the driver's actual response instead of a fixed sleep. The
        # spec does not promise an immediate Busy, but every update bumps the
        # property revision; the bound comes from the driver's own timeout
        # attribute when it sent one (spec-preferred), else RESPONSE_TIMEOUT.
        bound = self._indi.prop_timeout("EQUATORIAL_EOD_COORD") or RESPONSE_TIMEOUT
        responded = await self._indi.wait_until(
            lambda: self._indi.revision("EQUATORIAL_EOD_COORD") > rev0
                    or self._aborted or abort_event.is_set(), bound)
        if not responded:
            log.warning("indi       no response to the slew command within %.0f s", bound)
        while self._indi.state("EQUATORIAL_EOD_COORD") == STATE_BUSY:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(abort_event.wait(), 1.0)
            if abort_event.is_set() or self._aborted:
                await self.stop_motion()
                self._record("error", ra_j2000=ra, dec_j2000=dec, note="aborted mid-slew")
                raise InterruptedError("slew aborted")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(abort_event.wait(), self._settle_time)

        # An abort (via pyobs's abort_event or a direct stop_motion from
        # another client) ends the Busy state the same way an arrival does.
        # Without this check the loop above falls through and reports success
        # for an aborted slew (observed 2026-08-30).
        if self._aborted or abort_event.is_set():
            self._record("error", ra_j2000=ra, dec_j2000=dec, note="aborted mid-slew")
            raise InterruptedError("slew aborted")

        # Compare where it landed to where it was sent.
        nums = self._indi.numbers("EQUATORIAL_EOD_COORD")
        if nums and "RA" in nums:
            got_ra, got_dec = eod_to_j2000(nums["RA"], nums["DEC"])
            off = SkyCoord(ra=got_ra * u.deg, dec=got_dec * u.deg).separation(
                SkyCoord(ra=ra * u.deg, dec=dec * u.deg)).arcsec
            # Stopping far short of the target is a failure regardless of
            # status: could be a limit, a refusal, or an untracked stop.
            if off > self._arrival_tolerance:
                self._record("error", ra_j2000=ra, dec_j2000=dec,
                             ra_reported_h=nums["RA"], dec_reported=nums["DEC"],
                             arcsec_off=float(off), note="stopped short of the target")
                raise IndiError(
                    f"mount stopped {off / 3600:.2f} deg from the target; "
                    f"it did not arrive")
            self._record("arrived", ra_j2000=ra, dec_j2000=dec,
                         ra_reported_h=nums["RA"], dec_reported=nums["DEC"],
                         arcsec_off=float(off))

    async def _move_altaz(self, alt: float, az: float, abort_event: asyncio.Event) -> None:
        raise NotImplementedError("alt/az moves are not implemented yet")

    async def _set_tracking_rate(self, ra_rate: float, dec_rate: float) -> None:
        raise NotImplementedError("tracking rates are not implemented yet")

    async def _wait_until_still(self, timeout: float) -> bool:
        """Wait until the reported position stops changing.

        Judged by position, not by any vector's Busy state, since which
        vector goes Busy during a park is driver-specific.

        "Still" means within STILL_TOLERANCE between reads, not identical:
        even a parked mount creeps in RA at the sidereal rate (~15 arcsec/s,
        reported at full precision), so exact comparison never matches and
        would hang here for the full timeout. Two consecutive close readings
        count as settled. Returns False on timeout or lost contact.
        """
        last: SkyCoord | None = None
        stable = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(POSITION_INTERVAL)
            nums = self._indi.numbers("EQUATORIAL_EOD_COORD")
            if not nums or "RA" not in nums:
                return False               # contact lost
            if self._aborted:
                return False               # stopped externally
            here = SkyCoord(ra=nums["RA"] * u.hourangle, dec=nums["DEC"] * u.deg)
            if last is not None and here.separation(last).arcsec < STILL_TOLERANCE:
                stable += 1
            else:
                stable = 0
            last = here
            if stable >= 2:
                return True
        return False

    async def _settled_on_switch(self, prop: str, element: str,
                                 escape: Any = None) -> bool:
        """Wait for a switch to reach `element`; re-request it if nothing arrives.

        INDI publishes on change only, so a no-op command (unpark on an
        already-unparked mount, etc.) yields a driver message but no property
        update. If our cached switch value is stale, waiting for an update
        would hang; hence: wait half the budget, refresh the property, wait
        the rest. Found via repeated park/init commands, 2026-08-30.
        """
        def done() -> bool:
            return (self._indi.switch_on(prop) == element
                    or bool(escape and escape()))

        if await self._indi.wait_until(done, UNPARK_TIMEOUT / 2):
            return True
        await self._indi.refresh(prop)
        return await self._indi.wait_until(done, UNPARK_TIMEOUT / 2)


    async def _on_connected(self) -> None:
        """Runs on every connect and reconnect (INDI on_connected hook)."""
        await self._sync_site_and_time()
        await self._readopt_target_after_reconnect()

    async def _readopt_target_after_reconnect(self) -> None:
        """Re-arm the meridian-flip watcher after a restart that cleared the target.

        The flip watcher only fires when it knows its target, and _target is
        held in memory only -- a module restart resets it to None. A module
        restarted mid-track therefore forgets what it is following and never
        flips it: the mount tracks past the meridian un-flipped until someone
        re-commands the target. Measured on a live AM3N 2026-09-02, when
        restarting the module for a version bump silently disarmed the flip.

        If the mount is tracking but we hold no target, adopt its current
        position as the target. A tracking mount is locked on its target, so
        its position IS the target to within tracking drift -- close enough to
        decide a meridian crossing. Only tracking qualifies: a slew in progress
        has an unknown destination, and idle/parked has no target to flip.
        """
        if self._target is not None or not self._auto_flip:
            return
        if self._indi.switch_on("TELESCOPE_TRACK_STATE") != "TRACK_ON":
            return
        if self._position_radec is None:
            await self._publish_position()      # fill _cached from the mount
        pos = self._position_radec
        if pos is not None:
            self._target = pos
            log.info("indi       tracking on reconnect but no target held; adopted "
                     "current position %.4f %.4f -- meridian-flip watch armed",
                     pos[0], pos[1])

    async def _sync_site_and_time(self) -> None:
        """Send site coordinates and UTC to the mount.

        A real mount computes all pointing from these; a restarted driver
        resets to LAT=0/LONG=0 and (on ZWO firmware) the year 2000, so this
        runs on every connect and reconnect. Longitude goes out in INDI's
        east-positive 0..360 convention (a site at -104.02 west is sent as
        255.98). TIME_UTC is a text vector: ISO-8601 UTC plus the local
        offset in hours.
        """
        if self._observer is None:
            log.warning("indi       no site configured; not sending GEOGRAPHIC_COORD")
            return
        loc = self._observer.location
        # Event-driven, per INDI client convention: no timeout on property
        # definition. The real mount's handshake took ~16 s (2026-08-31) and a
        # fixed wait gave up just as the property arrived. Wait as long as the
        # link is alive; a dead link aborts, and the reconnect hook re-runs
        # this anyway.
        ok = await self._indi.wait_until(
            lambda: self._indi.state("GEOGRAPHIC_COORD") is not None
                    or not self._indi.connected, 600.0)
        if not ok or not self._indi.connected:
            log.warning("indi       driver never offered GEOGRAPHIC_COORD; site not sent")
            return
        lon = float(loc.lon.deg) % 360.0
        await self._indi.set_numbers("GEOGRAPHIC_COORD", {
            "LAT": float(loc.lat.deg), "LONG": lon,
            "ELEV": float(loc.height.value)})
        with contextlib.suppress(asyncio.TimeoutError):
            await self._indi.wait_for("TIME_UTC", 30.0)
            now = datetime.now(timezone.utc)
            offset = 0.0
            tz = getattr(self._observer, "timezone", None)
            if tz is not None:
                delta = datetime.now(tz).utcoffset()
                if delta is not None:
                    offset = delta.total_seconds() / 3600.0
            await self._indi.set_text("TIME_UTC", {
                "UTC": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "OFFSET": f"{offset:g}"})
        log.info("indi       sent site (lat %.4f, long %.4f east, elev %.0f m) and UTC",
                 float(loc.lat.deg), lon, float(loc.height.value))

    def _tracking_mode_now(self) -> TrackingMode:
        """Current tracking mode according to the cached switches."""
        if self._indi.switch_on("TELESCOPE_TRACK_STATE") != "TRACK_ON":
            return TrackingMode.OFF
        mode = self._indi.switch_on("TELESCOPE_TRACK_MODE") or ""
        return {"TRACK_SOLAR": TrackingMode.SOLAR,
                "TRACK_LUNAR": TrackingMode.LUNAR}.get(mode, TrackingMode.SIDEREAL)

    async def set_tracking_mode(self, mode: TrackingMode, **kwargs: Any) -> None:
        """Set the tracking rate, or stop tracking (mode OFF).

        OFF matters because INDI's abort restores the pre-move state: an
        aborted mount typically resumes tracking, and TRACK_OFF is the only
        command that makes it hold still.
        """
        if mode not in self._tracking_modes:
            raise exc.InvalidArgumentError(f"Mode {mode} not supported.")
        if mode == TrackingMode.OFF:
            await self._indi.set_switch("TELESCOPE_TRACK_STATE", "TRACK_OFF")
            ok = await self._settled_on_switch("TELESCOPE_TRACK_STATE", "TRACK_OFF")
        else:
            await self._indi.set_switch("TELESCOPE_TRACK_MODE", f"TRACK_{mode.upper()}")
            await self._indi.set_switch("TELESCOPE_TRACK_STATE", "TRACK_ON")
            ok = await self._settled_on_switch("TELESCOPE_TRACK_STATE", "TRACK_ON")
        if not ok:
            # Not fatal. The AM5 firmware refuses TRACK_ON while the mount
            # sits past the meridian un-flipped (measured 2026-08-31), and
            # pyobs resets tracking before every slew -- so raising here
            # killed the very goto that performs the flip and cures the
            # refusal. Warn, report the mount's actual state, carry on.
            log.warning("indi       mount did not confirm tracking mode %s; "
                        "continuing (a goto restores tracking after a "
                        "meridian flip)", mode)
            await self.comm.set_state(
                ITrackingMode, TrackingModeState(mode=self._tracking_mode_now()))
            return
        await self.comm.set_state(ITrackingMode, TrackingModeState(mode=mode))

    async def stop_motion(self, device: str | None = None, **kwargs: Any) -> None:
        # Record the abort: inside _move_radec an abort and an arrival are
        # otherwise indistinguishable (both end the Busy state), and an
        # aborted slew must not be reported as a success.
        self._aborted = True
        # A stopped mount must stay stopped: clear the target and latch the
        # watcher off, so a flip goto already in flight cannot re-arm it by
        # re-setting _target. It comes back on the next deliberate slew.
        self._target = None
        self._flip_disarmed = True
        await self._indi.set_switch("TELESCOPE_ABORT_MOTION", "ABORT")
        await self._change_motion_status(await self._get_status())

    async def init(self, **kwargs: Any) -> None:
        """Unpark, waiting on the park switch (not on motion).

        Unpark does not move the mount, so "stopped moving" is the wrong
        completion signal; reading status immediately after the command still
        showed parked (2026-08-30). The park switch is the real signal.
        """
        await self._change_motion_status(MotionStatus.INITIALIZING)
        await self._indi.set_switch("TELESCOPE_PARK", "UNPARK")
        if not await self._settled_on_switch("TELESCOPE_PARK", "UNPARK"):
            log.warning("indi       still reads as parked %.0f s after unparking "
                        "(last driver messages: %s)", UNPARK_TIMEOUT,
                        "; ".join(self._indi.messages[-3:]) or "none")
        await self._change_motion_status(await self._get_status())

    async def park(self, **kwargs: Any) -> None:
        """Park, and only report done once the mount has stopped moving.

        Requires both signals: the park switch (command accepted) and the
        position settling (movement finished). The switch alone flips ~18 s
        before the mount arrives (measured 2026-08-30), so trusting it would
        report a park position the mount had not reached. The park is also
        abortable mid-swing, and an aborted park raises instead of reporting
        success.
        """
        self._aborted = False
        self._parking = True
        self._target = None      # a parked mount has nothing to flip to
        try:
            await self._change_motion_status(MotionStatus.PARKING)
            await self._indi.set_switch("TELESCOPE_PARK", "PARK")
            if not await self._settled_on_switch("TELESCOPE_PARK", "PARK",
                                                 lambda: self._aborted):
                log.warning("indi       the mount did not accept the park command")
            if not await self._wait_until_still(MOVE_TIMEOUT) and not self._aborted:
                log.warning("indi       park did not settle within %.0f s; "
                            "the mount may still be moving", MOVE_TIMEOUT)
        finally:
            self._parking = False
        if self._aborted:
            self._record("error", note="park aborted")
            raise InterruptedError("park aborted")
        await self._change_motion_status(await self._get_status())


__all__ = ["IndiTelescope", "eod_to_j2000", "j2000_to_eod"]
