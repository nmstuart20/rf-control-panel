#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import signal
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "static"
MAX_LOG_LINES = 2000
ACTIVE_RUN_STATES = {"starting", "running", "stopping", "cleanup"}
RUN_DIRECTORY_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{12}$")
MAX_HISTORY_ENTRIES = 200
STOP_GRACE_SECONDS = 5
SCENARIO_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ARGUMENT_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PLACEHOLDER_PATTERN = re.compile(r"^\{([^{}]+)\}$")
# A placeholder beginning with "@" names a device in hardware_checks and is
# replaced by that device's configured address, so a re-addressed modem or
# switch is changed in one place instead of in every step that talks to it.
DEVICE_PREFIX = "@"


def placeholder_name(value: object) -> str | None:
    """Return the name inside a whole-token {placeholder}, or None."""
    if not isinstance(value, str):
        return None
    match = PLACEHOLDER_PATTERN.fullmatch(value)
    return match.group(1) if match else None


def substitute(value: str, arguments: dict[str, str], addresses: dict[str, str]) -> str:
    """Resolve one {argument} or {@Device name} token."""
    name = placeholder_name(value)
    if name is None:
        return value
    if name.startswith(DEVICE_PREFIX):
        return addresses.get(name[len(DEVICE_PREFIX):], value)
    return arguments.get(name, value)


def address_host(address: object) -> str | None:
    """Return just the hostname from a device address, with or without a scheme."""
    if not isinstance(address, str) or not address:
        return None
    parsed = urlparse(address if "//" in address else f"//{address}")
    return parsed.hostname


def device_addresses(checks: object) -> dict[str, str]:
    """Map each configured device name to its address, where one is set."""
    if not isinstance(checks, dict):
        return {}
    addresses = {}
    for name, check in checks.items():
        if isinstance(check, dict) and isinstance(check.get("address"), str) and check["address"]:
            addresses[name] = check["address"]
    return addresses


def _finite_number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def validate_scenario(item: object, checks: dict | None = None) -> dict:
    if not isinstance(item, dict):
        raise ValueError("scenario must be an object")

    scenario_id = item.get("id")
    if not isinstance(scenario_id, str) or not SCENARIO_ID_PATTERN.fullmatch(scenario_id):
        raise ValueError("scenario id must contain lowercase letters, numbers, and single hyphens")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"scenario {scenario_id} needs a name")
    if len(name.strip()) > 120:
        raise ValueError("scenario name must be 120 characters or fewer")
    description = item.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > 1000):
        raise ValueError(f"scenario {scenario_id} has an invalid description")

    equipment = item.get("equipment", [])
    if not isinstance(equipment, list) or not all(isinstance(value, str) and value for value in equipment):
        raise ValueError(f"scenario {scenario_id} has invalid equipment")
    if len(equipment) != len(set(equipment)):
        raise ValueError(f"scenario {scenario_id} contains duplicate equipment")
    if checks is not None:
        unknown = [value for value in equipment if value not in checks]
        if unknown:
            raise ValueError(f"unknown hardware: {', '.join(unknown)}")
    addresses = device_addresses(checks) if checks is not None else None

    arguments = item.get("arguments", [])
    if not isinstance(arguments, list):
        raise ValueError(f"scenario {scenario_id} has invalid arguments")
    argument_ids: set[str] = set()
    for argument in arguments:
        if not isinstance(argument, dict):
            raise ValueError(f"scenario {scenario_id} has an invalid argument")
        argument_id = argument.get("id")
        argument_type = argument.get("type", "number")
        if (
            not isinstance(argument_id, str)
            or not ARGUMENT_ID_PATTERN.fullmatch(argument_id)
            or argument_id in argument_ids
            or argument_type not in {"number", "integer"}
        ):
            raise ValueError(f"scenario {scenario_id} has an invalid argument")
        argument_ids.add(argument_id)
        if "label" in argument and not isinstance(argument["label"], str):
            raise ValueError(f"argument {argument_id} has an invalid label")
        if "unit" in argument and not isinstance(argument["unit"], str):
            raise ValueError(f"argument {argument_id} has an invalid unit")
        default = _finite_number(argument.get("default"), f"argument {argument_id} default")
        minimum = _finite_number(argument["min"], f"argument {argument_id} minimum") if "min" in argument else None
        maximum = _finite_number(argument["max"], f"argument {argument_id} maximum") if "max" in argument else None
        step = _finite_number(argument["step"], f"argument {argument_id} step") if "step" in argument else None
        if argument_type == "integer" and any(
            value is not None and not float(value).is_integer() for value in (default, minimum, maximum, step)
        ):
            raise ValueError(f"integer argument {argument_id} must use whole numbers")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"argument {argument_id} minimum cannot exceed its maximum")
        if minimum is not None and default < minimum or maximum is not None and default > maximum:
            raise ValueError(f"argument {argument_id} default is outside its allowed range")
        if step is not None and step <= 0:
            raise ValueError(f"argument {argument_id} step must be greater than zero")

    steps = item.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError(f"scenario {scenario_id} needs at least one step")
    stop_steps = item.get("stop_steps", [])
    if not isinstance(stop_steps, list):
        raise ValueError(f"scenario {scenario_id} has invalid stop steps")
    for step in [*steps, *stop_steps]:
        command = step.get("command") if isinstance(step, dict) else None
        if not isinstance(command, list) or not command or not all(
            isinstance(value, str) and value for value in command
        ):
            raise ValueError(f"scenario {scenario_id} has an invalid command")
        if "name" in step and (not isinstance(step["name"], str) or not step["name"].strip()):
            raise ValueError(f"scenario {scenario_id} has an invalid step name")
        if "background" in step and not isinstance(step["background"], bool):
            raise ValueError(f"scenario {scenario_id} has an invalid background flag")
        environment = step.get("environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError(f"scenario {scenario_id} has an invalid environment")
        for value in [*command, *environment.values()]:
            name = placeholder_name(value)
            if name is None:
                continue
            if name.startswith(DEVICE_PREFIX):
                device = name[len(DEVICE_PREFIX):]
                if addresses is not None and device not in addresses:
                    raise ValueError(
                        f"scenario {scenario_id} references device {device}, "
                        "which has no address in hardware_checks"
                    )
            elif name not in argument_ids:
                raise ValueError(f"scenario {scenario_id} references unknown argument {name}")

    return item


def validate_safe_state(steps: object, checks: dict) -> list[dict]:
    """Validate the catalog-level commands that return the range to a safe state."""
    if steps is None:
        return []
    if not isinstance(steps, list):
        raise ValueError("safe_state must be a list of steps")
    addresses = device_addresses(checks)
    for index, step in enumerate(steps, start=1):
        command = step.get("command") if isinstance(step, dict) else None
        if not isinstance(command, list) or not command or not all(
            isinstance(value, str) and value for value in command
        ):
            raise ValueError(f"safe_state step {index} has an invalid command")
        if "name" in step and (not isinstance(step["name"], str) or not step["name"].strip()):
            raise ValueError(f"safe_state step {index} has an invalid name")
        if "optional" in step and not isinstance(step["optional"], bool):
            raise ValueError(f"safe_state step {index} has an invalid optional flag")
        environment = step.get("environment", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise ValueError(f"safe_state step {index} has an invalid environment")
        for value in [*command, *environment.values()]:
            name = placeholder_name(value)
            if name is None:
                continue
            if not name.startswith(DEVICE_PREFIX):
                raise ValueError(
                    f"safe_state step {index} references {{{name}}}; safe state "
                    "commands run without scenario arguments"
                )
            device = name[len(DEVICE_PREFIX):]
            if device not in addresses:
                raise ValueError(
                    f"safe_state step {index} references device {device}, "
                    "which has no address in hardware_checks"
                )
    return steps


def load_catalog(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("scenarios.json must contain a 'scenarios' list")
    checks = raw.get("hardware_checks", {})
    if not isinstance(checks, dict):
        raise ValueError("hardware_checks must be an object")
    validate_safe_state(raw.get("safe_state"), checks)
    result = {}
    for item in scenarios:
        validate_scenario(item, checks)
        if item["id"] in result:
            raise ValueError(f"duplicate scenario id: {item['id']}")
        result[item["id"]] = item
    return result


@dataclass
class Run:
    id: str
    scenario_id: str
    scenario_name: str
    state: str = "starting"
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    current_step: str | None = None
    exit_code: int | None = None
    stop_requested: bool = False
    safe_state: str = "pending"
    force_killed: bool = False
    logs: list[str] = field(default_factory=list)
    arguments: dict[str, str] = field(default_factory=dict)
    steps: list[dict] = field(default_factory=list)
    process: subprocess.Popen | None = field(default=None, repr=False)
    processes: list[subprocess.Popen] = field(default_factory=list, repr=False)
    directory: Path | None = field(default=None, repr=False)
    log_file: object = field(default=None, repr=False)

    def public(self) -> dict:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "state": self.state,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "current_step": self.current_step,
            "exit_code": self.exit_code,
            "stop_requested": self.stop_requested,
            "safe_state": self.safe_state,
            "force_killed": self.force_killed,
            "logs": list(self.logs),
        }

    def record(self) -> dict:
        """The archived form: what ran, with which values, and how it ended."""
        summary = self.public()
        summary.pop("logs", None)
        summary["arguments"] = dict(self.arguments)
        summary["steps"] = [
            {
                "name": step.get("name"),
                "command": list(step.get("command", [])),
                "background": bool(step.get("background", False)),
                "environment": dict(step.get("environment", {})),
            }
            for step in self.steps
        ]
        return summary


class Runner:
    def __init__(self, catalog_path: Path, runs_path: Path | None = None):
        self.catalog_path = catalog_path
        self.runs_path = runs_path
        self.lock = threading.RLock()
        self.safe_state_lock = threading.Lock()
        self.run: Run | None = None
        self.hardware_cache: tuple[float, list[dict]] | None = None
        self.rf_switch_password = os.environ.get("RF_SWITCH_PASSWORD")
        self.rf_switch_sessions: set[str] = set()

    def catalog(self) -> dict[str, dict]:
        return load_catalog(self.catalog_path)

    def _config(self) -> dict:
        with self.catalog_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("invalid scenario catalog")
        checks = config.get("hardware_checks", {})
        if not isinstance(checks, dict):
            raise ValueError("hardware_checks must be an object")
        return config

    def addresses(self) -> dict[str, str]:
        return device_addresses(self._config().get("hardware_checks", {}))

    def scenarios(self) -> list[dict]:
        """Scenario metadata for the panel. Step commands are never sent to the browser."""
        safe = []
        for item in self.catalog().values():
            entry = {key: item.get(key) for key in ("id", "name", "description", "equipment", "arguments")}
            entry["step_names"] = [
                step.get("name", f"Step {index}")
                for index, step in enumerate(item.get("steps", []), start=1)
            ]
            safe.append(entry)
        return safe

    def create_scenario(self, value: object) -> dict:
        """Validate and atomically append a scenario to the JSON catalog."""
        with self.lock:
            config = self._config()
            scenarios = config.get("scenarios")
            checks = config["hardware_checks"]
            if not isinstance(scenarios, list):
                raise ValueError("invalid scenario catalog")
            scenario = validate_scenario(value, checks)
            if not scenario.get("equipment"):
                raise ValueError("select at least one configured hardware item")
            if any(existing.get("id") == scenario["id"] for existing in scenarios if isinstance(existing, dict)):
                raise RuntimeError(f"scenario id already exists: {scenario['id']}")

            # Copy user input into JSON-compatible values and normalize optional text.
            saved = json.loads(json.dumps(scenario))
            saved["name"] = saved["name"].strip()
            if not saved.get("description", "").strip():
                saved.pop("description", None)
            scenarios.append(saved)
            self._write_catalog(config)
            self.hardware_cache = None
            return saved

    def _write_catalog(self, config: dict) -> None:
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.catalog_path.parent,
                prefix=f".{self.catalog_path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(config, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, self.catalog_path.stat().st_mode & 0o777)
            os.replace(temporary_name, self.catalog_path)
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    def hardware(self) -> list[dict]:
        """Check every piece of equipment configured in hardware_checks."""
        now = time.monotonic()
        with self.lock:
            if self.hardware_cache and now - self.hardware_cache[0] < 5:
                return self.hardware_cache[1]

        checks = self._config()["hardware_checks"]
        addresses = device_addresses(checks)
        names = list(checks)

        results = [self._check_hardware(name, checks.get(name), addresses) for name in names]
        with self.lock:
            self.hardware_cache = (now, results)
        return results

    def hardware_names(self) -> list[str]:
        """Return configured hardware names without waiting for connection probes."""
        return list(self._config()["hardware_checks"])

    def _check_hardware(self, name: str, check: object, addresses: dict[str, str] | None = None) -> dict:
        if not isinstance(check, dict):
            return {"name": name, "state": "not_configured", "detail": "Check not configured"}
        with self.lock:
            if self.run and self.run.state in ACTIVE_RUN_STATES:
                return {"name": name, "state": "in_use", "detail": "Connection check paused during active run"}
        tx_state = None
        try:
            check_type = check.get("type")
            if check_type == "signalhound":
                command = [
                    sys.executable, "-c",
                    "from vsgdevice.vsg_api import vsg_open_device,vsg_close_device; "
                    "h=vsg_open_device()['handle']; vsg_close_device(h)",
                ]
                self._run_check(command, float(check.get("timeout", 5)))
            elif check_type == "command":
                command = check.get("command")
                if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
                    raise ValueError("invalid check command")
                command = [substitute(value, {}, addresses or {}) for value in command]
                output = self._run_check(command, float(check.get("timeout", 5)))
                tx_state = self._tx_state(output)
            elif check_type == "tcp":
                host, port = check.get("host"), check.get("port")
                if host is None:
                    host = address_host(check.get("address"))
                if not isinstance(host, str) or not isinstance(port, int):
                    raise ValueError("invalid TCP host or port")
                with socket.create_connection((host, port), timeout=float(check.get("timeout", 3))):
                    pass
            else:
                raise ValueError("unknown check type")
            result = {"name": name, "state": "connected", "detail": "Connected"}
            if tx_state is not None:
                result["tx_state"] = tx_state
            return result
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            detail = str(exc).strip() or "Connection check failed"
            return {"name": name, "state": "disconnected", "detail": detail[:180]}

    @staticmethod
    def _run_check(command: list[str], timeout: float) -> str:
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False,
        )
        if completed.returncode:
            raise OSError(completed.stdout.strip() or f"Exited with status {completed.returncode}")
        return completed.stdout

    @staticmethod
    def _tx_state(output: str) -> str | None:
        if "tx on" in output.lower():
            return "on"
        if "tx off" in output.lower():
            return "off"
        return None

    def status(self) -> dict | None:
        with self.lock:
            return self.run.public() if self.run else None

    def unlock_rf_switch(self, password: object) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.rf_switch_sessions.add(token)
        return token

    def rf_switch_unlocked(self, token: str | None) -> bool:
        with self.lock:
            return bool(token and token in self.rf_switch_sessions)

    def start(self, scenario_id: str, supplied_arguments: object = None) -> dict:
        config = self._config()
        scenario = self.catalog().get(scenario_id)
        if scenario is None:
            raise KeyError("unknown scenario")
        values = self._validate_arguments(scenario, supplied_arguments)
        addresses = device_addresses(config["hardware_checks"])
        prepared = dict(scenario)
        for key in ("steps", "stop_steps"):
            prepared[key] = [
                self._prepare_step(step, values, addresses) for step in scenario.get(key, [])
            ]
        with self.lock:
            if self.run and self.run.state in ACTIVE_RUN_STATES:
                raise RuntimeError("another scenario is already running")
            run = Run(
                id=uuid.uuid4().hex[:12],
                scenario_id=scenario_id,
                scenario_name=scenario.get("name", scenario_id),
                arguments=dict(values),
                steps=list(prepared["steps"]),
            )
            self.run = run
            self._begin_archive(run)
            threading.Thread(target=self._execute, args=(run, prepared), daemon=True).start()
            return run.public()

    @staticmethod
    def _prepare_step(step: dict, values: dict[str, str], addresses: dict[str, str]) -> dict:
        """Resolve every {argument} and {@Device} token in one step."""
        prepared = dict(step)
        prepared["command"] = [substitute(token, values, addresses) for token in step["command"]]
        prepared["environment"] = {
            name: substitute(token, values, addresses)
            for name, token in step.get("environment", {}).items()
        }
        return prepared

    @staticmethod
    def _validate_arguments(scenario: dict, supplied: object) -> dict[str, str]:
        supplied = supplied if isinstance(supplied, dict) else {}
        definitions = scenario.get("arguments", [])
        allowed = {item["id"] for item in definitions}
        if any(key not in allowed for key in supplied):
            raise ValueError("unknown scenario argument")
        values = {}
        for item in definitions:
            argument_id = item["id"]
            raw = supplied.get(argument_id, item.get("default"))
            try:
                value = int(raw) if item.get("type") == "integer" else float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{item.get('label', argument_id)} must be a number") from exc
            if "min" in item and value < item["min"]:
                raise ValueError(f"{item.get('label', argument_id)} must be at least {item['min']}")
            if "max" in item and value > item["max"]:
                raise ValueError(f"{item.get('label', argument_id)} must be no more than {item['max']}")
            # Avoid adding a trailing ".0" to whole-valued numeric arguments.
            # Command-line tools that require integers should receive the same
            # representation the user entered in the number field.
            values[argument_id] = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        return values

    def stop(self) -> dict:
        with self.lock:
            run = self.run
            if not run or run.state not in ACTIVE_RUN_STATES:
                raise RuntimeError("no scenario is running")
            run.stop_requested = True
            run.state = "stopping"
        # Escalate on a separate thread. The run thread is blocked reading the
        # step's output, and a command that ignores SIGTERM keeps its stdout
        # open, so the run thread cannot escalate on its own behalf.
        threading.Thread(target=self._terminate_processes, args=(run,), daemon=True).start()
        return self.status()

    def _begin_archive(self, run: Run) -> None:
        """Create this run's directory and start streaming its log to disk."""
        if self.runs_path is None:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run.started_at))
        directory = self.runs_path / f"{stamp}-{run.id}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle = (directory / "logs.txt").open("a", encoding="utf-8")
        except OSError as exc:
            print(f"WARNING: could not archive run {run.id}: {exc}", file=sys.stderr)
            return
        with self.lock:
            run.directory = directory
            run.log_file = handle
        self._write_record(run)

    def _write_record(self, run: Run) -> None:
        if run.directory is None:
            return
        with self.lock:
            record = run.record()
        try:
            path = run.directory / "run.json"
            with path.open("w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
        except OSError as exc:
            print(f"WARNING: could not write run record {run.id}: {exc}", file=sys.stderr)

    def _finish_archive(self, run: Run) -> None:
        """Write the final record and close the log file."""
        self._write_record(run)
        with self.lock:
            handle, run.log_file = run.log_file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def history(self, limit: int = MAX_HISTORY_ENTRIES) -> list[dict]:
        """Past run records, newest first."""
        if self.runs_path is None or not self.runs_path.is_dir():
            return []
        directories = sorted(
            (item for item in self.runs_path.iterdir()
             if item.is_dir() and RUN_DIRECTORY_PATTERN.fullmatch(item.name)),
            reverse=True,
        )
        records = []
        for directory in directories[:limit]:
            record = self._read_record(directory)
            if record is not None:
                records.append(record)
        return records

    def history_entry(self, run_id: str) -> dict:
        """One past run, with its complete log."""
        if self.runs_path is None or not self.runs_path.is_dir():
            raise KeyError("unknown run")
        for directory in self.runs_path.iterdir():
            if not directory.is_dir() or not RUN_DIRECTORY_PATTERN.fullmatch(directory.name):
                continue
            if not directory.name.endswith(f"-{run_id}"):
                continue
            record = self._read_record(directory)
            if record is None:
                break
            try:
                record["logs"] = (directory / "logs.txt").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                record["logs"] = []
            return record
        raise KeyError("unknown run")

    @staticmethod
    def _read_record(directory: Path) -> dict | None:
        try:
            with (directory / "run.json").open(encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        # A record still marked active means the server stopped mid-run.
        if record.get("state") in ACTIVE_RUN_STATES:
            record["state"] = "interrupted"
        return record

    def request_safe_state(self) -> dict:
        """Return the range to its safe state now, whatever is running."""
        with self.lock:
            if self.run and self.run.state in ACTIVE_RUN_STATES:
                self._log(self.run, "Safe state requested; stopping the active run")
                return self.stop()
            run = Run(
                id=uuid.uuid4().hex[:12],
                scenario_id="safe-state",
                scenario_name="Safe state",
            )
            self.run = run
            self._begin_archive(run)
            threading.Thread(target=self._execute_safe_state, args=(run,), daemon=True).start()
            return run.public()

    def _execute_safe_state(self, run: Run) -> None:
        with self.lock:
            run.state = "cleanup"
        try:
            self._run_safe_state(run)
        finally:
            with self.lock:
                run.state = "failed" if run.safe_state == "failed" else "completed"
                run.current_step = None
                run.process = None
                run.processes.clear()
                run.finished_at = time.time()
            self._log(run, f"Safe state {run.state}")
            self._finish_archive(run)

    def _log(self, run: Run, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text.rstrip()}"
        with self.lock:
            run.logs.append(line)
            del run.logs[:-MAX_LOG_LINES]
            handle = run.log_file
            if handle is None:
                return
            try:
                handle.write(f"{line}\n")
                handle.flush()
            except OSError:
                run.log_file = None

    def _execute(self, run: Run, scenario: dict) -> None:
        with self.lock:
            run.state = "stopping" if run.stop_requested else "running"
        self._log(run, f"Starting {run.scenario_name}")
        background: list[tuple[str, subprocess.Popen, threading.Thread]] = []
        outcome = "failed"
        try:
            for index, step in enumerate(scenario["steps"], start=1):
                if run.stop_requested:
                    break
                name = step.get("name", f"Step {index}")
                with self.lock:
                    run.current_step = name
                self._log(run, f"Step {index}: {name}")
                env = os.environ.copy()
                env.update({str(k): str(v) for k, v in step.get("environment", {}).items()})
                process = subprocess.Popen(
                    step["command"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    # No terminal is attached, so a command that asks for input
                    # would hang instead of failing with a readable message.
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                with self.lock:
                    run.process = process
                    run.processes.append(process)
                assert process.stdout is not None
                if step.get("background", False):
                    reader = threading.Thread(
                        target=self._read_output, args=(run, process), daemon=True
                    )
                    reader.start()
                    background.append((name, process, reader))
                    self._log(run, f"{name} continues in background")
                else:
                    self._read_output(run, process)
                    code = process.wait()
                    self._remove_process(run, process, code)
                    if code != 0 and not run.stop_requested:
                        raise RuntimeError(f"{name} exited with status {code}")

            for name, process, reader in background:
                code = process.wait()
                reader.join()
                self._remove_process(run, process, code)
                if code != 0 and not run.stop_requested:
                    raise RuntimeError(f"{name} exited with status {code}")
            outcome = "completed"
        except Exception as exc:
            self._log(run, f"ERROR: {exc}")
            self._terminate_processes(run)
            outcome = "failed"
        finally:
            with self.lock:
                run.state = "cleanup"
            self._terminate_processes(run)
            if run.stop_requested or outcome == "failed":
                try:
                    self._execute_stop_steps(run, scenario.get("stop_steps", []))
                    if outcome != "failed":
                        outcome = "stopped"
                except Exception as exc:
                    self._log(run, f"ERROR: cleanup command failed: {exc}")
                    outcome = "failed"
            # The safe state runs after every scenario, a clean completion
            # included. A killed command skips its own shutdown path, so this is
            # the only step that confirms nothing is left transmitting.
            self._run_safe_state(run)
            if run.safe_state == "failed":
                outcome = "failed"
            with self.lock:
                run.state = outcome
                run.current_step = None
                run.process = None
                run.processes.clear()
                run.finished_at = time.time()
            self._log(run, f"Scenario {run.state}")
            self._finish_archive(run)

    def _execute_stop_steps(self, run: Run, steps: list[dict]) -> None:
        for index, step in enumerate(steps, start=1):
            name = step.get("name", f"Stop step {index}")
            with self.lock:
                run.current_step = name
            self._log(run, f"Cleanup step {index}: {name}")
            self._run_step_to_completion(run, step, name)

    def _read_output(self, run: Run, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self._log(run, line)
        finally:
            # The pipe is drained; closing it here keeps a long-lived server
            # from accumulating one file descriptor per step.
            try:
                process.stdout.close()
            except OSError:
                pass

    def _remove_process(self, run: Run, process: subprocess.Popen, code: int) -> None:
        with self.lock:
            if process in run.processes:
                run.processes.remove(process)
            if run.process is process:
                run.process = run.processes[-1] if run.processes else None
            run.exit_code = code

    def _terminate_processes(self, run: Run) -> bool:
        """Terminate every tracked process. Returns True if SIGKILL was needed."""
        with self.lock:
            processes = list(run.processes)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        force_killed = False
        for process in processes:
            try:
                process.wait(timeout=STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                force_killed = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                # SIGKILL cannot be caught, so the command's own shutdown path
                # never ran. Anything it was driving may still be transmitting.
                self._log(
                    run,
                    "WARNING: a command ignored SIGTERM and was killed; its own "
                    "hardware cleanup did not run",
                )
                with self.lock:
                    run.force_killed = True
        return force_killed

    def safe_state_steps(self) -> list[dict]:
        """Catalog-level commands that return the range to a non-transmitting state."""
        config = self._config()
        checks = config["hardware_checks"]
        steps = validate_safe_state(config.get("safe_state"), checks)
        addresses = device_addresses(checks)
        return [self._prepare_step(step, {}, addresses) for step in steps]

    def _run_safe_state(self, run: Run) -> None:
        """Run the safe state commands. This runs at the end of every run."""
        try:
            steps = self.safe_state_steps()
        except (OSError, ValueError) as exc:
            self._log(run, f"ERROR: safe state is not usable: {exc}")
            with self.lock:
                run.safe_state = "failed"
            return
        if not steps:
            with self.lock:
                run.safe_state = "skipped"
            return
        degraded = False
        with self.safe_state_lock:
            self._log(run, "Returning hardware to its safe state")
            for index, step in enumerate(steps, start=1):
                name = step.get("name", f"Safe state step {index}")
                with self.lock:
                    run.current_step = name
                self._log(run, f"Safe state {index}: {name}")
                try:
                    self._run_step_to_completion(run, step, name)
                except Exception as exc:
                    if step.get("optional", False):
                        self._log(run, f"WARNING: optional safe state step failed: {exc}")
                        degraded = True
                        continue
                    self._log(run, f"ERROR: safe state command failed: {exc}")
                    self._log(
                        run,
                        "WARNING: hardware may still be transmitting; verify it by hand",
                    )
                    with self.lock:
                        run.safe_state = "failed"
                    return
            with self.lock:
                run.safe_state = "degraded" if degraded else "ok"
            if degraded:
                self._log(
                    run,
                    "Hardware is in its safe state, except where an optional "
                    "step could not be reached",
                )
            else:
                self._log(run, "Hardware is in its safe state")

    def _run_step_to_completion(self, run: Run, step: dict, name: str) -> None:
        """Start one command, stream its output, and fail on a non-zero exit."""
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in step.get("environment", {}).items()})
        process = subprocess.Popen(
            step["command"], cwd=PROJECT_ROOT, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1, start_new_session=True,
        )
        with self.lock:
            run.process = process
            run.processes.append(process)
        self._read_output(run, process)
        code = process.wait()
        self._remove_process(run, process, code)
        if code != 0:
            raise RuntimeError(f"{name} exited with status {code}")


class Handler(SimpleHTTPRequestHandler):
    server_version = "RFControlPanel/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    @property
    def runner(self) -> Runner:
        return self.server.runner  # type: ignore[attr-defined]

    def send_response(self, *args, **kwargs):
        self._cache_control_sent = False
        super().send_response(*args, **kwargs)

    def end_headers(self):
        # Static files carry only Last-Modified, which lets a browser reuse them
        # heuristically without asking. A stale app.js then outlives an updated
        # index.html and its handlers never attach, so make every asset revalidate.
        if not getattr(self, "_cache_control_sent", False):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, value, status=HTTPStatus.OK, headers=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cache_control_sent = True
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 262_144:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("JSON body must be an object")
            return body
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc

    def _cookie(self, name: str) -> str | None:
        cookies = self.headers.get("Cookie", "").split(";")
        for cookie in cookies:
            key, separator, value = cookie.strip().partition("=")
            if key == name and separator:
                return unquote(value)
        return None

    def _rf_switch_required(self) -> bool:
        return True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/scenarios":
            try:
                self._json({"scenarios": self.runner.scenarios()})
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/status":
            self._json({"run": self.runner.status()})
        elif path == "/api/hardware":
            try:
                self._json({"hardware": self.runner.hardware()})
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/hardware/options":
            try:
                self._json({"hardware": self.runner.hardware_names()})
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/runs":
            try:
                self._json({"runs": self.runner.history()})
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path.startswith("/api/runs/"):
            run_id = unquote(path[len("/api/runs/"):])
            try:
                self._json({"run": self.runner.history_entry(run_id)})
            except KeyError:
                self._json({"error": "unknown run"}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/rf-switch":
            self._json({"unlocked": True})
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/run":
                self._json({"run": self.runner.start(body.get("scenario_id"), body.get("arguments"))}, HTTPStatus.ACCEPTED)
            elif path == "/api/scenarios":
                scenario = self.runner.create_scenario(body.get("scenario"))
                self._json({"scenario": scenario}, HTTPStatus.CREATED)
            elif path == "/api/stop":
                self._json({"run": self.runner.stop()}, HTTPStatus.ACCEPTED)
            elif path == "/api/safe-state":
                self._json({"run": self.runner.request_safe_state()}, HTTPStatus.ACCEPTED)
            elif path == "/api/rf-switch/access":
                token = self.runner.unlock_rf_switch(body.get("password"))
                self._json({"unlocked": True}, HTTPStatus.OK, {"Set-Cookie": f"rf_switch_session={token}; HttpOnly; SameSite=Strict; Path=/api/rf-switch"})
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except PermissionError as exc:
            self._json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except KeyError as exc:
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (ValueError, RuntimeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.CONFLICT)

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="RF scenario control panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--config", type=Path, default=PROJECT_ROOT / "scenarios" / "scenarios.json"
    )
    parser.add_argument(
        "--runs", type=Path, default=PROJECT_ROOT / "runs",
        help="directory for archived run records (default: runs/)",
    )
    args = parser.parse_args()
    runs_path = args.runs.resolve()
    runs_path.mkdir(parents=True, exist_ok=True)
    runner = Runner(args.config.resolve(), runs_path)
    runner.catalog()  # fail early on an invalid catalog
    runner.safe_state_steps()  # fail early on an invalid safe state
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.runner = runner  # type: ignore[attr-defined]
    print(f"RF control panel listening on http://{args.host}:{args.port}")
    print(f"Archiving runs to {runs_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        status = runner.status()
        if status and status["state"] in ACTIVE_RUN_STATES:
            runner.stop()
            # Give the run thread time to finish cleanup and the safe state
            # rather than exiting while hardware may still be transmitting.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = runner.status()
                if not status or status["state"] not in ACTIVE_RUN_STATES:
                    break
                time.sleep(0.2)
            else:
                print("WARNING: run did not finish cleanup; check hardware by hand")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
