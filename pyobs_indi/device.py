"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    Minimal asyncio INDI client.

    Implements only what a telescope module needs (about six properties), not the
    full protocol. No pyobs dependencies, so it can be tested standalone against
    indi_simulator_telescope.

    Two protocol behaviors worth knowing (observed 2026-08-27):
    - A driver defines most of its properties only after CONNECTION is set.
    The simulator goes from 28 properties to 42 on connect.
    - Out-of-range targets are ignored silently: no error, no message, no state
    change. Callers cannot assume a command worked just because nothing
    complained.

###############################################################################################"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_PORT = 7624
CONNECT_TIMEOUT = 10.0
PROPERTY_TIMEOUT = 10.0     # wait for mount properties to appear after connect
RECONNECT_DELAY = 2.0       # delay between reconnect attempts
HEARTBEAT = 2.0             # send a probe after this much silence
SILENCE_TIMEOUT = 6.0       # declare the link dead after this much silence
# Timeout rationale: a false positive costs one automatic reconnect; a slow
# detection means the cached position stays wrong longer (~6 deg of sky per
# second at the simulator's measured slew rate). Values were tuned against a
# local VM where replies are near-instant; re-check them on a real serial or
# WiFi link, which may legitimately take seconds to respond.

# INDI vector state doubles as motion status:
# Idle before a slew, Busy during, Ok on arrival, Alert on failure.
STATE_IDLE, STATE_BUSY, STATE_OK, STATE_ALERT = "Idle", "Busy", "Ok", "Alert"

_VECTOR = re.compile(
    r'<(def|set)(\w+?)Vector\s+([^>]*?)>(.*?)</(?:def|set)\2Vector>', re.S)
_ATTR = re.compile(r'(\w+)="([^"]*)"')
# Match both defXXX and oneXXX children: definitions carry the initial value,
# updates carry the rest.
_ONE = re.compile(r'<(?:one|def)(?:Number|Switch|Text|Light)\s+[^>]*?name="([^"]+)"[^>]*>\s*([^<]*?)\s*</(?:one|def)', re.S)


class IndiError(Exception):
    """Error reported by, or inferred from, the INDI driver."""


def _why(err: BaseException) -> str:
    """Human-readable reason for a connect failure.

    asyncio.TimeoutError stringifies to an empty string, which makes for
    useless log lines, so name it explicitly.
    """
    if isinstance(err, asyncio.TimeoutError):
        return f"timed out after {CONNECT_TIMEOUT:.0f} s"
    return str(err) or type(err).__name__


class IndiDevice:
    """One INDI device on one indiserver, with a local property cache."""

    def __init__(self, host: str, device: str, port: int = DEFAULT_PORT) -> None:
        self._host, self._port, self._device = host, port, device
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        # name -> {"state": str, "values": {element: str}}
        self._props: dict[str, dict[str, Any]] = {}
        self._changed = asyncio.Event()
        self.messages: list[str] = []
        # Count of error messages this device has emitted. A driver that has
        # lost contact with its mount floods these ("Serial read error",
        # "Error reading RA/DEC" -- measured on the AM5, 2026-09-03); the
        # liveness check uses a rising count to tell a dead link from a mount
        # that is simply holding a static position.
        self._error_count = 0
        # Optional async callback, invoked when the mount is (back) in
        # contact: after the first socket connect, and whenever the driver's
        # CONNECTION switch turns On (see _absorb). Used by the telescope
        # layer to re-send site and time: a restarted driver resets to
        # LAT=0/LONG=0 and a power-cycled ZWO to the year 2000, and must be
        # reconfigured each time the link comes back. The CONNECTION edge is
        # the trigger that matters -- a serial-only recovery (mount power
        # cycle, 2026-09-03) never touches the socket, and a hook tied to
        # socket connects missed it, leaving the mount's clock at Jan 2000.
        self.on_connected: Any = None
        self._announcing = False

    # -- connection ------------------------------------------------------

    @property
    def device(self) -> str:
        """The INDI device name this client is bound to (e.g. 'ZWO AM5')."""
        return self._device

    @property
    def error_count(self) -> int:
        """Cumulative count of error messages this driver has emitted."""
        return self._error_count

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def open(self) -> None:
        """Connect and start the background reader.

        A failed first connect is not fatal: the reader task keeps retrying,
        and the module comes up reporting "unknown" until the mount appears.
        Startup order should not matter (seen 2026-08-30: module started 21 s
        before indiserver and previously died on the unresolved hostname).
        """
        reached = True
        try:
            await self._connect()
        except (OSError, asyncio.TimeoutError) as err:
            log.warning("indi       cannot reach %s:%d yet (%s); will keep trying",
                        self._host, self._port, _why(err))
            reached = False
        self._task = asyncio.create_task(self._run_forever())
        if reached:
            await self._announce_connected()
        if not reached:
            # Don't block startup waiting on a timeout we know will expire.
            return
        try:
            await self.wait_for("EQUATORIAL_EOD_COORD", PROPERTY_TIMEOUT)
        except asyncio.TimeoutError:
            # Likely a wrong device name; log the hint but keep waiting.
            log.warning("indi       %r has not published EQUATORIAL_EOD_COORD in %.0f s; "
                        "still waiting; check the device name if this persists",
                        self._device, PROPERTY_TIMEOUT)
            return
        log.info("indi       connected to %r on %s:%d", self._device, self._host, self._port)


    async def _announce_connected(self) -> None:
        if self.on_connected is None:
            return
        if self._announcing:
            # Overlapping fires coalesce: at startup the open() announce is
            # usually still waiting for the driver's handshake when the
            # CONNECTION-On edge lands, and that one running hook reads
            # current state and completes the same sync a second run would.
            return
        self._announcing = True
        try:
            await self.on_connected()
        except Exception as err:
            # A failing hook must not take down the read loop.
            log.warning("indi       on_connected hook failed: %s: %s",
                        type(err).__name__, err)
        finally:
            self._announcing = False

    async def _connect(self) -> None:
        """Single connect attempt: open socket, request properties, connect driver.

        getProperties is required here. INDI only publishes on change, so a
        client that skips it gets nothing from an idle mount.
        """
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), CONNECT_TIMEOUT)
        await self._send('<getProperties version="1.7"/>')
        # CONNECTION is switched on when the driver defines it -- see
        # _absorb. Doing it here raced the definition (and a wait here
        # would deadlock: _connect runs on the same task that reads).

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._forget()

    # -- reading ---------------------------------------------------------

    async def _run_forever(self) -> None:
        """Reader loop: pump until the link dies, clear state, reconnect.

        An earlier version returned on disconnect, leaving the cache full of
        stale values that every accessor kept serving. Clearing and
        reconnecting is the fix for that class of bug.
        """
        while True:
            if self._reader is None:
                try:
                    await self._connect()
                    log.info("indi       reconnected to %r; re-asked for every property",
                             self._device)
                    # No hook here. It fires from _absorb when the driver's
                    # CONNECTION switch reads On again -- the cache was just
                    # cleared, so the re-asked definitions produce that edge
                    # whether the driver stayed connected or reconnects. A
                    # socket-level fire was both redundant with the edge and
                    # blind to serial-only recoveries (mount power cycle with
                    # the socket healthy throughout, 2026-09-03). An earlier
                    # inline await here also deadlocked against _pump for the
                    # hook's whole timeout (measured 2026-09-03).
                except (OSError, asyncio.TimeoutError) as err:
                    # Each attempt may itself burn CONNECT_TIMEOUT, so these
                    # lines appear every CONNECT_TIMEOUT + RECONNECT_DELAY
                    # seconds, not every RECONNECT_DELAY.
                    log.warning("indi       reconnect failed (%s); next attempt in %.0f s",
                                _why(err), RECONNECT_DELAY)
                    await asyncio.sleep(RECONNECT_DELAY)
                    continue
            try:
                await self._pump()
            except asyncio.CancelledError:
                raise                       # close() cancelling us
            except Exception as err:
                # Catch-all is intentional: if this task dies silently the
                # cache stops updating with no external symptom, which is the
                # failure mode this loop exists to prevent.
                log.warning("indi       read failed: %s: %s", type(err).__name__, err)
            # Link is down; drop cached state before reconnecting.
            self._forget()
            await asyncio.sleep(RECONNECT_DELAY)

    def _forget(self) -> None:
        """Close the connection and clear the property cache.

        The cache must be emptied, not just left to age: stale entries are
        indistinguishable from current ones to callers.
        """
        if self._writer is not None:
            self._writer.close()
        self._reader = self._writer = None
        self._props.clear()
        self._changed.set()             # wake anything blocked in wait_for

    async def _pump(self) -> None:
        """Read the stream into the cache; detect a silently dead peer.

        A clean close is easy (read returns b""). A peer that disappears
        without a FIN (power loss, VM shutdown, WiFi drop) leaves the socket
        ESTABLISHED and the read blocked forever; verified 2026-08-30 by
        stopping the VM mid-link. INDI has no ping, so after HEARTBEAT
        seconds of silence we request one property; the reply proves the link
        is alive. SILENCE_TIMEOUT of total silence means it isn't. Without
        the probe, an idle mount (which publishes nothing) would be
        indistinguishable from a dead link.
        """
        assert self._reader is not None
        loop = asyncio.get_running_loop()
        heard = loop.time()
        buf = ""
        while True:
            try:
                data = await asyncio.wait_for(self._reader.read(65536), HEARTBEAT)
            except asyncio.TimeoutError:
                quiet = loop.time() - heard
                if quiet > SILENCE_TIMEOUT:
                    log.warning("indi       nothing heard for %.0f s; treating the "
                                "link as dead", quiet)
                    return
                # Probe with a single property, not the full table.
                await self._send(f'<getProperties version="1.7" device="{self._device}" '
                                 f'name="EQUATORIAL_EOD_COORD"/>')
                continue
            if not data:
                log.warning("indi       server closed the connection")
                return
            heard = loop.time()
            buf += data.decode("utf-8", "replace")
            # Parse complete vectors; keep any trailing partial for next read.
            last = 0
            for m in _VECTOR.finditer(buf):
                self._absorb(m)
                last = m.end()
            for tag in re.finditer(r'<message\b([^>]*)>', buf[:last]):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', tag.group(1)))
                # Drop messages addressed to another device: several drivers
                # share one indiserver and it broadcasts every driver's
                # messages to all clients, so an unfiltered log mixes devices
                # (a real mount's errors showed up in the sim's log, 2026-09-03).
                # Keep device-less messages (general server notices).
                dev = attrs.get("device")
                if dev and dev != self._device:
                    continue
                msg = attrs.get("message")
                if msg is None:
                    continue
                self.messages.append(msg)
                if "error" in msg.lower():
                    self._error_count += 1
                log.info("indi       %s", msg)
            buf = buf[last:]
            if last:
                self._changed.set()

    def _absorb(self, match: re.Match[str]) -> None:
        defined = match.group(1) == "def"
        attrs = dict(_ATTR.findall(match.group(3)))
        if attrs.get("device") != self._device:
            return
        name = attrs.get("name")
        if not name:
            return
        entry = self._props.setdefault(name, {"state": STATE_IDLE, "values": {},
                                              "rev": 0, "timeout": None})
        entry["rev"] += 1
        if "state" in attrs:
            entry["state"] = attrs["state"]
        # Per the INDI spec, a device is "strongly encouraged to send an
        # accompanying timeout value that specifies the worst-case time it
        # might take to change the value". Keep it so waits can be sized by
        # the driver instead of by constants.
        if "timeout" in attrs:
            try:
                entry["timeout"] = float(attrs["timeout"]) or None
            except ValueError:
                pass
        prev_connect = entry["values"].get("CONNECT") if name == "CONNECTION" else None
        for element, value in _ONE.findall(match.group(4)):
            entry["values"][element] = value
        if name != "CONNECTION":
            return
        connect = entry["values"].get("CONNECT")
        # Connect the device the moment the driver DEFINES CONNECTION --
        # def only, never on set updates. Setting it from _connect() raced
        # the definition ("dispatch error: Property CONNECTION is not
        # defined", Telescope Simulator, 2026-09-01). Reacting to every
        # update was worse: with the serial port gone, each failed CONNECT
        # publishes CONNECTION Off plus an error message, and answering
        # each with another CONNECT looped at same-millisecond speed --
        # the log storm measured on the AM3N power-cycle, 2026-09-03.
        # If this one attempt fails, the telescope layer's paced
        # _auto_reconnect owns the retries. Runs on the reader task, so
        # the send goes out as a task rather than blocking the loop.
        if defined and connect != "On":
            asyncio.get_running_loop().create_task(
                self.set_switch("CONNECTION", "CONNECT"))
        # The mount is (back) in contact when CONNECT turns On -- first
        # definition or an Off->On recovery, including a serial-only one
        # where the socket never blipped (mount power cycle, 2026-09-03:
        # the socket-level hook missed it and the mount kept its cold-boot
        # Jan-2000 clock). CONNECTION semantics per the INDI standard
        # properties: CONNECT=On means the driver holds the device link.
        if connect == "On" and prev_connect != "On":
            asyncio.get_running_loop().create_task(self._announce_connected())

    # -- accessors -------------------------------------------------------

    def revision(self, prop: str) -> int:
        """Update counter for a property; increments on every def/set received."""
        p = self._props.get(prop)
        return 0 if p is None else p["rev"]

    def prop_timeout(self, prop: str) -> float | None:
        """The driver's own worst-case time for this property, if it sent one."""
        p = self._props.get(prop)
        return None if p is None else p.get("timeout")

    def state(self, prop: str) -> str | None:
        p = self._props.get(prop)
        return None if p is None else p["state"]

    def numbers(self, prop: str) -> dict[str, float] | None:
        p = self._props.get(prop)
        if p is None:
            return None
        out = {}
        for k, v in p["values"].items():
            try:
                out[k] = float(v)
            except ValueError:
                pass
        return out

    def switch_on(self, prop: str) -> str | None:
        """Return the name of the element that is On, or None."""
        p = self._props.get(prop)
        if p is None:
            return None
        for k, v in p["values"].items():
            if v.strip() == "On":
                return k
        return None

    async def wait_until(self, check: Any, timeout: float) -> bool:
        """Wait until check() is true, waking on each incoming update.

        Event-driven, so it returns as soon as the relevant update arrives.
        The 0.5 s cap per wait covers conditions not tied to an incoming
        property. Returns False on timeout rather than raising, so callers
        can report "never settled" explicitly.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not check():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), min(remaining, 0.5))
            except asyncio.TimeoutError:
                pass
        return True

    async def wait_for(self, prop: str, timeout: float) -> None:
        """Block until the named property exists in the cache."""
        async def _wait() -> None:
            while prop not in self._props:
                self._changed.clear()
                await self._changed.wait()
        await asyncio.wait_for(_wait(), timeout)

    # -- writing ---------------------------------------------------------

    async def _send(self, xml: str) -> None:
        if self._writer is None:
            raise IndiError("not connected")
        self._writer.write((xml + "\n").encode())
        await self._writer.drain()

    async def refresh(self, prop: str) -> None:
        """Ask the driver to re-announce one property.

        Needed because INDI publishes on change only. A no-op command (e.g.
        UNPARK to an already-unparked mount) produces a message but no
        property update, so a client whose cached value is stale would wait
        forever for an update that never comes (hit 2026-08-30).
        """
        await self._send(f'<getProperties version="1.7" device="{self._device}" '
                         f'name="{prop}"/>')

    async def set_switch(self, prop: str, element: str) -> None:
        log.debug("indi       -> %s = %s (currently %s)",
                  prop, element, self.switch_on(prop))
        await self._send(
            f'<newSwitchVector device="{self._device}" name="{prop}">'
            f'<oneSwitch name="{element}">On</oneSwitch></newSwitchVector>')

    async def set_text(self, prop: str, values: dict[str, str]) -> None:
        """Send a text vector (TIME_UTC is a text property, not numeric)."""
        body = "".join(f'<oneText name="{k}">{v}</oneText>' for k, v in values.items())
        await self._send(
            f'<newTextVector device="{self._device}" name="{prop}">{body}</newTextVector>')

    async def set_numbers(self, prop: str, values: dict[str, float]) -> None:
        body = "".join(f'<oneNumber name="{k}">{v}</oneNumber>' for k, v in values.items())
        await self._send(
            f'<newNumberVector device="{self._device}" name="{prop}">{body}</newNumberVector>')
