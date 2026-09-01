#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    Plot the automatic meridian flips from indi-log.csv.

    A watcher flip appears in the log as a slew to exactly the coordinates
    of the previous slew: the module re-issuing the goto after the target
    crossed the meridian. This finds those pairs and draws two views:
    the afternoon timeline (when each flip fired and how close it
    re-acquired) and the durations (goto to re-acquired, sorted by
    declination).

    Accuracy here is commanded position vs the position the mount reports
    back, on a real ZWO AM3N with targets picked in Stellarium. The
    encoders are grading their own work; it is not plate-solved sky
    pointing. Same caveat as error_vs_dec.png.

    Usage:
    python analysis/plot_flips.py           # writes flips_timeline.png, flips_duration.png
    python analysis/plot_flips.py --show    # opens windows

###############################################################################################"""

from __future__ import annotations

import argparse
import pathlib

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_LOG = HERE / "indi-log.csv"

# The watcher went live at this moment; duplicate-coordinate slews before it
# are manual re-slews, not flips.
FEATURE_LIVE = "2026-08-31T18:20"

# North/south of the observer's zenith, which decides which meridian segment
# the target crossed. Approximate site latitude is enough for the split.
SITE_LAT = 41.0

NORTH, SOUTH = "#1f4e79", "#e67e22"


def load_flips(path: pathlib.Path = DEFAULT_LOG) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"])
    slews = df[df["event"] == "slew"].reset_index(drop=True)
    arrived = df[df["event"] == "arrived"]

    rows = []
    for i in range(1, len(slews)):
        prev, cur = slews.iloc[i - 1], slews.iloc[i]
        if (cur["ra_j2000"], cur["dec_j2000"]) != (prev["ra_j2000"], prev["dec_j2000"]):
            continue
        if cur["time"] < pd.Timestamp(FEATURE_LIVE, tz="UTC"):
            continue
        done = arrived[(arrived["ra_j2000"] == cur["ra_j2000"])
                       & (arrived["time"] > cur["time"])].head(1)
        if done.empty:
            continue
        rows.append({"time": cur["time"], "dec": cur["dec_j2000"],
                     "duration": (done.iloc[0]["time"] - cur["time"]).total_seconds(),
                     "arcsec": done.iloc[0]["arcsec_off"],
                     "side": "north" if cur["dec_j2000"] > SITE_LAT else "south"})
    return pd.DataFrame(rows)


def plot_timeline(f: pd.DataFrame, ax=None):
    """Every flip of the afternoon: when it fired, how close it came back."""
    ax = ax or plt.subplots(figsize=(8, 5), dpi=160)[1]
    for _, r in f.iterrows():
        c = NORTH if r["side"] == "north" else SOUTH
        ax.plot([r["time"], r["time"]], [0, r["arcsec"]], color=c, lw=2)
        ax.scatter([r["time"]], [r["arcsec"]], s=70, color=c, zorder=3)
        ax.annotate(f"{r['dec']:+.0f}\N{DEGREE SIGN}", (r["time"], r["arcsec"]),
                    textcoords="offset points", xytext=(0, 8), ha="center",
                    fontsize=8, color="#444444")
    ax.scatter([], [], s=70, color=NORTH, label="north crossings")
    ax.scatter([], [], s=70, color=SOUTH, label="south crossings")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    day = f["time"].iloc[0].strftime("%Y-%m-%d")
    ax.set_xlabel(f"time (UTC), {day}")
    ax.set_ylabel("re-acquisition error (arcsec)")
    ax.set_title(f"Automatic meridian flips on the real AM3N  "
                 f"({len(f)} flips; labels: target declination)")
    ax.set_ylim(0, f["arcsec"].max() * 1.25)
    ax.grid(alpha=.3)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    return ax


def plot_duration(f: pd.DataFrame, ax=None):
    """How long each flip took, pole to south, with the miss labeled."""
    ax = ax or plt.subplots(figsize=(8, 5), dpi=160)[1]
    order = f.sort_values("dec").reset_index(drop=True)
    for y, r in order.iterrows():
        c = NORTH if r["side"] == "north" else SOUTH
        ax.barh(y, r["duration"], height=.62, color=c)
        ax.annotate(f"{r['arcsec']:.1f}\"", (r["duration"], y),
                    textcoords="offset points", xytext=(6, 0), va="center",
                    fontsize=8, color="#444444")
    ax.set_yticks(range(len(order)),
                  [f"{r['dec']:+.0f}\N{DEGREE SIGN}  {r['side']}" for _, r in order.iterrows()])
    ax.set_xlabel("flip duration (seconds, goto to re-acquired)")
    ax.set_title("Meridian flip duration by declination  (label: re-acquisition error)")
    ax.set_xlim(0, order["duration"].max() * 1.15)
    ax.grid(alpha=.3, axis="x")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    return ax


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    p.add_argument("--show", action="store_true", help="open windows instead of saving")
    args = p.parse_args()

    f = load_flips(args.log)
    if f.empty:
        raise SystemExit(f"no flips found in {args.log}")
    plot_timeline(f)
    plt.tight_layout()
    if not args.show:
        plt.savefig(HERE / "flips_timeline.png")
    plot_duration(f)
    plt.tight_layout()
    if args.show:
        plt.show()
    else:
        plt.savefig(HERE / "flips_duration.png")
        print(f"{len(f)} flips -> flips_timeline.png, flips_duration.png")


if __name__ == "__main__":
    main()
