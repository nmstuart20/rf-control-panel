#!/usr/bin/env python3
"""Connect a fan-out input to one or more outputs."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
from collections.abc import Sequence

try:
    from .fanin_connect import (
        PROJECT_ROOT,
        QuintechError,
        QuintechWebClient,
        dotenv_value,
    )
except ImportError:  # Allow direct execution as well as ``python -m``.
    from fanin_connect import (  # type: ignore[no-redef]
        PROJECT_ROOT,
        QuintechError,
        QuintechWebClient,
        dotenv_value,
    )


class FanOutWebClient(QuintechWebClient):
    """Quintech web client operations for the fan-out matrix."""

    def authenticate(self, username: str, password: str) -> None:
        result = self.post(
            [
                {"AuthenticateUser": {"username": username, "password": password}},
                {"GetFanOutCrosspoints": {}},
            ]
        )
        user_id = result.get("UserId", -1)
        if not isinstance(user_id, int) or user_id < 0:
            raise QuintechError("authentication failed")
        self.check_command(result, "GetFanOutCrosspoints")
        self.credentials = (username, password)

    def get_fanout_crosspoints(self) -> list[dict[str, object]]:
        result = self.authenticated_post([{"GetFanOutCrosspoints": {}}])
        self.check_command(result, "GetFanOutCrosspoints")
        crosspoints = result.get("FanOutCrosspoints")
        if not isinstance(crosspoints, list):
            raise QuintechError("switch did not return fan-out crosspoints")
        return crosspoints

    def set_fanout_crosspoint(self, input_index: int, output_index: int) -> None:
        result = self.authenticated_post(
            [
                {
                    "SetCrosspoint": {
                        "matrix": "FanOut",
                        "input": input_index,
                        "output": output_index,
                        "change_queue_id": 128,
                    }
                }
            ]
        )
        self.check_command(result, "SetCrosspoint")

    def get_rf_levels(self) -> tuple[list[object], list[object]]:
        """Return the physical fan-out input and output RF-level readings."""
        result = self.authenticated_post(
            [
                {"GetFanOutInputSignalValues": {}},
                {"GetFanOutOutputSignalValues": {}},
            ]
        )
        self.check_command(result, "GetFanOutInputSignalValues")
        self.check_command(result, "GetFanOutOutputSignalValues")
        input_levels = result.get("FanOutInputSignalValues")
        output_levels = result.get("FanOutOutputSignalValues")
        if not isinstance(input_levels, list) or not isinstance(output_levels, list):
            raise QuintechError(
                "switch did not return fan-out input and output RF levels"
            )
        return input_levels, output_levels

    def clear_fanout_crosspoint(self, output_index: int) -> None:
        """Disconnect one fan-out output from its selected input."""
        self.set_fanout_crosspoint(input_index=-1, output_index=output_index)


def connected_outputs_for_input(
    crosspoints: Sequence[object], input_index: int
) -> set[int]:
    """Return API output indexes connected to one fan-out input."""
    connected: set[int] = set()
    for position, crosspoint in enumerate(crosspoints):
        if not isinstance(crosspoint, dict):
            continue
        if crosspoint.get("input") != input_index:
            continue
        output_index = crosspoint.get("output", position)
        if isinstance(output_index, int) and not isinstance(output_index, bool):
            connected.add(output_index)
    return connected


def connect_crosspoint(
    host: str,
    timeout: float,
    verify_tls: bool,
    username: str,
    password: str,
    input_number: int,
    output_numbers: Sequence[int],
    expected_input_level: float | None = None,
    level_tolerance: float = 3.0,
    level_settle_seconds: float = 0.0,
    check_rf_only: bool = False,
    reset_existing: bool = False,
) -> None:
    if check_rf_only and expected_input_level is None:
        raise QuintechError("--check-rf-only requires --expected-input-level")
    client = FanOutWebClient(host, timeout, verify_tls)
    client.authenticate(username, password)
    print(f"HTTPS connection and authentication verified for {host}")

    if check_rf_only:
        assert expected_input_level is not None
        verify_rf_levels(
            client,
            input_number,
            output_numbers,
            expected_input_level,
            level_tolerance,
            level_settle_seconds,
        )
        return

    # CLI port numbers are one-based, while the web API uses zero-based indexes.
    input_index = input_number - 1
    output_indexes = {output_number - 1 for output_number in output_numbers}
    connected_output_indexes = connected_outputs_for_input(
        client.get_fanout_crosspoints(), input_index
    )

    if reset_existing:
        for output_index in sorted(connected_output_indexes - output_indexes):
            client.clear_fanout_crosspoint(output_index)

    for output_index in sorted(output_indexes - connected_output_indexes):
        client.set_fanout_crosspoint(input_index, output_index)

    connected_output_indexes = connected_outputs_for_input(
        client.get_fanout_crosspoints(), input_index
    )
    missing = output_indexes - connected_output_indexes
    if missing:
        missing_numbers = ", ".join(str(index + 1) for index in sorted(missing))
        raise QuintechError(
            f"read-back failed: input {input_number} does not report "
            f"output(s) {missing_numbers}"
        )
    if reset_existing:
        unexpected = connected_output_indexes - output_indexes
        if unexpected:
            unexpected_numbers = ", ".join(
                str(index + 1) for index in sorted(unexpected)
            )
            raise QuintechError(
                f"read-back failed: input {input_number} still reports "
                f"unexpected output(s) {unexpected_numbers}"
            )

    output_list = ", ".join(map(str, output_numbers))
    print(f"Connected fan-out input {input_number} to output(s) {output_list}")

    if expected_input_level is not None:
        verify_rf_levels(
            client,
            input_number,
            output_numbers,
            expected_input_level,
            level_tolerance,
            level_settle_seconds,
        )


def verify_rf_levels(
    client: FanOutWebClient,
    input_number: int,
    output_numbers: Sequence[int],
    expected_input_level: float,
    level_tolerance: float,
    level_settle_seconds: float,
) -> None:
    """Read and verify the input and each selected fan-out output RF level."""
    if level_settle_seconds:
        time.sleep(level_settle_seconds)
    input_levels, output_levels = client.get_rf_levels()
    measured_input = rf_level_at(input_levels, input_number, "input")
    measured_outputs = [
        rf_level_at(output_levels, number, "output") for number in output_numbers
    ]

    failures: list[str] = []
    print(
        f"Input {input_number} RF level: {measured_input:.1f} dBm "
        f"(expected {expected_input_level:.1f} dBm)"
    )
    if abs(measured_input - expected_input_level) > level_tolerance:
        failures.append(
            f"input {input_number} measured {measured_input:.1f} dBm, "
            f"expected {expected_input_level:.1f} dBm"
        )
    for number, measured in zip(output_numbers, measured_outputs):
        print(
            f"Output {number} RF level: {measured:.1f} dBm "
            f"(expected {expected_input_level:.1f} dBm)"
        )
        if abs(measured - expected_input_level) > level_tolerance:
            failures.append(
                f"output {number} measured {measured:.1f} dBm, "
                f"expected {expected_input_level:.1f} dBm"
            )
    if failures:
        raise QuintechError(
            f"RF level verification failed (tolerance {level_tolerance:g} dB): "
            + "; ".join(failures)
        )
    print(f"RF levels verified within {level_tolerance:g} dB")


def rf_level_at(levels: Sequence[object], port_number: int, kind: str) -> float:
    """Extract a one-based port reading from the switch's RF-level array."""
    index = port_number - 1
    if index >= len(levels):
        raise QuintechError(f"switch did not return RF level for {kind} {port_number}")
    reading = levels[index]
    if isinstance(reading, dict):
        for key in ("level", "rf_level", "RFLevel", "value"):
            if key in reading:
                reading = reading[key]
                break
    if isinstance(reading, bool):
        raise QuintechError(f"invalid RF level for {kind} {port_number}")
    try:
        return float(reading)
    except (TypeError, ValueError) as exc:
        raise QuintechError(f"invalid RF level for {kind} {port_number}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Connect one input to one or more outputs on the XTREME 32 "
            "fan-out matrix."
        )
    )
    parser.add_argument(
        "input",
        type=int,
        choices=range(1, 9),
        metavar="INPUT",
        help="physical fan-out input number (1-8)",
    )
    parser.add_argument(
        "outputs",
        type=int,
        choices=range(1, 9),
        nargs="+",
        metavar="OUTPUT",
        help="one or more physical fan-out output numbers (1-8)",
    )
    parser.add_argument("--host", default="192.168.0.248")
    parser.add_argument("--username", default="Admin")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--password-env",
        default="QUINTECH_PASSWORD",
        help="password environment variable (default: QUINTECH_PASSWORD)",
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        help="verify the HTTPS certificate (off by default for self-signed certificates)",
    )
    parser.add_argument(
        "--expected-input-level",
        "--expected-input-levels",
        type=float,
        dest="expected_input_level",
        metavar="DBM",
        help="verify the selected input and each output against this RF level",
    )
    parser.add_argument(
        "--check-rf-only",
        action="store_true",
        help="check RF levels without changing or verifying fan-out crosspoints",
    )
    parser.add_argument(
        "--reset-existing",
        action="store_true",
        help=(
            "remove existing connections from INPUT to outputs not requested "
            "on this command"
        ),
    )
    parser.add_argument(
        "--level-tolerance",
        type=float,
        default=5.0,
        metavar="DB",
        help="maximum RF-level error (default: 5 dB)",
    )
    parser.add_argument(
        "--level-settle-seconds",
        type=float,
        default=1.0,
        metavar="SECONDS",
        help="delay before reading RF levels (default: 1 second)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check_rf_only and args.reset_existing:
        print(
            "ERROR: --reset-existing cannot be used with --check-rf-only",
            file=sys.stderr,
        )
        return 2
    if args.level_tolerance < 0:
        print("ERROR: --level-tolerance must not be negative", file=sys.stderr)
        return 2
    if args.level_settle_seconds < 0:
        print("ERROR: --level-settle-seconds must not be negative", file=sys.stderr)
        return 2
    password = os.environ.get(args.password_env)
    if password is None:
        password = dotenv_value(PROJECT_ROOT / ".env", args.password_env)
    if password is None:
        password = getpass.getpass(f"Password for {args.username}@{args.host}: ")
    try:
        connect_crosspoint(
            args.host,
            args.timeout,
            args.verify_tls,
            args.username,
            password,
            args.input,
            args.outputs,
            args.expected_input_level,
            args.level_tolerance,
            args.level_settle_seconds,
            args.check_rf_only,
            args.reset_existing,
        )
    except (OSError, QuintechError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
