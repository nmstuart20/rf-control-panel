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
SCENARIO_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ARGUMENT_ID_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _finite_number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return value


def validate_scenario(item: object, hardware_names: set[str] | None = None) -> dict:
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
    if hardware_names is not None:
        unknown = [value for value in equipment if value not in hardware_names]
        if unknown:
            raise ValueError(f"unknown hardware: {', '.join(unknown)}")

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
    placeholder = re.compile(r"^\{([^{}]+)\}$")
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
            match = placeholder.fullmatch(value)
            if match and match.group(1) not in argument_ids:
                raise ValueError(f"scenario {scenario_id} references unknown argument {match.group(1)}")

    return item


def load_catalog(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("scenarios.json must contain a 'scenarios' list")
    result = {}
    for item in scenarios:
        validate_scenario(item)
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
    logs: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = field(default=None, repr=False)
    processes: list[subprocess.Popen] = field(default_factory=list, repr=False)

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
            "logs": list(self.logs),
        }


class Runner:
    def __init__(self, catalog_path: Path):
        self.catalog_path = catalog_path
        self.lock = threading.RLock()
        self.run: Run | None = None
        self.hardware_cache: tuple[float, list[dict]] | None = None
        self.rf_switch_password = os.environ.get("RF_SWITCH_PASSWORD")
        self.rf_switch_sessions: set[str] = set()

    def catalog(self) -> dict[str, dict]:
        return load_catalog(self.catalog_path)

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
            with self.catalog_path.open(encoding="utf-8") as handle:
                config = json.load(handle)
            scenarios = config.get("scenarios")
            checks = config.get("hardware_checks", {})
            if not isinstance(scenarios, list) or not isinstance(checks, dict):
                raise ValueError("invalid scenario catalog")
            scenario = validate_scenario(value, set(checks))
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

        with self.catalog_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        checks = config.get("hardware_checks", {})
        if not isinstance(checks, dict):
            raise ValueError("hardware_checks must be an object")
        names = list(checks)

        results = [self._check_hardware(name, checks.get(name)) for name in names]
        with self.lock:
            self.hardware_cache = (now, results)
        return results

    def hardware_names(self) -> list[str]:
        """Return configured hardware names without waiting for connection probes."""
        with self.catalog_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        checks = config.get("hardware_checks", {})
        if not isinstance(checks, dict):
            raise ValueError("hardware_checks must be an object")
        return list(checks)

    def _check_hardware(self, name: str, check: object) -> dict:
        if not isinstance(check, dict):
            return {"name": name, "state": "not_configured", "detail": "Check not configured"}
        with self.lock:
            if self.run and self.run.state in {"starting", "running", "stopping"}:
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
                output = self._run_check(command, float(check.get("timeout", 5)))
                tx_state = self._tx_state(output)
            elif check_type == "tcp":
                host, port = check.get("host"), check.get("port")
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
            command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
        scenario = self.catalog().get(scenario_id)
        if scenario is None:
            raise KeyError("unknown scenario")
        values = self._validate_arguments(scenario, supplied_arguments)
        prepared = dict(scenario)
        for key in ("steps", "stop_steps"):
            prepared[key] = []
            for step in scenario.get(key, []):
                prepared_step = dict(step)
                prepared_step["command"] = [values.get(token[1:-1], token) if token.startswith("{") and token.endswith("}") else token for token in step["command"]]
                prepared_step["environment"] = {
                    name: values.get(token[1:-1], token)
                    if token.startswith("{") and token.endswith("}") else token
                    for name, token in step.get("environment", {}).items()
                }
                prepared[key].append(prepared_step)
        with self.lock:
            if self.run and self.run.state in {"starting", "running", "stopping"}:
                raise RuntimeError("another scenario is already running")
            run = Run(
                id=uuid.uuid4().hex[:12],
                scenario_id=scenario_id,
                scenario_name=scenario.get("name", scenario_id),
            )
            self.run = run
            threading.Thread(target=self._execute, args=(run, prepared), daemon=True).start()
            return run.public()

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
            if not run or run.state not in {"starting", "running", "stopping"}:
                raise RuntimeError("no scenario is running")
            run.stop_requested = True
            run.state = "stopping"
            processes = list(run.processes)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return self.status()

    def _log(self, run: Run, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            run.logs.append(f"[{stamp}] {text.rstrip()}")
            del run.logs[:-MAX_LOG_LINES]

    def _execute(self, run: Run, scenario: dict) -> None:
        with self.lock:
            run.state = "stopping" if run.stop_requested else "running"
        self._log(run, f"Starting {run.scenario_name}")
        background: list[tuple[str, subprocess.Popen, threading.Thread]] = []
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
            with self.lock:
                run.state = "completed"
        except Exception as exc:
            self._log(run, f"ERROR: {exc}")
            self._terminate_processes(run)
            with self.lock:
                run.state = "failed"
        finally:
            self._terminate_processes(run)
            failed_before_cleanup = run.state == "failed"
            if run.stop_requested or failed_before_cleanup:
                try:
                    self._execute_stop_steps(run, scenario.get("stop_steps", []))
                    if not failed_before_cleanup:
                        with self.lock:
                            run.state = "stopped"
                except Exception as exc:
                    self._log(run, f"ERROR: cleanup command failed: {exc}")
                    with self.lock:
                        run.state = "failed"
            with self.lock:
                run.current_step = None
                run.process = None
                run.processes.clear()
                run.finished_at = time.time()
            self._log(run, f"Scenario {run.state}")

    def _execute_stop_steps(self, run: Run, steps: list[dict]) -> None:
        for index, step in enumerate(steps, start=1):
            name = step.get("name", f"Stop step {index}")
            with self.lock:
                run.current_step = name
            self._log(run, f"Cleanup step {index}: {name}")
            env = os.environ.copy()
            env.update({str(k): str(v) for k, v in step.get("environment", {}).items()})
            process = subprocess.Popen(
                step["command"], cwd=PROJECT_ROOT, env=env,
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

    def _read_output(self, run: Run, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._log(run, line)

    def _remove_process(self, run: Run, process: subprocess.Popen, code: int) -> None:
        with self.lock:
            if process in run.processes:
                run.processes.remove(process)
            if run.process is process:
                run.process = run.processes[-1] if run.processes else None
            run.exit_code = code

    def _terminate_processes(self, run: Run) -> None:
        with self.lock:
            processes = list(run.processes)
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


class Handler(SimpleHTTPRequestHandler):
    server_version = "RFControlPanel/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    @property
    def runner(self) -> Runner:
        return self.server.runner  # type: ignore[attr-defined]

    def _json(self, value, status=HTTPStatus.OK, headers=None):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
    args = parser.parse_args()
    runner = Runner(args.config.resolve())
    runner.catalog()  # fail early on an invalid catalog
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.runner = runner  # type: ignore[attr-defined]
    print(f"RF control panel listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
        if runner.status() and runner.status()["state"] in {"starting", "running", "stopping"}:
            runner.stop()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
