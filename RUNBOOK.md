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
