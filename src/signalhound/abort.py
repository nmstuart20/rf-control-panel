#!/usr/bin/env python3
"""Stop transmission on every attached VSG60A.
"""

from __future__ import annotations

import argparse
import sys

from vsgdevice.vsg_api import vsg_abort, vsg_close_device, vsg_open_device


MAX_DEVICES = 8


def abort_all(maximum: int = MAX_DEVICES) -> int:
    """Abort and close every generator that can be opened. Returns the count.

    Devices are held open while the next one is opened so that each call
    selects a different generator, matching how dual_cw.py drives two units.
    """
    handles: list[object] = []
    failures: list[str] = []
    try:
        while len(handles) < maximum:
            try:
                handles.append(vsg_open_device()["handle"])
            except Exception:
                # No further device is available to open.
                break
        for index, handle in enumerate(handles, start=1):
            try:
                vsg_abort(handle)
                print(f"aborted generator {index}", flush=True)
            except Exception as exc:
                failures.append(f"generator {index}: {exc}")
    finally:
        for handle in handles:
            try:
                vsg_close_device(handle)
            except Exception as exc:
                failures.append(f"closing a generator: {exc}")
    if failures:
        raise RuntimeError("; ".join(failures))
    return len(handles)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-device",
        action="store_true",
        help="fail when no generator can be opened, instead of reporting none found",
    )
    parser.add_argument(
        "--max-devices", type=int, default=MAX_DEVICES,
        help=f"most generators to open (default: {MAX_DEVICES})",
    )
    args = parser.parse_args()
    if args.max_devices < 1:
        parser.error("--max-devices must be at least 1")

    try:
        count = abort_all(args.max_devices)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if count:
        print(f"{count} generator(s) are not transmitting", flush=True)
        return 0
    # A generator that is absent is not transmitting. A generator that is
    # present but unopenable is a different problem, which --require-device
    # turns into a failure for setups that always expect one.
    print("no generator was found", flush=True)
    return 1 if args.require_device else 0


if __name__ == "__main__":
    raise SystemExit(main())
