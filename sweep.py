# -*- coding: utf-8 -*-
"""
VSG60A sweep generator
"""

import argparse
import numpy as np
from time import sleep

from vsgdevice.vsg_api import *

# Make Waveform
def make_cw():
    """Single full-scale I/Q sample -> unmodulated carrier at the LO."""
    iq = np.zeros(2, dtype=np.float32)
    iq[0] = 1.0
    return iq, 1

# stepped LO
def stepped_lo_sweep(center_hz=1.0e9,
                     span_hz=200.0e6,
                     n_points=201,
                     dwell_s=0.005,
                     level_dbm=-20.0,
                     sample_rate=50.0e6,
                     duration_s=10.0,
                     bidirectional=False):
    """
    Step a CW carrier across center_hz +/- span/2 by retuning the synthesizer.
    """
    start = center_hz - (span_hz / 2.0)
    stop = center_hz + (span_hz / 2.0)
    freqs = np.linspace(start, stop, n_points)
    if bidirectional and n_points > 2:
        freqs = np.concatenate([freqs, freqs[-2:0:-1]])

    iq, n = make_cw()

    handle = vsg_open_device()["handle"]
    try:
        vsg_set_frequency(handle, float(start))
        vsg_set_level(handle, level_dbm)
        vsg_set_sample_rate(handle, sample_rate)
        vsg_repeat_waveform(handle, iq, n)

        print(f"stepped LO: {start/1e6:.3f} - {stop/1e6:.3f} MHz, "
              f"{n_points} pts, {dwell_s*1e3:.1f} ms dwell, "
              f"{level_dbm:.1f} dBm")

        sweeps = 0
        for f in freqs:
            vsg_set_frequency(handle, float(f))
            sleep(dwell_s)
            sweeps += 1
        print(f"completed ~{sweeps} sweeps")
    finally:
        vsg_abort(handle)
        vsg_close_device(handle)


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="VSG60A sweeper")
    p.add_argument("--mode", choices=["chirp", "stepped"], default="chirp")
    p.add_argument("--center", type=float, default=1.0e9, help="Hz")
    p.add_argument("--span", type=float, default=None, help="Hz")
    p.add_argument("--level", type=float, default=-20.0, help="dBm")
    p.add_argument("--sample-rate", type=float, default=50.0e6, help="S/s")
    p.add_argument("--duration", type=float, default=10.0, help="s")
    # stepped
    p.add_argument("--points", type=int, default=201)
    p.add_argument("--dwell", type=float, default=0.005, help="s")
    p.add_argument("--bidirectional", action="store_true")
    a = p.parse_args()
    stepped_lo_sweep(center_hz=a.center,
                        span_hz=a.span if a.span else 200.0e6,
                        n_points=a.points,
                        dwell_s=a.dwell,
                        level_dbm=a.level,
                        sample_rate=a.sample_rate,
                        duration_s=a.duration,
                        bidirectional=a.bidirectional)


if __name__ == "__main__":
    main()
