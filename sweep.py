# -*- coding: utf-8 -*-
"""
VSG60A sweep generators

  chirp_sweep()      Fixed LO, linear-FM chirp synthesized in baseband.
                     Hardware-timed, deterministic, gap-free. Span is
                     limited to the instantaneous bandwidth (<= ~0.8 * fs).

  stepped_lo_sweep() Retunes the synthesizer across an arbitrary span.
                     Any span from 50 MHz to 6 GHz, but dwell timing is
                     software-timed and therefore NOT deterministic.
"""

import argparse
import numpy as np
from time import sleep

from vsgdevice.vsg_api import *


# --------------------------------------------------------------------------
# Waveform construction
# --------------------------------------------------------------------------

def make_lfm(span_hz, sample_rate, sweep_time_s, shape="triangle"):
    """
    Build a baseband linear-FM chirp as interleaved float32 [I0,Q0,I1,Q1,...].

    The phase is defined analytically so that the accumulated phase over one
    period is an exact multiple of 2*pi -- the buffer repeats seamlessly with
    no phase glitch at the wrap.

      shape="up"        -span/2 -> +span/2, sawtooth retrace
      shape="down"      +span/2 -> -span/2, sawtooth retrace
      shape="triangle"  -span/2 -> +span/2 -> -span/2, continuous in
                        both phase and frequency (no retrace discontinuity)

    Returns (iq_interleaved_float32, n_samples).
    """
    if shape not in ("up", "down", "triangle"):
        raise ValueError("shape must be 'up', 'down', or 'triangle'")

    n = int(round(sweep_time_s * sample_rate))
    if n < 2:
        raise ValueError("sweep_time too short for this sample rate")
    if shape == "triangle" and n % 2:
        n += 1  # need an even split for a symmetric ramp

    fs = float(sample_rate)
    T = n / fs
    t = np.arange(n, dtype=np.float64) / fs

    if shape in ("up", "down"):
        k = span_hz / T                                  # Hz/s
        phase = 2.0 * np.pi * (-span_hz / 2.0 * t + 0.5 * k * t * t)
        if shape == "down":
            phase = -phase
    else:
        h = n // 2
        Th = h / fs
        k = span_hz / Th
        t1 = t[:h]
        u = t[h:] - Th
        phase = np.concatenate([
            2.0 * np.pi * (-span_hz / 2.0 * t1 + 0.5 * k * t1 * t1),
            2.0 * np.pi * ( span_hz / 2.0 * u  - 0.5 * k * u  * u),
        ])

    z = np.exp(1j * phase)

    iq = np.empty(2 * n, dtype=np.float32)
    iq[0::2] = z.real
    iq[1::2] = z.imag
    return iq, n


def make_cw():
    """Single full-scale I/Q sample -> unmodulated carrier at the LO."""
    iq = np.zeros(2, dtype=np.float32)
    iq[0] = 1.0
    return iq, 1


# --------------------------------------------------------------------------
# Mode 1: baseband chirp, fixed LO
# --------------------------------------------------------------------------

def chirp_sweep(center_hz=1.0e9,
                span_hz=20.0e6,
                sweep_time_s=1.0e-3,
                level_dbm=20.0,
                sample_rate=50.0e6,
                shape="triangle",
                duration_s=10.0):
    """
    Sweep +/- span/2 around center_hz by chirping the baseband
    """
    if span_hz > 0.8 * sample_rate:
        raise ValueError(
            f"span {span_hz/1e6:.1f} MHz exceeds 80% of the {sample_rate/1e6:.1f} "
            f"MSPS sample rate; raise sample_rate or use stepped_lo_sweep()"
        )

    iq, n = make_lfm(span_hz, sample_rate, sweep_time_s, shape)

    handle = vsg_open_device()["handle"]
    try:
        vsg_set_frequency(handle, center_hz)
        vsg_set_level(handle, level_dbm)
        vsg_set_sample_rate(handle, sample_rate)

        vsg_repeat_waveform(handle, iq, n)   # n = I/Q PAIRS, not float count

        print(f"chirp: {(center_hz - span_hz/2)/1e6:.3f} - "
              f"{(center_hz + span_hz/2)/1e6:.3f} MHz, "
              f"{shape}, {sweep_time_s*1e6:.1f} us/period, "
              f"{n} samples, {level_dbm:.1f} dBm")
        sleep(duration_s)
    finally:
        vsg_abort(handle)
        vsg_close_device(handle)


# --------------------------------------------------------------------------
# Mode 2: stepped LO
# --------------------------------------------------------------------------

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
    # chirp
    p.add_argument("--sweep-time", type=float, default=1.0e-3, help="s/period")
    p.add_argument("--shape", choices=["up", "down", "triangle"],
                   default="triangle")
    # stepped
    p.add_argument("--points", type=int, default=201)
    p.add_argument("--dwell", type=float, default=0.005, help="s")
    p.add_argument("--bidirectional", action="store_true")
    a = p.parse_args()

    if a.mode == "chirp":
        chirp_sweep(center_hz=a.center,
                    span_hz=a.span if a.span else 20.0e6,
                    sweep_time_s=a.sweep_time,
                    level_dbm=a.level,
                    sample_rate=a.sample_rate,
                    shape=a.shape,
                    duration_s=a.duration)
    else:
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
