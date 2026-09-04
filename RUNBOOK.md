# Runbook

This is the author's development-machine setup (macOS, with a Linux VM running
on OrbStack). On a native Linux box there is no OrbStack, no VM and no USB
handoff: run indiserver directly (`indiserver -v indi_lx200am5`) and skip
steps 1-3. The paths and environment names below are my M5 Mac; please
adapt to your specific development environment.

Startup sequence for the full stack on the development machine (Mac +
OrbStack). One terminal window per long-running program, and use a real
terminal app, not an IDE's built-in one: an IDE that quits takes its
terminals with it (learned the hard way when a 1 AM macOS update closed
VS Code mid-run; Terminal refused to quit and its processes survived).

Order barely matters, since everything waits and reconnects, but this
sequence starts clean.

### 1. OrbStack and the containers (ejabberd + pyobs simulator)

`orb` commands wake OrbStack on their own; plain `docker` commands do not,
so start it explicitly after a reboot:

```
orb start
```

```
cd ~/Development/Pyobs/sim-2.0 && docker compose up -d
```

### 2. Hand the mount's USB to Linux

Mount powered on and plugged in first. `orb usb list` shows the ID if it
changes.

```
orb usb attach 02100000
```

### 3. indiserver with the ZWO driver

Boots the `indi` VM if it is asleep. Window stays busy; leave it running.

```
orb -m indi indiserver -v indi_lx200am5
```

### 3b. USB watcher (optional but recommended)

Re-attaches the mount's USB to the VM automatically after a mount
power-cycle, so a mid-session power blip needs no hands at all. Window
stays busy; leave it running. Safe to leave up: a healthy link is never
touched.

```
cd ~/Development/Pyobs/pyobs-indi && ./tools/orb_usb_watch.sh
```

### 4. This module

Window stays busy; leave it running.

```
conda activate pyobs-2.0 && cd ~/Development/Pyobs/pyobs-indi && PYTHONPATH=. pyobs indi.yaml
```

Watch the log for `connected to 'ZWO AM5'` and the `sent site ... and UTC`
line: that second one is the module fixing the mount's clock (a cold ZWO
boots thinking it is January 2000) and its elevation.

When a tracked target crosses the meridian the module flips the mount by
itself about 30 seconds later; the log says `flipping the mount` and then
`meridian flip complete`. No action needed. The mount never flips on its
own, so if this module is not running, a tracking mount stops a few
minutes past the meridian and sits there.

### 5. Stellarium bridge for the real mount

Window stays busy; leave it running. Serves port 10002.

```
conda activate pyobs-2.0 && cd ~/Development/Pyobs/stellarium-bridge-2.0 && python bridge.py --config config-indi.yaml
```

### 6. Console

Slew, park, abort, init from the keyboard.

```
conda activate pyobs-2.0 && cd ~/Development/Pyobs/stellarium-bridge-2.0 && python scope.py --config config-indi.yaml
```

### 7. Stellarium

Start the app; the Telescope Control plugin connects to `localhost:10002`.

## Optional

Second bridge and console for the pyobs simulator: same commands as 5 and
6 without `--config`. In Stellarium, add a second telescope connecting to
`localhost:10001` for it; each telescope gets its own reticle.

pyobs GUI (log in as `scratch@localhost`, SSL off, server override
`localhost:5222`):

```
/Users/michael/miniforge3/envs/pyobs-gui/bin/pyobs-gui
```

## Shutdown

Park the mount first (console: `park`), then Ctrl-C the terminals in any
order. To give the USB device back to macOS:

```
orb usb detach 02100000
```

Software dying never stops the mount; it keeps doing whatever it was last
told. Park before you walk away.

## Total shutdown

Everything to zero, including programs running detached with no terminal
(a bridge whose window was closed survives and Ctrl-C can never reach it).
In order:

1. Mount: park, then power it off. Always first -- software never stops
   the mount.

2. Ctrl-C every open terminal: the module, the USB watcher, scope/console.

3. Kill the detached bridges (all of them, whatever configs they were
   started with):

```
pkill -f "python bridge.py"
```

4. NorthStar, if it is running detached:

```
pkill -f northstar.py
```

4b. Any detached pyobs-indi module -- catches both the real-mount and
simulator configs (a sim module from an afternoon of testing survived a
closed window by 8 hours, found only by the verify grep):

```
pkill -f "pyobs indi"
```

5. indiserver in the VM:

```
orb -m indi pkill indiserver
```

6. The containers (ejabberd + sim):

```
cd ~/Development/Pyobs/sim-2.0 && docker compose down
```

7. OrbStack itself. This also stops the Forgejo container safely -- its
   data lives in a volume and comes back on the next start:

```
orb stop
```

Verify everything is gone; empty output means clean:

```
ps ax | grep -E "bridge.py|northstar|pyobs indi" | grep -v grep
```
