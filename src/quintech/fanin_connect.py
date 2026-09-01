#!/usr/bin/env python3
"""Connect a fan-in output to multiple inputs"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence


class QuintechError(RuntimeError):
    """Raised when the switch rejects a request or returns invalid data."""


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

    def get_fanin_crosspoints(self) -> list[dict[str, object]]:
        result = self.post([{"GetFanInCrosspoints": {}}])
        self.check_command(result, "GetFanInCrosspoints")
        crosspoints = result.get("FanInCrosspoints")
        if not isinstance(crosspoints, list):
            raise QuintechError("switch did not return fan-in crosspoints")
        return crosspoints

    def set_fanin_crosspoint(self, input_index: int, output_index: int) -> None:
        result = self.post(
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
) -> None:
    client = QuintechWebClient(host, timeout, verify_tls)
    client.authenticate(username, password)
    print(f"HTTPS connection and authentication verified for {host}")

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    password = os.environ.get(args.password_env)
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
        )
    except (OSError, QuintechError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
