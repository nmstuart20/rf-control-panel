#!/usr/bin/env python3
"""Transmit a CW carrier whose output power varies over time on a VSG60A."""

from __future__ import annotations

import argparse
import random
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


def bounded_random_step(
    current: float, minimum: float, maximum: float, maximum_step: float, rng: random.Random
) -> float:
    """Return a random-walk step reflected back into the permitted range."""
    candidate = current + rng.uniform(-maximum_step, maximum_step)
    if candidate < minimum:
        candidate = minimum + (minimum - candidate)
    elif candidate > maximum:
        candidate = maximum - (candidate - maximum)
    return min(max(candidate, minimum), maximum)


def transmit_variable_power(
    frequency_hz: float,
    minimum_dbm: float,
    maximum_dbm: float,
    maximum_step_db: float,
    interval_s: float,
    duration_s: float,
    sample_rate: float,
    seed: int | None,
) -> None:
    """Transmit CW and adjust its power using a bounded random walk."""
    rng = random.Random(seed)
    level_dbm = (minimum_dbm + maximum_dbm) / 2.0
    # One interleaved I/Q sample repeats as an unmodulated carrier at the LO.
    iq = np.array([1.0, 0.0], dtype=np.float32)

    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handlers = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_stop)

    handle = vsg_open_device()["handle"]
    try:
        vsg_set_frequency(handle, frequency_hz)
        vsg_set_level(handle, level_dbm)
        vsg_set_sample_rate(handle, sample_rate)
        vsg_repeat_waveform(handle, iq, 1)

        print(
            f"variable-power CW: {frequency_hz / 1e6:.3f} MHz, "
            f"{minimum_dbm:.1f} to {maximum_dbm:.1f} dBm, "
            f"up to {maximum_step_db:.1f} dB every {interval_s:.3f} s, "
            f"for {duration_s:.1f} s",
            flush=True,
        )
        print(f"power: {level_dbm:.2f} dBm", flush=True)

        deadline = time.monotonic() + duration_s
        while not stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval_s, remaining))
            if stop_requested or time.monotonic() >= deadline:
                break
            level_dbm = bounded_random_step(
                level_dbm, minimum_dbm, maximum_dbm, maximum_step_db, rng
            )
            vsg_set_level(handle, level_dbm)
            print(f"power: {level_dbm:.2f} dBm", flush=True)
    finally:
        vsg_abort(handle)
        vsg_close_device(handle)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        print("transmission stopped", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transmit VSG60A CW with a bounded, randomly varying power level."
    )
    parser.add_argument("--frequency", type=float, default=1.0e9, help="carrier frequency in Hz")
    parser.add_argument("--min-level", type=float, default=-35.0, help="minimum power in dBm")
    parser.add_argument("--max-level", type=float, default=-15.0, help="maximum power in dBm")
    parser.add_argument("--max-step", type=float, default=3.0, help="largest power change in dB")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between changes")
    parser.add_argument("--duration", type=float, default=30.0, help="transmit time in seconds")
    parser.add_argument("--sample-rate", type=float, default=50.0e6, help="sample rate in S/s")
    parser.add_argument("--seed", type=int, help="optional seed for repeatable power changes")
    args = parser.parse_args()

    if not VSG60_MIN_FREQ <= args.frequency <= VSG60_MAX_FREQ:
        parser.error(f"--frequency must be between {VSG60_MIN_FREQ:g} and {VSG60_MAX_FREQ:g} Hz")
    if not VSG_MIN_LEVEL <= args.min_level <= VSG_MAX_LEVEL:
        parser.error(f"--min-level must be between {VSG_MIN_LEVEL:g} and {VSG_MAX_LEVEL:g} dBm")
    if not VSG_MIN_LEVEL <= args.max_level <= VSG_MAX_LEVEL:
        parser.error(f"--max-level must be between {VSG_MIN_LEVEL:g} and {VSG_MAX_LEVEL:g} dBm")
    if args.min_level >= args.max_level:
        parser.error("--min-level must be less than --max-level")
    if args.max_step <= 0 or args.max_step > args.max_level - args.min_level:
        parser.error("--max-step must be positive and no larger than the power range")
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if not VSG_MIN_SAMPLE_RATE <= args.sample_rate <= VSG_MAX_SAMPLE_RATE:
        parser.error(
            f"--sample-rate must be between {VSG_MIN_SAMPLE_RATE:g} and "
            f"{VSG_MAX_SAMPLE_RATE:g} S/s"
        )
    return args


def main() -> None:
    args = parse_args()
    transmit_variable_power(
        frequency_hz=args.frequency,
        minimum_dbm=args.min_level,
        maximum_dbm=args.max_level,
        maximum_step_db=args.max_step,
        interval_s=args.interval,
        duration_s=args.duration,
        sample_rate=args.sample_rate,
        seed=args.seed,
    )


if __name__ == "__main__":
    main()
