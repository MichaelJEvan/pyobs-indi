#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    One-shot: set the simulator's park position to the pole (HA 0, Dec +90)
    and persist it to ParkData.xml in the VM.

    Rationale: with no ParkData.xml the simulator defaults to PARK_HA -6 /
    PARK_DEC 0 (tube horizontal). The INDI AM5 driver, which also drives the
    AM3, redirects park() to goHome() -- counterweight down, pointing at the
    celestial pole; so the simulator is set to match the real mount.

    Park is stored as hour angle + declination because it is a mechanical
    position, independent of sidereal time. Dec +90 qualifies; Polaris does
    not (it drifts in HA, and sits 0.74 deg from the pole).

    Note: a parked mount rejects a new park position, so this unparks first.

    Usage: python set_park_position.py [--ha 0 --dec 90]

###############################################################################################"""

import argparse
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from pyobs_indi.device import IndiDevice          # noqa: E402

DEVICE = "Telescope Simulator"
HOST, PORT = "ubuntu.orb.local", 7624


async def run(host: str, port: int, device: str,
              ha: float, dec: float) -> int:
    dev = IndiDevice(host, device, port)
    await dev.open()
    if not dev.connected:
        print(f"  cannot reach {device} on {host}:{port}")
        return 1

    # Wait for the properties to actually arrive before reading them. open()
    # only waits for the coordinates, so reading the park properties straight
    # afterwards raced them: the first run of this script saw None for both,
    # skipped the unpark on that basis, and wrote the *old* values to disk.
    for prop in ("TELESCOPE_PARK", "TELESCOPE_PARK_POSITION"):
        try:
            await dev.wait_for(prop, 10.0)
        except asyncio.TimeoutError:
            print(f"  {device} never published {prop}")
            return 1

    before = dev.numbers("TELESCOPE_PARK_POSITION") or {}
    print(f"  before : PARK_HA {before.get('PARK_HA')}  PARK_DEC {before.get('PARK_DEC')}")

    if dev.switch_on("TELESCOPE_PARK") == "PARK":
        print("  mount is parked; unparking first")
        await dev.set_switch("TELESCOPE_PARK", "UNPARK")
        await asyncio.sleep(2.0)

    await dev.set_numbers("TELESCOPE_PARK_POSITION",
                          {"PARK_HA": ha, "PARK_DEC": dec})
    await asyncio.sleep(1.0)

    # Without this it lasts until the driver restarts, which is exactly the
    # state that produced the wrong park in the first place.
    await dev.set_switch("TELESCOPE_PARK_OPTION", "PARK_WRITE_DATA")
    await asyncio.sleep(1.5)

    after = dev.numbers("TELESCOPE_PARK_POSITION") or {}
    print(f"  after  : PARK_HA {after.get('PARK_HA')}  PARK_DEC {after.get('PARK_DEC')}")
    for line in dev.messages[-5:]:
        print(f"    driver: {line}")
    await dev.close()

    ok = (abs(after.get("PARK_HA", 99) - ha) < 0.01
          and abs(after.get("PARK_DEC", 99) - dec) < 0.01)
    print("  " + ("set, and written to ParkData.xml." if ok
                  else "NOT set; is the mount still parked?"))
    return 0 if ok else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=HOST)
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--device", default=DEVICE)
    p.add_argument("--ha", type=float, default=0.0, help="park hour angle, hours")
    p.add_argument("--dec", type=float, default=90.0, help="park declination, degrees")
    a = p.parse_args()
    sys.exit(asyncio.run(run(a.host, a.port, a.device, a.ha, a.dec)))
