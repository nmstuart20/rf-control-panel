#!/usr/bin/env python3
"""Transmit two CW carriers concurrently from two VSG60A generators."""

from __future__ import annotations

import argparse
import signal
import time

import numpy as np

from vsgdevice.vsg_api import (
    VSG60_MAX_FREQ,
    VSG60_MIN_FREQ,
    VSG_MAX_LEVEL,
    VSG_MIN_LEVEL,
    vsg_abort,
    vsg_close_device,
    vsg_open_device,
    vsg_repeat_waveform,
    vsg_set_frequency,
    vsg_set_level,
    vsg_set_sample_rate,
)


def transmit_dual_cw(
    frequency_1: float,
    frequency_2: float,
    level_1: float,
    level_2: float,
    duration: float,
    sample_rate: float = 50.0e6,
) -> None:
    """Open two generators and transmit both carriers for the requested time."""
    handles: list[object] = []
    stop_requested = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handlers = {
        signum: signal.signal(signum, request_stop)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    iq = np.array([1.0, 0.0], dtype=np.float32)

    try:
        # Keeping the first device open makes the second call select the other
        # available Signal Hound rather than reopening the same generator.
        handles.append(vsg_open_device()["handle"])
        handles.append(vsg_open_device()["handle"])
        for handle, frequency, level in zip(
            handles, (frequency_1, frequency_2), (level_1, level_2)
        ):
            vsg_set_frequency(handle, frequency)
            vsg_set_level(handle, level)
            vsg_set_sample_rate(handle, sample_rate)
            vsg_repeat_waveform(handle, iq, 1)

        print(
            f"transmitting two CW signals: {frequency_1 / 1e6:.3f} MHz at "
            f"{level_1:.1f} dBm and {frequency_2 / 1e6:.3f} MHz at "
            f"{level_2:.1f} dBm for {duration:.1f} s",
            flush=True,
        )
        deadline = time.monotonic() + duration
        while not stop_requested and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    finally:
        cleanup_error: Exception | None = None
        for handle in handles:
            try:
                vsg_abort(handle)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
            try:
                vsg_close_device(handle)
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        print("both transmissions stopped", flush=True)
        if cleanup_error is not None:
            raise cleanup_error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frequency-1", type=float, default=1.0e9)
    parser.add_argument("--frequency-2", type=float, default=1.01e9)
    parser.add_argument("--level-1", type=float, default=-20.0)
    parser.add_argument("--level-2", type=float, default=-20.0)
    parser.add_argument("--duration", type=float, default=30.0)
    args = parser.parse_args()

    for name in ("frequency_1", "frequency_2"):
        value = getattr(args, name)
        if not VSG60_MIN_FREQ <= value <= VSG60_MAX_FREQ:
            parser.error(
                f"--{name.replace('_', '-')} must be between "
                f"{VSG60_MIN_FREQ:g} and {VSG60_MAX_FREQ:g} Hz"
            )
    for name in ("level_1", "level_2"):
        value = getattr(args, name)
        if not VSG_MIN_LEVEL <= value <= VSG_MAX_LEVEL:
            parser.error(
                f"--{name.replace('_', '-')} must be between "
                f"{VSG_MIN_LEVEL:g} and {VSG_MAX_LEVEL:g} dBm"
            )
    if args.frequency_1 == args.frequency_2:
        parser.error("the two signals must use different frequencies")
    if args.duration <= 0:
        parser.error("--duration must be positive")
    return args


def main() -> None:
    args = parse_args()
    transmit_dual_cw(
        args.frequency_1,
        args.frequency_2,
        args.level_1,
        args.level_2,
        args.duration,
    )


if __name__ == "__main__":
    main()
