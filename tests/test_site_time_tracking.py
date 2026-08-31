#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    Site, time, and the tracking switch; the last gaps before real metal.

    Run: python tests/test_site_time_tracking.py

###############################################################################################"""

import asyncio
import pathlib
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import astropy.units as u
from astropy.coordinates import EarthLocation

from pyobs.interfaces.ITrackingMode import TrackingMode
from pyobs.utils import exceptions as exc
from pyobs_indi import telescope as tel_mod

from test_motion_commands import FakeIndi, _scope, _noop_status  # noqa: E402


class FakeIndiTracking(FakeIndi):
    """FakeIndi that really flips its tracking switches and takes text."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.text_sent: dict[str, dict[str, str]] = {}
        self.switches = {"TELESCOPE_TRACK_STATE": "TRACK_ON",
                         "TELESCOPE_TRACK_MODE": "TRACK_SIDEREAL"}
        self.props = {"GEOGRAPHIC_COORD", "TIME_UTC",
                      "TELESCOPE_TRACK_STATE", "TELESCOPE_TRACK_MODE"}

    def switch_on(self, prop: str):
        if prop in self.switches:
            return self.switches[prop]
        return super().switch_on(prop)

    async def set_switch(self, prop: str, element: str) -> None:
        await super().set_switch(prop, element)
        if prop == "TELESCOPE_TRACK_STATE":
            self.switches[prop] = element
        if prop == "TELESCOPE_TRACK_MODE":
            self.switches[prop] = element

    async def set_text(self, prop: str, values: dict[str, str]) -> None:
        self.text_sent[prop] = values

    async def wait_for(self, prop: str, timeout: float) -> None:
        if prop not in self.props:
            raise asyncio.TimeoutError()


def _rig():
    t = _scope()
    t._indi = FakeIndiTracking()
    t._change_motion_status = _noop_status
    t._tracking_modes = [TrackingMode.SIDEREAL, TrackingMode.SOLAR,
                         TrackingMode.LUNAR, TrackingMode.OFF]
    t._observer = SimpleNamespace(
        # McDonald Observatory: a public site, same one the container's
        # _environment.example.yaml uses. Never the real site here; this
        # file is destined for a public repo.
        location=EarthLocation(lat=30.6797 * u.deg, lon=-104.0247 * u.deg,
                               height=2077 * u.m),
        timezone=None)
    calls = []
    async def set_state(iface, state): calls.append(("state", iface, state))
    async def set_caps(iface, caps): calls.append(("caps", iface, caps))
    t._comm = SimpleNamespace(set_state=set_state, set_capabilities=set_caps)
    return t, calls


def test_longitude_goes_on_the_wire_east_positive() -> None:
    """INDI wants 0..360 east; a western-hemisphere site is written negative."""
    t, _ = _rig()
    asyncio.run(t._sync_site_and_time())
    sent = t._indi.numbers_sent["GEOGRAPHIC_COORD"]
    assert abs(sent["LONG"] - (360.0 - 104.0247)) < 0.001, sent
    assert abs(sent["LAT"] - 30.6797) < 0.001, sent
    assert abs(sent["ELEV"] - 2077.0) < 0.01, sent


def test_time_is_sent_as_text() -> None:
    """TIME_UTC is a text vector on the wire; measured, not assumed."""
    t, _ = _rig()
    asyncio.run(t._sync_site_and_time())
    sent = t._indi.text_sent["TIME_UTC"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", sent["UTC"]), sent
    assert "OFFSET" in sent


def test_no_site_means_nothing_sent() -> None:
    """Better to send nothing than to send zeros with confidence."""
    t, _ = _rig()
    t._observer = None
    asyncio.run(t._sync_site_and_time())
    assert "GEOGRAPHIC_COORD" not in t._indi.numbers_sent


def test_tracking_off_actually_stops_tracking() -> None:
    """The half of a stop that abort cannot do."""
    t, calls = _rig()
    asyncio.run(t.set_tracking_mode(TrackingMode.OFF))
    assert ("TELESCOPE_TRACK_STATE", "TRACK_OFF") in t._indi.sent
    assert t._indi.switches["TELESCOPE_TRACK_STATE"] == "TRACK_OFF"
    states = [c for c in calls if c[0] == "state"]
    assert states and states[-1][2].mode == TrackingMode.OFF


def test_lunar_sets_the_mode_and_turns_tracking_on() -> None:
    t, _ = _rig()
    t._indi.switches["TELESCOPE_TRACK_STATE"] = "TRACK_OFF"
    asyncio.run(t.set_tracking_mode(TrackingMode.LUNAR))
    assert ("TELESCOPE_TRACK_MODE", "TRACK_LUNAR") in t._indi.sent
    assert t._indi.switches["TELESCOPE_TRACK_STATE"] == "TRACK_ON"


def test_an_unsupported_mode_is_refused_loudly() -> None:
    t, _ = _rig()
    t._tracking_modes = [TrackingMode.SIDEREAL, TrackingMode.OFF]
    try:
        asyncio.run(t.set_tracking_mode(TrackingMode.LUNAR))
    except exc.InvalidArgumentError:
        return
    raise AssertionError("accepted a mode the mount does not have")


def test_current_mode_reads_off_the_switches() -> None:
    t, _ = _rig()
    assert t._tracking_mode_now() == TrackingMode.SIDEREAL
    t._indi.switches["TELESCOPE_TRACK_MODE"] = "TRACK_LUNAR"
    assert t._tracking_mode_now() == TrackingMode.LUNAR
    t._indi.switches["TELESCOPE_TRACK_STATE"] = "TRACK_OFF"
    assert t._tracking_mode_now() == TrackingMode.OFF


if __name__ == "__main__":
    tel_mod.POSITION_INTERVAL = 0.01
    tel_mod.UNPARK_TIMEOUT = 1.0
    tel_mod.RESPONSE_TIMEOUT = 0.3
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
