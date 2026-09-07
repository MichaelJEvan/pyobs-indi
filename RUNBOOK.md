# Runbook

The author's development-machine setup: an Apple Silicon Mac with a Linux VM
(named `indi`) under OrbStack. On a native Linux box there is no OrbStack
and no USB handoff; adapt accordingly. Rewritten from the ground up
2026-09-04 against the live, fully-running stack.

Nine terminal windows when everything is up: **windows 1-7 are necessary,
windows 8-9 are optional.** Every window's command starts with a `printf`
that stamps the window's own title bar, so the title always says what lives
there. Plus one ordinary "work window" for typing one-shot commands -- every
command in this document that finishes and returns to the prompt runs there.

## What the pieces are (read once)

- **indiserver** (window 1) -- the hardware server, inside the Linux VM.
  Loads two drivers: the real ZWO mount over USB, and a fake telescope for
  practicing without touching real iron.
- **Mount program** (window 2, `pyobs indi.yaml`) -- connects the real
  mount to pyobs. pyobs calls a program like this a "module."
- **Sim program** (window 3, `pyobs indi-sim.yaml`) -- the same program
  pointed at the fake telescope. Only `-sim` distinguishes the two
  commands; the window titles exist for exactly that reason.
- **Mount bridge** (window 4) and **sim bridge** (window 5, `bridge.py`) --
  each feeds one telescope's position to Stellarium and forwards
  Stellarium's slews back. Mount bridge serves port 10002, sim bridge 10001.
- **NorthStar** (window 6, `northstar.py`) -- the browser dashboard at
  http://localhost:8620.
- **USB watcher** (window 7, `orb_usb_watch.sh`) -- re-attaches the mount's
  USB to the VM automatically after a mount power-cycle.
- **Scope console** (window 8, OPTIONAL, `scope.py`) -- keyboard control of
  the real mount: slew, home, park, abort.
- **pyobs GUI** (window 9, OPTIONAL, `pyobs-gui`) -- the Qt desktop app;
  run from a terminal, its log owns the window while the app is open.
- **Containers** (Docker, no windows): `sim20-ejabberd` is the pyobs
  message server every pyobs program logs into; `sim20-sim` is a set of
  practice pyobs devices (dummy camera, roof, scheduler and friends).

**The window rule:** a live window's last line is a recent log line. A
window whose last line is your shell prompt (`... %`) is DEAD -- everything
above the prompt is history, no matter how official it looks.

## STARTUP -- in this order

### Step 0 -- power and plumbing (work window)

Mount powered on, its USB cable in the Mac. Then, one at a time:

```
orb start
```

```
cd ~/Development/Pyobs/sim-2.0 && docker compose up -d
```

```
orb usb attach -m indi 02100000
```

Check all three took (expect two `sim20-` containers "Up", and one line
naming the ZWO device):

```
docker ps --format '{{.Names}}  {{.Status}}' ; orb -m indi ls /dev/serial/by-id/
```

If `orb usb attach` claims "already attached" but the check shows no
device, the claim is stale:
`orb usb detach 02100000 ; sleep 3 ; orb usb attach -m indi 02100000`

### Window 1 -- INDISERVER (necessary)

New window, paste:

```
printf '\e]0;1 INDISERVER\a'; orb -m indi indiserver -v indi_simulator_telescope indi_lx200am5
```

Healthy: `listening to port 7624`. If it says `bind: Address already in
use` and quits, an old copy is running: `orb -m indi pkill indiserver` in
the work window, then paste this again.

**Reading this window's log:** `Client N: ... welcome!` and
`Client N: shut down complete - bye!` are one CLIENT connecting and
hanging up -- deceptive wording, but never the server dying. Every
one-shot command is such a client (an indi_getprop check, a NorthStar
fault button; NorthStar's NOISY mode produces a pair every 1-3 seconds
for as long as it runs). The server itself exits only with a bare
`good bye` and the window returning to a prompt.

### Window 2 -- MOUNT (necessary)

```
printf '\e]0;2 MOUNT\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/pyobs-indi && PYTHONPATH=. pyobs indi.yaml
```

Healthy: `connected to 'ZWO AM5'`, a `sent site ... and UTC` line (fixing
the mount's clock -- a cold ZWO boots thinking it is January 2000), then a
motion status line (`parked` on a normal morning).

### Window 3 -- SIM (necessary)

Note the `-sim`: this is the fake telescope, not the mount.

```
printf '\e]0;3 SIM\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/pyobs-indi && PYTHONPATH=. pyobs indi-sim.yaml
```

Healthy: `connected to 'Telescope Simulator'` and a motion status line.

### Window 4 -- MOUNT-BRIDGE (necessary)

```
printf '\e]0;4 MOUNT-BRIDGE\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/stellarium-bridge-2.0 && python bridge.py --config config-indi.yaml
```

Healthy: `listening on 127.0.0.1:10002 -- point Stellarium here`.

### Window 5 -- SIM-BRIDGE (necessary)

Same program, no `--config`, different port.

```
printf '\e]0;5 SIM-BRIDGE\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/stellarium-bridge-2.0 && python bridge.py
```

Healthy: `listening on 127.0.0.1:10001`. If it warns `telescope module not
present; waiting for it`, window 3 is down -- fix window 3 and this one
recovers by itself.

### Window 6 -- NORTHSTAR (necessary)

```
printf '\e]0;6 NORTHSTAR\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/northstar && python northstar.py
```

Then open http://localhost:8620 in the browser. Both cards -- mount and
Simulator -- should show live positions (LAST PACKET a few seconds old).

### Window 7 -- USB-WATCH (necessary)

```
cd ~/Development/Pyobs/pyobs-indi && ./tools/orb_usb_watch.sh
```

Healthy: `serial node present; watching`, then silence -- it only speaks
when something changes.

**Stopping the watcher: Ctrl-C does not work on it** (the orb commands it
runs interfere with the terminal). Stop it from the work window instead:

```
pkill -f orb_usb_watch
```

### Window 8 -- CONSOLE (optional)

Keyboard control of the real mount. Type `?` at its prompt for the command
list.

```
printf '\e]0;8 CONSOLE\a'; conda activate pyobs-2.0 && cd ~/Development/Pyobs/stellarium-bridge-2.0 && python scope.py --config config-indi.yaml
```

Healthy: the `>` prompt.

### Window 9 -- GUI (optional)

Opens the pyobs Qt desktop app; the terminal window holds its log while
the app is open. Log in as `scratch@localhost`, password `pyobs`, SSL/TLS
off, Override server address ticked -> `localhost` / `5222`.

```
printf '\e]0;9 GUI\a'; /Users/michael/miniforge3/envs/pyobs-gui/bin/pyobs-gui
```

### Stellarium (app, not a terminal)

Telescope Control plugin. The entry named **INDI** -> localhost:10002 (the
real mount); the entry named **Pyobs** -> localhost:10001 (the sim). Both
read "Connected" within seconds; a stuck "Connecting" means that bridge's
window or its telescope program is down. (An entry named Pyobs-Pi, port
10003, is left over from the retired Pi -- deletable.)

### Startup verification (work window)

```
ps ax | grep -E "bridge.py|northstar.py|pyobs indi" | grep -v grep
```

Exactly five lines: `pyobs indi.yaml`, `pyobs indi-sim.yaml`, two
`bridge.py` (one with `--config`, one without), `northstar.py`. More than
five means a duplicate (two copies of the same one fight over its login
and both break); fewer means a step was missed.

```
for p in 10001 10002 8620; do nc -z localhost $p && echo "$p ok" || echo "$p DEAD"; done
```

All three ok = fully up.

## SHUTDOWN -- in this order

**The mount comes first, always.** Software dying never stops the mount; it
keeps doing whatever it was last told. Park it (console `park`, NorthStar,
or the GUI) and wait for `parked`, then power the mount off.

Then close the windows, highest number first:

- **Window 9 (GUI):** quit the app (Cmd-Q in the Qt window); the terminal
  returns to its prompt.
- **Window 8 (CONSOLE):** type `q` at its prompt.
- **Window 7 (USB-WATCH):** `pkill -f orb_usb_watch` in the work window
  (Ctrl-C does not work on this one).
- **Window 6 (NORTHSTAR):** Ctrl-C.
- **Window 5 (SIM-BRIDGE):** Ctrl-C.
- **Window 4 (MOUNT-BRIDGE):** Ctrl-C.
- **Window 3 (SIM):** Ctrl-C.
- **Window 2 (MOUNT):** Ctrl-C.
- **Window 1 (INDISERVER):** Ctrl-C.

Stopping here leaves the containers and the VM running, which is fine for
a Mac that stays on -- that is the normal nightly stop. To also give the
USB back to macOS: `orb usb detach 02100000` in the work window.

## TOTAL SHUTDOWN -- everything to zero (work window)

Do the SHUTDOWN section above first. Then these catch anything that
survived -- a program whose window was closed does NOT die; it runs on
invisibly and Ctrl-C can never reach it again -- and take the containers
and VM down too. In order:

```
pkill -f "python bridge.py"
```

```
pkill -f northstar.py
```

```
pkill -f "pyobs indi"
```

```
orb -m indi pkill indiserver
```

```
cd ~/Development/Pyobs/sim-2.0 && docker compose down
```

```
orb stop
```

(`orb stop` also stops the Forgejo container safely -- its data lives in a
volume. But it does NOT start itself again: after the next `orb start`,
bring the issue board back with `docker start ns-forgejo-demo`. Found the
hard way 2026-09-06 -- the board was down for two days and nobody noticed
until an issue needed filing.)

Verify -- empty output means everything is truly gone:

```
ps ax | grep -E "bridge.py|northstar.py|pyobs indi" | grep -v grep
```

## Using the mount with OTHER Mac software

The `orb usb attach` from step 0 is a standing grant: whenever the mount is
plugged in and powered, its USB belongs to the Linux VM and **macOS cannot
see it at all**. Any Mac app pointed at the mount (ZWO's own software, or
anything else) will report "no device found" -- nothing is broken, the
mount is just spoken for.

And there is a trap: if you detach while the USB watcher (window 7) is
running, **the watcher steals the mount back within seconds** -- it cannot
tell your deliberate detach from a power-cycle it exists to repair.

The procedure, in order:

1. Stop the watcher: `pkill -f orb_usb_watch` (work window).
2. `orb usb detach 02100000` (work window).
3. Run whatever Mac-side test you wanted.
4. When done, restart the watcher (window 7's command) -- it notices the
   node is missing and re-attaches by itself; the mount program then
   reconnects on its own.

## Rules that keep the mount safe

- **Never cut mount power anywhere but parked.** The mount only knows
  where it points by counting from a known start; kill power mid-slew or
  mid-track and its idea of the sky is fiction -- the next goto is what
  hits the pier, and recovery is manual re-homing with ZWO's own app.
- A mid-session power blip while parked is handled: the watcher re-attaches
  the USB, the mount program reconnects and re-sends site and clock, all
  hands-off (verified 2026-09-03).
- Use a real terminal app, not an IDE's built-in one: an IDE that quits
  takes its terminals with it.
