# pyobs-indi

A pyobs 2.0 telescope module that drives a mount over **INDI**, so a Linux
observatory can use amateur hardware. Nothing in pyobs covers this today: the
core team runs professional control systems (`pyobs-pilar`, `pyobs-brot`), and
the 2022 pyobs paper itself notes there is no INDI wrapper and that one would
be an easy way to add hardware.

**Not published until it works.** Local repo only.

## Why this, and why now

The only gap between the current setup and a working observatory is the mount.
`pyobs-asi` covers the ASI585, `pyobs-zwoeaf` covers the EAF, `IAutoGuiding` is
in core. Neither the ZWO AM3N nor the Sirius EQ-G has a pyobs driver, and the
only easy Alpaca path for them runs through ASCOM Remote, which means Windows.
INDI is the Linux-native answer.

pyobs 2.0 makes this the right moment: INDI *pushes* property updates and
pyobs 2.0 *publishes* state, so the module is cache-to-cache. Against 1.54 it
would have been push-to-pull and rewritten within weeks.

## Precedent to follow

`pyobs-alpaca` is the closest existing module, and the structure to mirror:

    pyobs_alpaca/
      device.py      the protocol client, no pyobs in it
      telescope.py   AlpacaTelescope(BaseTelescope, ...) -- the pyobs mapping

`AlpacaTelescope` is roughly 280 lines and implements:

    open, _get_status, _update_position, init, park,
    _move_radec, _move_altaz, _set_tracking_rate,
    set_offsets_radec, stop_motion, sync_target,
    get_fits_header_before

`_update_position` is the important one -- it calls
`comm.set_state(IPointingRaDec, RaDecState(ra=..., dec=...))`, which is what
every client (including the Stellarium bridge) reads.

Reference clone kept at `~/Development/Pyobs/pyobs-alpaca-reference/`.

## The six INDI properties

Everything else a driver announces is ignored.

| property | used for |
|---|---|
| `CONNECTION` | connect/disconnect the driver |
| `EQUATORIAL_EOD_COORD` | current position, and slew by writing it |
| `ON_COORD_SET` | whether a write means TRACK, SLEW or SYNC |
| `TELESCOPE_ABORT_MOTION` | stop |
| `TELESCOPE_PARK` | park / unpark |
| `TELESCOPE_TRACK_STATE` | tracking on/off |

Verified present on `indi_simulator_telescope` (INDI 1.9.9), 2026-08-27.

Also available and worth knowing about:

- `TARGET_EOD_COORD` -- where the mount is *going*. pyobs defines
  `MoveRaDecEvent` for this but never delivers it (nothing registers the event
  type, measured 2026-08-27), so publishing the target as state from here would
  fill a gap nothing else fills.
- `GEOGRAPHIC_COORD`, `TIME_UTC` -- site location and clock, from the mount.
- `TELESCOPE_PIER_SIDE`, `TELESCOPE_SLEW_RATE`, `TELESCOPE_TRACK_MODE/RATE`,
  timed guiding -- later, if wanted.

## Traps found before writing any code

**A driver advertises almost nothing until it is connected.** Before
`CONNECTION`, `indi_simulator_telescope` exposes 28 properties -- port, baud,
mount type, debug -- and none of the six. After connecting, 42, including all
six. Any client must set `CONNECTION` first and re-read.

**`EQUATORIAL_EOD_COORD` is equinox-of-date.** pyobs and the Stellarium
protocol are J2000. Precession conversion is needed in both directions. Easy
with astropy, easy to forget, and the error grows slowly enough to look like a
pointing-model problem rather than a bug.

## Spike result (2026-08-27) -- the INDI half is proven

A throwaway raw-socket script connected, set `ON_COORD_SET` to TRACK, wrote
`EQUATORIAL_EOD_COORD`, and watched the simulated mount slew from the pole to
RA 20h41m / Dec +45 in nine seconds. So driving a mount over INDI works, and
the protocol is small enough to speak directly.

What that run taught, all of which the module has to get right:

- **RA is in HOURS, Dec in degrees.** A 15x error otherwise.
- **The vector's `state` attribute is the motion status.** `Idle` before a
  slew, `Busy` during, `Ok` on arrival -- maps straight onto pyobs's
  `MotionStatus`. No separate property needed.
- **Position streams at about 1 Hz while slewing**, which is exactly what the
  Stellarium bridge wants and better than pyobs's own dummy managed before
  2.0.1.
- **The driver sends `<message>` elements** with human-readable text
  ("Slewing to RA: 20:40:55 - DEC: 45:00:00", "Telescope slew is complete.
  Tracking..."). Free log lines.
- **An out-of-range target is silently ignored.** Asking for Dec +105 produced
  no error, no message, no state change -- the mount just sat there. Same shape
  as the existing "a parked telescope ignores slews silently" rule, and it
  looked exactly like a dead protocol rather than a bad number. **The module
  must validate before sending, and must not report success on silence.**

### Found while writing `device.py`

**A `defVector` carries `<defNumber>`/`<defSwitch>` children; a `setVector`
carries `<oneNumber>`/`<oneSwitch>`.** Parsing only the `one` form gives you
every property name and not one value, which looks like a driver that
announces itself and then says nothing. Both forms have to be matched.

`GEOGRAPHIC_COORD` on the simulator is 0/0/0 until set -- fine for RA/Dec, but
alt/az from the driver would be meaningless until the site is configured.

### Verified working (2026-08-27, in Michael's own terminals)

`pyobs-gui` -> pyobs -> `IndiTelescope` -> INDI -> `indi_simulator_telescope`,
commanded and watched live:

    Moving telescope to RA=18:36:56.336 (279.23473 deg)     J2000, as asked
    indi [INFO] Slewing to RA: 18:37:50 - DEC: 38:48:28     equinox of date
    indi [INFO] Telescope slew is complete. Tracking...
    Reached destination

Same star, 54 seconds of RA apart -- the precession correction visible in the
log. Position reads back as J2000 in the GUI, motion status follows the INDI
vector state.

Two bugs found by running it rather than by reading:

- **`IPointingRaDec` must be declared explicitly.** `BaseTelescope` provides
  only `ITelescope` and `IFitsHeaderBefore`. Without naming it, the module
  publishes `RaDecState` that nothing looks for: pyobs-gui showed position as
  N/A and offered no RA/Dec entry -- correctly reflecting what the module
  claimed about itself. `_DummyTelescopeBase` declares it the same way.
- **The site in the config is load-bearing.** With `location: 0/0/0` the
  altitude limit refused Polaris at alt 0.57 deg. Correct behaviour, confusing
  error, and nothing to do with INDI.

Still missing: alt/az pointing, tracking rates, offsets, and any tests.

## Steps

Each step ends somewhere testable.

1. **Skeleton.** `pyproject.toml`, `pyobs_indi/{__init__,device,telescope}.py`,
   `tests/`. Mirrors pyobs-alpaca.
2. **`device.py`.** Connect, `getProperties`, parse the `def*Vector` /
   `set*Vector` stream into a property cache, send `newNumberVector` /
   `newSwitchVector`. No pyobs involved -- testable on its own against the
   simulator.
3. **Connect handshake.** Set `CONNECTION`, wait for the mount properties to
   appear, fail clearly if they do not.
4. **Epoch conversion.** EOD <-> J2000, both directions, with tests.
5. **`telescope.py`.** `IndiTelescope(BaseTelescope)` implementing the methods
   listed above against the six properties.
6. **Wire it up.** A sixth XMPP account in the container's `CTL_ON_START`, a
   `telcam.yaml` entry, running alongside the dummy telescope.
7. **Verify with the existing suite.** bridge.py, nightwatch.py, scope.py and
   Stellarium, all unchanged -- to them it is just another telescope module.

## Development environment

- **indiserver** runs in the OrbStack `ubuntu` machine (arm64):
  `indiserver -v indi_simulator_telescope`, port **7624**, reachable from the
  Mac at `ubuntu.orb.local:7624`.
- **This module** runs on the Mac in the `pyobs-2.0` conda env, talking to
  ejabberd on `localhost:5222` and indiserver on `ubuntu.orb.local:7624`.
- Ubuntu ships **INDI 1.9.9**; upstream is 2.x. Fine here -- the core protocol
  and the six standard properties are unchanged. The INDI PPA is only needed if
  a current driver is wanted for the AM3N specifically.

## Eventual deployment

Pi at the mount running indiserver plus pyobs-indi, pyobs-asi and pyobs-zwoeaf;
ejabberd, scheduler and archive indoors; Stellarium and the bridge on the Mac.
Do not let indiserver and pyobs-asi claim the same USB camera.
