#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    What the client does when indiserver goes away.

    This is the same failure fixed on the pyobs side on 2026-08-27,
    where a proxy kept handing back the last value it ever received after its module died.

    Run: python tests/test_reconnect.py

###############################################################################################"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pyobs_indi import device as indi
from pyobs_indi.device import IndiDevice


class FakeIndiServer:
    """Just enough INDI to satisfy IndiDevice, and it can hang up on demand."""

    def __init__(self) -> None:
        self.connections = 0            # how many times anyone has connected
        self.requests = 0               # how many getProperties we answered
        self.silent = False             # stop answering, without ever hanging up
        self.ra = 5.0                   # so a re-read can be told from a stale one
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    return
                if b"getProperties" in data:
                    self.requests += 1
                    if self.silent:
                        # Heard, never answered, never closed. A peer that has
                        # simply ceased to exist; the socket stays
                        # ESTABLISHED on our side with nothing behind it.
                        continue
                    writer.write(self._definitions().encode())
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return

    def _definitions(self) -> str:
        return (
            f'<defNumberVector device="Mount" name="EQUATORIAL_EOD_COORD" state="Ok">'
            f'<defNumber name="RA">{self.ra}</defNumber>'
            f'<defNumber name="DEC">7.5</defNumber>'
            f'</defNumberVector>'
            f'<defSwitchVector device="Mount" name="TELESCOPE_TRACK_STATE" state="Ok">'
            f'<defSwitch name="TRACK_ON">On</defSwitch>'
            f'</defSwitchVector>')

    async def push(self, blob: str) -> None:
        """Send raw INDI XML to every connected client."""
        for w in self._writers:
            w.write(blob.encode())
            await w.drain()
        await asyncio.sleep(0.05)

    async def hang_up(self) -> None:
        """Drop every client, as killing indiserver would."""
        for w in self._writers:
            w.close()
        self._writers.clear()
        await asyncio.sleep(0)

    async def stop(self) -> None:
        await self.hang_up()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def _settled(predicate, timeout: float = 3.0) -> bool:
    """Wait for something to become true, without pinning down when."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def _rig() -> tuple[FakeIndiServer, IndiDevice]:
    server = FakeIndiServer()
    await server.start()
    dev = IndiDevice("127.0.0.1", "Mount", server.port)
    await dev.open()
    return server, dev


def test_a_dead_link_is_not_reported_as_a_position() -> None:
    """The heart of it: no answer beats a wrong answer."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            assert dev.numbers("EQUATORIAL_EOD_COORD")["RA"] == 5.0, "setup failed"
            await server.hang_up()
            assert await _settled(lambda: dev.numbers("EQUATORIAL_EOD_COORD") is None), \
                "still serving the last known position after the link died"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_connected_goes_false_when_the_link_dies() -> None:
    """Anything asking "can I trust this?" must get a straight answer."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            assert dev.connected
            await server.hang_up()
            assert await _settled(lambda: not dev.connected), \
                "still claims to be connected after the server hung up"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_it_reconnects_on_its_own() -> None:
    """A dropped link must heal without anyone restarting the module."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            await server.hang_up()
            assert await _settled(lambda: server.connections >= 2), \
                "never reconnected"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_it_re_asks_for_everything_after_reconnecting() -> None:
    """Reconnecting is half of it. The cache is empty until we re-ask."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            server.ra = 9.0                     # so a fresh read is distinguishable
            await server.hang_up()
            ok = await _settled(
                lambda: (dev.numbers("EQUATORIAL_EOD_COORD") or {}).get("RA") == 9.0)
            assert ok, "reconnected but never re-asked; the cache stayed empty"
            assert server.requests >= 2, "no second getProperties"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_the_stale_value_never_reappears() -> None:
    """Clear it, do not merely stop updating it."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            await server.hang_up()
            await server.stop()                 # nothing to reconnect to
            for _ in range(20):
                await asyncio.sleep(0.05)
                assert dev.numbers("EQUATORIAL_EOD_COORD") is None, \
                    "the pre-disconnect position came back"
        finally:
            await dev.close()

    asyncio.run(run())


def test_a_mount_that_is_not_there_yet_is_waited_for() -> None:
    """Starting before the mount does must not be fatal. Seen 2026-08-30: the module was started 21 seconds before indiserver, the VM's hostname did not resolve yet, and the whole module exited on a traceback."""
    async def run() -> None:
        server = FakeIndiServer()
        await server.start()
        port = server.port
        await server.stop()                     # nothing listening on it now

        dev = IndiDevice("127.0.0.1", "Mount", port)
        try:
            await dev.open()                    # must not raise
            assert not dev.connected, "claimed a connection to nothing"
            assert dev.numbers("EQUATORIAL_EOD_COORD") is None

            back = FakeIndiServer()             # the mount turns up late
            back._server = await asyncio.start_server(back._handle, "127.0.0.1", port)
            back.port = port
            try:
                assert await _settled(
                    lambda: (dev.numbers("EQUATORIAL_EOD_COORD") or {}).get("RA") == 5.0,
                    timeout=5.0), "never picked the mount up once it appeared"
            finally:
                await back.stop()
        finally:
            await dev.close()

    asyncio.run(run())


def test_the_connect_hook_fires_on_reconnect() -> None:
    """Site and time must be re-sent after every reconnect, not just the first."""
    async def run() -> int:
        server, dev = await _rig()
        fired = 0
        async def hook() -> None:
            nonlocal fired
            fired += 1
        dev.on_connected = hook
        try:
            await server.hang_up()
            assert await _settled(lambda: fired >= 1, timeout=5.0), \
                "reconnected without firing the hook"
            return fired
        finally:
            await dev.close()
            await server.stop()

    assert asyncio.run(run()) >= 1


def test_a_blocking_hook_does_not_stall_reconnect() -> None:
    """The on_connected hook runs concurrently, not in the reader's path.

    The telescope layer's hook blocks waiting for a property (site re-send waits
    for GEOGRAPHIC_COORD), and that property only arrives once the reader has
    connected the driver. Awaiting the hook inline on reconnect deadlocked the
    two against each other: the reader stalled for the hook's whole timeout and
    an indiserver bounce left the mount dead for ~600 s (measured 2026-09-03).
    Here a hook that waits on a property the server never sends must NOT stop
    the reader from repopulating the cache."""
    async def run() -> None:
        server, dev = await _rig()
        async def slow_hook() -> None:
            # never satisfied: only a running reader could deliver it
            await dev.wait_until(lambda: dev.state("NEVER_SENT") is not None, 5.0)
        dev.on_connected = slow_hook
        try:
            server.ra = 9.0                     # so a fresh read is distinguishable
            await server.hang_up()
            # the hook blocks for 5 s; the cache must refill well before then
            assert await _settled(
                lambda: (dev.numbers("EQUATORIAL_EOD_COORD") or {}).get("RA") == 9.0,
                timeout=2.5), "the reader stalled behind the on_connected hook"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_a_vanished_peer_is_noticed() -> None:
    """The failure a polite close never exercises. Measured 2026-08-30: the OrbStack VM running indiserver was shut down and the Mac's socket stayed ESTABLISHED, because a machine that disappears sends no FIN and no reset."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            server.silent = True                # alive, listening, answering nothing
            assert await _settled(
                lambda: dev.numbers("EQUATORIAL_EOD_COORD") is None, timeout=5.0), \
                "a peer that stopped answering was never noticed"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_a_healthy_link_is_not_killed_by_the_watchdog() -> None:
    """The other half: no false positives."""
    async def run() -> None:
        server, dev = await _rig()
        try:
            before = server.connections
            for _ in range(int(indi.SILENCE_TIMEOUT / 0.02) + 20):
                await asyncio.sleep(0.02)
                assert dev.numbers("EQUATORIAL_EOD_COORD") is not None, \
                    "dropped a working connection"
            assert server.connections == before, "reconnected without cause"
        finally:
            await dev.close()
            await server.stop()

    asyncio.run(run())


def test_messages_for_other_devices_are_not_logged() -> None:
    """One indiserver hosts several drivers and broadcasts every driver's
    messages to all clients. A client bound to one device must log only its
    own (and device-less server) messages -- not a neighbour's, which is how a
    real mount's errors ended up in the sim's log (2026-09-03)."""
    async def run():
        server, dev = await _rig()
        try:
            # messages precede a vector so they fall within buf[:last] and parse
            await server.push(
                '<message device="Mount" message="mine"/>'
                '<message device="Camera" message="theirs"/>'
                '<message message="server notice"/>'
                '<defNumberVector device="Mount" name="X" state="Ok">'
                '<defNumber name="RA">1</defNumber></defNumberVector>')
            await _settled(lambda: "mine" in dev.messages)
            return list(dev.messages)
        finally:
            await dev.close()
            await server.stop()

    msgs = asyncio.run(run())
    assert "mine" in msgs, "own-device message was dropped"
    assert "server notice" in msgs, "device-less server message was dropped"
    assert "theirs" not in msgs, "another device's message leaked into the log"


if __name__ == "__main__":
    indi.RECONNECT_DELAY = 0.05                 # keep the suite quick
    indi.HEARTBEAT = 0.05
    indi.SILENCE_TIMEOUT = 0.3
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} passed")
