#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    The epoch conversion, which nothing else in the chain will catch.

    Run: python tests/test_epoch.py

###############################################################################################"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from pyobs_indi.telescope import eod_to_j2000, j2000_to_eod


def _ra_apart(a: float, b: float) -> float:
    """Arcseconds between two right ascensions, the short way round."""
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d) * 3600


def test_round_trip_is_exact():
    """Whatever we send must be what we read back."""
    for ra, dec in [(0.0, 0.0), (37.95456, 89.26411), (279.2347, 38.7837),
                    (180.0, -45.0), (359.9, -0.1)]:
        h, d = j2000_to_eod(ra, dec)
        back_ra, back_dec = eod_to_j2000(h, d)
        assert _ra_apart(back_ra, ra) < 0.01, f"RA drifted at {ra},{dec}"
        assert abs(back_dec - dec) * 3600 < 0.01, f"Dec drifted at {ra},{dec}"


def test_ra_comes_back_in_hours_not_degrees():
    """INDI wants hours. Returning degrees is a 15x error that still looks like a coordinate, which is exactly how it would survive review."""
    h, _ = j2000_to_eod(279.2347, 38.7837)     # Vega, 18.6h
    assert 0.0 <= h < 24.0, f"RA {h} is not in hours"
    assert 18.0 < h < 19.0, f"RA {h} is not near Vega's 18.6 h"


def test_the_conversion_actually_does_something():
    """A no-op conversion would pass a round-trip test perfectly."""
    h, d = j2000_to_eod(279.2347, 38.7837)
    moved = abs(h * 15 - 279.2347) * 3600
    assert moved > 300, f"RA moved only {moved:.0f} arcsec; conversion is a no-op?"


def test_the_ra_term_scales_with_declination():
    """Near the pole the RA correction is enormous, and that is correct. RA precession carries a tan(dec) term, so at Dec +89 the shift is tens of minutes of RA while Dec moves only arcminutes."""
    h_eq, _ = j2000_to_eod(37.95456, 0.0)          # same RA, on the equator
    h_pole, _ = j2000_to_eod(37.95456, 89.26411)   # Polaris
    shift_eq = abs(h_eq * 3600 - (37.95456 / 15) * 3600)
    shift_pole = abs(h_pole * 3600 - (37.95456 / 15) * 3600)
    assert shift_pole > 20 * shift_eq, (
        f"pole shift {shift_pole:.0f}s is not much larger than equatorial "
        f"{shift_eq:.0f}s; is the tan(dec) term missing?")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
