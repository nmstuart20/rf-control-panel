#!/usr/bin/env python3
"""Connect a fan-in output to multiple inputs"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class QuintechError(RuntimeError):
    """Raised when the switch rejects a request or returns invalid data."""


def dotenv_value(path: Path, name: str) -> str | None:
    """Read one value from a simple dotenv file without changing the environment."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        return value
    return None


class QuintechWebClient:
    def __init__(self, host: str, timeout: float, verify_tls: bool) -> None:
        self.url = f"https://{host}/qfx.cgi"
        self.timeout = timeout
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context),
        )
        self.credentials: tuple[str, str] | None = None

    def post(self, commands: list[dict[str, object]]) -> dict[str, object]:
        request = urllib.request.Request(
            self.url,
            data=json.dumps(commands, separators=(",", ":")).encode(),
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise QuintechError(f"HTTPS API returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise QuintechError(f"HTTPS connection failed: {exc.reason}") from exc

        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QuintechError("HTTPS API returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise QuintechError("HTTPS API returned an unexpected response")
        return result

    @staticmethod
    def check_command(result: dict[str, object], command: str) -> None:
        status = result.get(f"{command}Error")
        if not isinstance(status, dict):
            raise QuintechError(f"no status returned for {command}")
        if status.get("code") != "Success":
            raise QuintechError(f"{command} failed: {status.get('code', 'unknown')}")

    def authenticate(self, username: str, password: str) -> None:
        result = self.post(
            [
                {"AuthenticateUser": {"username": username, "password": password}},
                {"GetFanInCrosspoints": {}},
            ]
        )
        user_id = result.get("UserId", -1)
        if not isinstance(user_id, int) or user_id < 0:
            raise QuintechError("authentication failed")
        self.check_command(result, "GetFanInCrosspoints")
        self.credentials = (username, password)

    def authenticated_post(
        self, commands: list[dict[str, object]]
    ) -> dict[str, object]:
        """Run commands with authentication in the same switch request."""
        if self.credentials is None:
            raise QuintechError("client is not authenticated")
        username, password = self.credentials
        result = self.post(
            [
                {
                    "AuthenticateUser": {
                        "username": username,
                        "password": password,
                    }
                },
                *commands,
            ]
        )
        user_id = result.get("UserId", -1)
        if not isinstance(user_id, int) or user_id < 0:
            raise QuintechError("authentication failed")
        return result

    def get_fanin_crosspoints(self) -> list[dict[str, object]]:
        result = self.authenticated_post([{"GetFanInCrosspoints": {}}])
        self.check_command(result, "GetFanInCrosspoints")
        crosspoints = result.get("FanInCrosspoints")
        if not isinstance(crosspoints, list):
            raise QuintechError("switch did not return fan-in crosspoints")
        return crosspoints

    def get_rf_levels(self) -> tuple[list[object], list[object]]:
        """Return the physical input and output RF-level readings."""
        result = self.authenticated_post(
            [
                {"GetFanInInputSignalValues": {}},
                {"GetFanInOutputSignalValues": {}},
            ]
        )
        self.check_command(result, "GetFanInInputSignalValues")
        self.check_command(result, "GetFanInOutputSignalValues")
        input_levels = result.get("FanInInputSignalValues")
        output_levels = result.get("FanInOutputSignalValues")
        if not isinstance(input_levels, list) or not isinstance(output_levels, list):
            raise QuintechError(
                "switch did not return fan-in input and output RF levels"
            )
        return input_levels, output_levels

    def set_fanin_crosspoint(self, input_index: int, output_index: int) -> None:
        result = self.authenticated_post(
            [
                {
                    "SetCrosspoint": {
                        "matrix": "FanIn",
                        "input": input_index,
                        "output": output_index,
                        "change_queue_id": 128,
                    }
                }
            ]
        )
        self.check_command(result, "SetCrosspoint")


def connect_crosspoint(
    host: str,
    timeout: float,
    verify_tls: bool,
    username: str,
    password: str,
    output_number: int,
    input_numbers: Sequence[int],
    expected_input_levels: Sequence[float] | None = None,
    level_tolerance: float = 3.0,
    level_settle_seconds: float = 0.0,
    check_rf_only: bool = False,
) -> None:
    if expected_input_levels is not None and len(expected_input_levels) != len(
        input_numbers
    ):
        raise QuintechError(
            "one expected RF level is required for each selected input"
        )
    if check_rf_only and expected_input_levels is None:
        raise QuintechError("--check-rf-only requires --expected-input-levels")
    client = QuintechWebClient(host, timeout, verify_tls)
    client.authenticate(username, password)
    print(f"HTTPS connection and authentication verified for {host}")

    if check_rf_only:
        assert expected_input_levels is not None
        verify_rf_levels(
            client,
            output_number,
            input_numbers,
            expected_input_levels,
            level_tolerance,
            level_settle_seconds,
        )
        return

    # CLI port numbers are one-based, while the web API uses zero-based indexes.
    # On the FanIn matrix, the API coordinates are reversed relative to the
    # physical RF labels: API input is the physical output, and API output is
    # the physical input.
    output_index = output_number - 1
    input_indexes = [input_number - 1 for input_number in input_numbers]
    for input_index in input_indexes:
        client.set_fanin_crosspoint(
            input_index=output_index, output_index=input_index
        )

    crosspoints = client.get_fanin_crosspoints()
    missing: list[int] = []
    for input_number, input_index in zip(input_numbers, input_indexes):
        verified = any(
            isinstance(crosspoint, dict)
            and crosspoint.get("input") == output_index
            and crosspoint.get("output") == input_index
            for crosspoint in crosspoints
        )

        # The switch normally returns one item per API output, with the output
        # index implied by its position in the list.
        if input_index < len(crosspoints) and isinstance(
            crosspoints[input_index], dict
        ):
            selected = crosspoints[input_index].get("input")
            verified = verified or selected == output_index

        if not verified:
            missing.append(input_number)
    if missing:
        raise QuintechError(
            f"read-back failed: output {output_number} does not report "
            f"input(s) {', '.join(map(str, missing))}"
        )
    input_list = ", ".join(map(str, input_numbers))
    print(
        f"Connected fan-in output {output_number} to input(s) "
        f"{input_list}"
    )

    if expected_input_levels is not None:
        verify_rf_levels(
            client,
            output_number,
            input_numbers,
            expected_input_levels,
            level_tolerance,
            level_settle_seconds,
        )


def verify_rf_levels(
    client: QuintechWebClient,
    output_number: int,
    input_numbers: Sequence[int],
    expected_input_levels: Sequence[float],
    level_tolerance: float,
    level_settle_seconds: float,
) -> None:
    """Read and verify RF levels without changing any crosspoints."""
    if level_settle_seconds:
        time.sleep(level_settle_seconds)
    input_levels, output_levels = client.get_rf_levels()
    measured_inputs = [
        rf_level_at(input_levels, number, "input") for number in input_numbers
    ]
    measured_output = rf_level_at(output_levels, output_number, "output")
    expected_output = combined_dbm(expected_input_levels)

    failures: list[str] = []
    for number, expected, measured in zip(
        input_numbers, expected_input_levels, measured_inputs
    ):
        print(
            f"Input {number} RF level: {measured:.1f} dBm "
            f"(expected {expected:.1f} dBm)"
        )
        if abs(measured - expected) > level_tolerance:
            failures.append(
                f"input {number} measured {measured:.1f} dBm, "
                f"expected {expected:.1f} dBm"
            )
    print(
        f"Output {output_number} RF level: {measured_output:.1f} dBm "
        f"(expected combined level {expected_output:.1f} dBm)"
    )
    if abs(measured_output - expected_output) > level_tolerance:
        failures.append(
            f"output {output_number} measured {measured_output:.1f} dBm, "
            f"expected {expected_output:.1f} dBm"
        )
    if failures:
        raise QuintechError(
            f"RF level verification failed (tolerance {level_tolerance:g} dB): "
            + "; ".join(failures)
        )
    print(f"RF levels verified within {level_tolerance:g} dB")


def combined_dbm(levels: Sequence[float]) -> float:
    """Combine independent power levels expressed in dBm."""
    if not levels:
        raise QuintechError("at least one RF level is required")
    return 10.0 * math.log10(sum(10.0 ** (level / 10.0) for level in levels))


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
        description="Connect one output to one or more inputs on the XTREME 32 fan-in matrix."
    )
    parser.add_argument(
        "output",
        type=int,
        choices=range(1, 9),
        metavar="OUTPUT",
        help="physical fan-in output number (1-32)",
    )
    parser.add_argument(
        "inputs",
        type=int,
        choices=range(1, 9),
        nargs="+",
        metavar="INPUT",
        help="one or more physical fan-in input numbers (1-32)",
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
        "--expected-input-levels",
        type=float,
        nargs="+",
        metavar="DBM",
        help="verify each connected input and the combined output RF level",
    )
    parser.add_argument(
        "--check-rf-only",
        action="store_true",
        help="check RF levels without changing or verifying fan-in crosspoints",
    )
    parser.add_argument(
        "--level-tolerance",
        type=float,
        default=3.0,
        metavar="DB",
        help="maximum RF-level error (default: 3 dB)",
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
            args.output,
            args.inputs,
            args.expected_input_levels,
            args.level_tolerance,
            args.level_settle_seconds,
            args.check_rf_only,
        )
    except (OSError, QuintechError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
