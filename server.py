#!/usr/bin/env python3
"""Small, dependency-free RF scenario control panel."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
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

    def catalog(self) -> dict[str, dict]:
        return load_catalog(self.catalog_path)

    def scenarios(self) -> list[dict]:
        safe = []
        for item in self.catalog().values():
            safe.append({key: item.get(key) for key in ("id", "name", "description", "equipment")})
        return safe

    def status(self) -> dict | None:
        with self.lock:
            return self.run.public() if self.run else None

    def start(self, scenario_id: str) -> dict:
        scenario = self.catalog().get(scenario_id)
        if scenario is None:
            raise KeyError("unknown scenario")
        with self.lock:
            if self.run and self.run.state in {"starting", "running", "stopping"}:
                raise RuntimeError("another scenario is already running")
            run = Run(
                id=uuid.uuid4().hex[:12],
                scenario_id=scenario_id,
                scenario_name=scenario.get("name", scenario_id),
            )
            self.run = run
            threading.Thread(target=self._execute, args=(run, scenario), daemon=True).start()
            return run.public()

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
                    cwd=ROOT,
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
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/run":
                self._json({"run": self.runner.start(body.get("scenario_id"))}, HTTPStatus.ACCEPTED)
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
    parser.add_argument("--config", type=Path, default=ROOT / "scenarios.json")
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
