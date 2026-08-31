#!/usr/bin/env python3
"""###############################################################################################

    Michael J Evan
    MS Computer Science | University of Massachusetts Dartmouth 2026
    AAVSO (American Association of Variable Star Observers)

    Plot arrival error vs declination from indi-log.csv.

    Purpose: sanity-check the equinox-of-date <-> J2000 conversion, which
    fails quietly (a wrong conversion gives a plausible coordinate, not an
    error). Expected shape: error ~ k*cos(dec), since a fixed RA rounding
    covers less sky at higher declination. A bad tan(dec) term would make
    the points rise toward the pole instead.

    Usage:
    python analysis/plot_log.py           # writes error_vs_dec.png
    python analysis/plot_log.py --show    # opens a window

###############################################################################################"""

from __future__ import annotations

import argparse
import pathlib

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_LOG = HERE / "indi-log.csv"


def load(path: pathlib.Path = DEFAULT_LOG) -> pd.DataFrame:
    """Arrivals only, with the columns that matter."""
    df = pd.read_csv(path, parse_dates=["time"])
    return df[df["event"] == "arrived"].dropna(subset=["dec_j2000", "arcsec_off"])


def plot_error_vs_dec(df: pd.DataFrame, ax=None):
    """Arrival error against declination, with the cos(dec) curve geometry predicts."""
    ax = ax or plt.subplots(figsize=(8, 5), dpi=160)[1]
    dec, off = df["dec_j2000"].to_numpy(), df["arcsec_off"].to_numpy()

    # Scale the curve on the low-declination points only, so the pole is a
    # prediction rather than something the fit was told about.
    low = np.abs(dec) < 45
    if low.any():
        k = np.median(off[low]) / np.cos(np.radians(np.median(np.abs(dec[low]))))
        x = np.linspace(min(-30, dec.min()), 90, 400)
        ax.plot(x, k * np.cos(np.radians(x)), color="#c0392b", lw=1.6,
                label=rf"${k:.2f}\,\cos(\delta)$ - fixed RA rounding")

    ax.scatter(dec, off, s=55, color="#1f4e79", zorder=3, label="measured arrival error")
    ax.set_yscale("log")
    ax.set_xlabel("declination of target (degrees, J2000)")
    ax.set_ylabel("arrival error (arcsec, log scale)")
    ax.set_title(f"pyobs-indi: arrival error vs declination  ({len(df)} slews)")
    ax.grid(alpha=.3, which="both")
    ax.legend(frameon=False)
    return ax


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    p.add_argument("--show", action="store_true", help="open a window instead of saving")
    args = p.parse_args()

    df = load(args.log)
    if df.empty:
        raise SystemExit(f"no arrivals in {args.log} yet; slew a few times first")
    plot_error_vs_dec(df)
    plt.tight_layout()
    if args.show:
        plt.show()
    else:
        out = HERE / "error_vs_dec.png"
        plt.savefig(out)
        print(f"{len(df)} slews -> {out}")


if __name__ == "__main__":
    main()
