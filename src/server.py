#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC = PROJECT_ROOT / "static"
MAX_LOG_LINES = 2000


def load_catalog(path: Path) -> dict[str, dict]:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    scenarios = raw.get("scenarios")
    if not isinstance(scenarios, list):
        raise ValueError("scenarios.json must contain a 'scenarios' list")
    result = {}
    for item in scenarios:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("every scenario needs a string id")
        if item["id"] in result:
            raise ValueError(f"duplicate scenario id: {item['id']}")
        arguments = item.get("arguments", [])
        if not isinstance(arguments, list):
            raise ValueError(f"scenario {item['id']} has invalid arguments")
        argument_ids = set()
        for argument in arguments:
            if not isinstance(argument, dict) or not isinstance(argument.get("id"), str):
                raise ValueError(f"scenario {item['id']} has an invalid argument")
            if argument["id"] in argument_ids or argument.get("type", "number") not in {"number", "integer"}:
                raise ValueError(f"scenario {item['id']} has an invalid argument")
            argument_ids.add(argument["id"])
        steps = item.get("steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError(f"scenario {item['id']} needs at least one step")
        for step in steps:
            command = step.get("command") if isinstance(step, dict) else None
            if not isinstance(command, list) or not command or not all(
                isinstance(value, str) and value for value in command
            ):
                raise ValueError(f"scenario {item['id']} has an invalid command")
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

    def catalog(self) -> dict[str, dict]:
        return load_catalog(self.catalog_path)

    def scenarios(self) -> list[dict]:
        safe = []
        for item in self.catalog().values():
            safe.append({key: item.get(key) for key in ("id", "name", "description", "equipment", "arguments")})
        return safe

    def hardware(self) -> list[dict]:
        """Check each unique piece of equipment referenced by a scenario."""
        now = time.monotonic()
        with self.lock:
            if self.hardware_cache and now - self.hardware_cache[0] < 5:
                return self.hardware_cache[1]

        with self.catalog_path.open(encoding="utf-8") as handle:
            config = json.load(handle)
        checks = config.get("hardware_checks", {})
        names = []
        for scenario in config.get("scenarios", []):
            for name in scenario.get("equipment", []):
                if isinstance(name, str) and name not in names:
                    names.append(name)

        results = [self._check_hardware(name, checks.get(name)) for name in names]
        with self.lock:
            self.hardware_cache = (now, results)
        return results

    def _check_hardware(self, name: str, check: object) -> dict:
        if not isinstance(check, dict):
            return {"name": name, "state": "not_configured", "detail": "Check not configured"}
        with self.lock:
            if self.run and self.run.state in {"starting", "running", "stopping"}:
                return {"name": name, "state": "in_use", "detail": "Connection check paused during active run"}
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
                self._run_check(command, float(check.get("timeout", 5)))
            elif check_type == "tcp":
                host, port = check.get("host"), check.get("port")
                if not isinstance(host, str) or not isinstance(port, int):
                    raise ValueError("invalid TCP host or port")
                with socket.create_connection((host, port), timeout=float(check.get("timeout", 3))):
                    pass
            else:
                raise ValueError("unknown check type")
            return {"name": name, "state": "connected", "detail": "Connected"}
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            detail = str(exc).strip() or "Connection check failed"
            return {"name": name, "state": "disconnected", "detail": detail[:180]}

    @staticmethod
    def _run_check(command: list[str], timeout: float) -> None:
        completed = subprocess.run(
            command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout, check=False,
        )
        if completed.returncode:
            raise OSError(completed.stdout.strip() or f"Exited with status {completed.returncode}")

    def status(self) -> dict | None:
        with self.lock:
            return self.run.public() if self.run else None

    def start(self, scenario_id: str, supplied_arguments: object = None) -> dict:
        scenario = self.catalog().get(scenario_id)
        if scenario is None:
            raise KeyError("unknown scenario")
        values = self._validate_arguments(scenario, supplied_arguments)
        prepared = dict(scenario)
        prepared["steps"] = []
        for step in scenario["steps"]:
            prepared_step = dict(step)
            prepared_step["command"] = [values.get(token[1:-1], token) if token.startswith("{") and token.endswith("}") else token for token in step["command"]]
            prepared["steps"].append(prepared_step)
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
            values[argument_id] = str(value)
        return values

    def stop(self) -> dict:
        with self.lock:
            run = self.run
            if not run or run.state not in {"starting", "running", "stopping"}:
                raise RuntimeError("no scenario is running")
            run.stop_requested = True
            run.state = "stopping"
            process = run.process
        if process and process.poll() is None:
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
            run.state = "running"
        self._log(run, f"Starting {run.scenario_name}")
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
                assert process.stdout is not None
                for line in process.stdout:
                    self._log(run, line)
                code = process.wait()
                with self.lock:
                    run.process = None
                    run.exit_code = code
                if code != 0 and not run.stop_requested:
                    raise RuntimeError(f"{name} exited with status {code}")
            with self.lock:
                run.state = "stopped" if run.stop_requested else "completed"
        except Exception as exc:
            self._log(run, f"ERROR: {exc}")
            with self.lock:
                run.state = "failed"
        finally:
            with self.lock:
                run.current_step = None
                run.process = None
                run.finished_at = time.time()
            self._log(run, f"Scenario {run.state}")


class Handler(SimpleHTTPRequestHandler):
    server_version = "RFControlPanel/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC), **kwargs)

    @property
    def runner(self) -> Runner:
        return self.server.runner  # type: ignore[attr-defined]

    def _json(self, value, status=HTTPStatus.OK):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16_384:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc

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
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/run":
                self._json({"run": self.runner.start(body.get("scenario_id"), body.get("arguments"))}, HTTPStatus.ACCEPTED)
            elif path == "/api/stop":
                self._json({"run": self.runner.stop()}, HTTPStatus.ACCEPTED)
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
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
