#!/usr/bin/env python3
"""Generate a stepped-frequency sweep with a Signal Hound VSG60A."""

from __future__ import annotations

import argparse
import signal
import time

import numpy as np

from vsgdevice.vsg_api import (
    VSG60_MAX_FREQ,
    VSG60_MIN_FREQ,
    VSG_MAX_LEVEL,
    VSG_MAX_SAMPLE_RATE,
    VSG_MIN_LEVEL,
    VSG_MIN_SAMPLE_RATE,
    vsg_abort,
    vsg_close_device,
    vsg_open_device,
    vsg_repeat_waveform,
    vsg_set_frequency,
    vsg_set_level,
    vsg_set_sample_rate,
)


def make_cw() -> tuple[np.ndarray, int]:
    """Return one full-scale interleaved I/Q sample for a carrier at the LO."""
    return np.array([1.0, 0.0], dtype=np.float32), 1


def stepped_lo_sweep(
    center_hz: float = 1.0e9,
    span_hz: float = 200.0e6,
    n_points: int = 201,
    dwell_s: float = 0.005,
    level_dbm: float = -20.0,
    sample_rate: float = 50.0e6,
    duration_s: float = 10.0,
    bidirectional: bool = False,
) -> None:
    """Repeat a stepped CW sweep for approximately ``duration_s`` seconds."""
    start = center_hz - span_hz / 2.0
    stop = center_hz + span_hz / 2.0
    freqs = np.linspace(start, stop, n_points)
    if bidirectional and n_points > 2:
        freqs = np.concatenate([freqs, freqs[-2:0:-1]])

    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    iq, sample_count = make_cw()
    handle = vsg_open_device()["handle"]
    steps = 0
    completed_sweeps = 0
    try:
        vsg_set_frequency(handle, float(start))
        vsg_set_level(handle, level_dbm)
        vsg_set_sample_rate(handle, sample_rate)
        vsg_repeat_waveform(handle, iq, sample_count)

        print(
            f"stepped LO: {start / 1e6:.3f} - {stop / 1e6:.3f} MHz, "
            f"{n_points} pts, {dwell_s * 1e3:.1f} ms dwell, "
            f"{level_dbm:.1f} dBm for {duration_s:.1f} s",
            flush=True,
        )

        deadline = time.monotonic() + duration_s
        while not stop_requested and time.monotonic() < deadline:
            completed_cycle = True
            for frequency in freqs:
                if stop_requested or time.monotonic() >= deadline:
                    completed_cycle = False
                    break
                vsg_set_frequency(handle, float(frequency))
                steps += 1
                remaining = deadline - time.monotonic()
                time.sleep(min(dwell_s, max(0.0, remaining)))
            if completed_cycle:
                completed_sweeps += 1
        print(
            f"completed {completed_sweeps} full sweep(s), {steps} frequency steps",
            flush=True,
        )
    finally:
        vsg_abort(handle)
        vsg_close_device(handle)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        print("transmission stopped", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["stepped"], default="stepped")
    parser.add_argument("--center", type=float, default=1.0e9, help="Hz")
    parser.add_argument("--span", type=float, default=200.0e6, help="Hz")
    parser.add_argument("--level", type=float, default=-20.0, help="dBm")
    parser.add_argument("--sample-rate", type=float, default=50.0e6, help="S/s")
    parser.add_argument("--duration", type=float, default=10.0, help="s")
    parser.add_argument("--points", type=int, default=201)
    parser.add_argument("--dwell", type=float, default=0.005, help="s")
    parser.add_argument("--bidirectional", action="store_true")
    args = parser.parse_args()

    start = args.center - args.span / 2.0
    stop = args.center + args.span / 2.0
    if args.span <= 0:
        parser.error("--span must be positive")
    if start < VSG60_MIN_FREQ or stop > VSG60_MAX_FREQ:
        parser.error(
            f"sweep endpoints must be between {VSG60_MIN_FREQ:g} and "
            f"{VSG60_MAX_FREQ:g} Hz"
        )
    if not VSG_MIN_LEVEL <= args.level <= VSG_MAX_LEVEL:
        parser.error(
            f"--level must be between {VSG_MIN_LEVEL:g} and "
            f"{VSG_MAX_LEVEL:g} dBm"
        )
    if not VSG_MIN_SAMPLE_RATE <= args.sample_rate <= VSG_MAX_SAMPLE_RATE:
        parser.error(
            f"--sample-rate must be between {VSG_MIN_SAMPLE_RATE:g} and "
            f"{VSG_MAX_SAMPLE_RATE:g} S/s"
        )
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if args.points < 2:
        parser.error("--points must be at least 2")
    if args.dwell <= 0:
        parser.error("--dwell must be positive")
    return args


def main() -> None:
    args = parse_args()
    stepped_lo_sweep(
        center_hz=args.center,
        span_hz=args.span,
        n_points=args.points,
        dwell_s=args.dwell,
        level_dbm=args.level,
        sample_rate=args.sample_rate,
        duration_s=args.duration,
        bidirectional=args.bidirectional,
    )


if __name__ == "__main__":
    main()
