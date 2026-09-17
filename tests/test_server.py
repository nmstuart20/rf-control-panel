import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.server import (
    ACTIVE_RUN_STATES,
    Runner,
    load_catalog,
    substitute,
    validate_safe_state,
    validate_scenario,
)


class ScenarioCreationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.catalog_path = Path(self.temporary_directory.name) / "scenarios.json"
        self.catalog_path.write_text(
            json.dumps(
                {
                    "hardware_checks": {
                        "Used radio": {"type": "tcp", "host": "192.0.2.1", "port": 1},
                        "Unused radio": {"type": "tcp", "host": "192.0.2.2", "port": 2},
                    },
                    "scenarios": [
                        {
                            "id": "existing",
                            "name": "Existing",
                            "equipment": ["Used radio"],
                            "steps": [{"name": "Check", "command": ["true"]}],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.runner = Runner(self.catalog_path)

    def tearDown(self):
        self.temporary_directory.cleanup()

    @staticmethod
    def new_scenario():
        return {
            "id": "created-in-ui",
            "name": " Created in UI ",
            "description": "",
            "equipment": ["Unused radio"],
            "arguments": [
                {
                    "id": "frequency",
                    "label": "Frequency",
                    "type": "integer",
                    "default": 100,
                    "min": 10,
                    "max": 1000,
                    "step": 10,
                    "unit": "Hz",
                }
            ],
            "steps": [
                {
                    "name": "Transmit",
                    "background": True,
                    "command": ["radio", "--frequency", "{frequency}"],
                    "environment": {"RF_FREQUENCY": "{frequency}"},
                }
            ],
            "stop_steps": [{"name": "Disable", "command": ["radio", "--off"]}],
        }

    def test_create_scenario_persists_complete_schema(self):
        created = self.runner.create_scenario(self.new_scenario())

        self.assertEqual(created["name"], "Created in UI")
        self.assertNotIn("description", created)
        self.assertEqual(created["steps"][0]["command"][-1], "{frequency}")
        self.assertEqual(created["stop_steps"][0]["name"], "Disable")
        self.assertIn("created-in-ui", load_catalog(self.catalog_path))

    def test_create_rejects_duplicate_id_and_unknown_hardware(self):
        scenario = self.new_scenario()
        scenario["id"] = "existing"
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.runner.create_scenario(scenario)

        scenario = self.new_scenario()
        scenario["equipment"] = ["Not configured"]
        with self.assertRaisesRegex(ValueError, "unknown hardware"):
            self.runner.create_scenario(scenario)

    def test_create_requires_hardware(self):
        scenario = self.new_scenario()
        scenario["equipment"] = []
        with self.assertRaisesRegex(ValueError, "select at least one"):
            self.runner.create_scenario(scenario)

    def test_hardware_checks_include_unreferenced_configured_items(self):
        with patch.object(
            self.runner,
            "_check_hardware",
            side_effect=lambda name, check, addresses=None: {"name": name, "state": "connected"},
        ):
            names = [item["name"] for item in self.runner.hardware()]

        self.assertEqual(names, ["Used radio", "Unused radio"])
        self.assertEqual(self.runner.hardware_names(), ["Used radio", "Unused radio"])


class ScenarioValidationTests(unittest.TestCase):
    def test_unknown_argument_placeholder_is_rejected(self):
        scenario = ScenarioCreationTests.new_scenario()
        scenario["steps"][0]["command"].append("{missing}")
        with self.assertRaisesRegex(ValueError, "unknown argument missing"):
            validate_scenario(scenario)

    def test_integer_argument_rejects_fractional_values(self):
        scenario = ScenarioCreationTests.new_scenario()
        scenario["arguments"][0]["default"] = 1.5
        with self.assertRaisesRegex(ValueError, "whole numbers"):
            validate_scenario(scenario)


class DeviceAddressTests(unittest.TestCase):
    """Device addresses live in hardware_checks and are referenced by name."""

    CHECKS = {
        "Modem": {"type": "command", "address": "http://10.0.1.154", "command": ["true"]},
        "Switch": {"type": "tcp", "address": "192.168.0.248", "port": 443},
        "Generator": {"type": "signalhound"},
    }

    @staticmethod
    def scenario(command):
        return {
            "id": "addressed", "name": "Addressed", "equipment": ["Modem"],
            "steps": [{"name": "Talk to the modem", "command": command}],
        }

    def test_device_reference_resolves_to_the_configured_address(self):
        self.assertEqual(
            substitute("{@Modem}", {}, {"Modem": "http://10.0.1.154"}),
            "http://10.0.1.154",
        )

    def test_arguments_and_devices_share_one_substitution_pass(self):
        addresses = {"Modem": "http://10.0.1.154"}
        values = {"level": "-30"}
        resolved = [substitute(t, values, addresses) for t in ["./m.sh", "{@Modem}", "{level}"]]
        self.assertEqual(resolved, ["./m.sh", "http://10.0.1.154", "-30"])

    def test_reference_to_a_device_without_an_address_is_rejected(self):
        scenario = self.scenario(["./m.sh", "{@Generator}", "status"])
        with self.assertRaisesRegex(ValueError, "no address in hardware_checks"):
            validate_scenario(scenario, self.CHECKS)

    def test_reference_to_an_unconfigured_device_is_rejected(self):
        scenario = self.scenario(["./m.sh", "{@Nowhere}", "status"])
        with self.assertRaisesRegex(ValueError, "references device Nowhere"):
            validate_scenario(scenario, self.CHECKS)

    def test_known_device_reference_is_accepted(self):
        validate_scenario(self.scenario(["./m.sh", "{@Modem}", "status"]), self.CHECKS)


class SafeStateValidationTests(unittest.TestCase):
    CHECKS = {"Modem": {"type": "command", "address": "http://10.0.1.154", "command": ["true"]}}

    def test_device_references_are_resolved_and_validated(self):
        steps = [{"name": "Disable", "command": ["./m.sh", "{@Modem}", "disable-transmit"]}]
        self.assertEqual(validate_safe_state(steps, self.CHECKS), steps)

    def test_scenario_arguments_are_rejected(self):
        # Safe state runs without a scenario, so it has no argument values.
        steps = [{"name": "Disable", "command": ["./m.sh", "{level}"]}]
        with self.assertRaisesRegex(ValueError, "without scenario arguments"):
            validate_safe_state(steps, self.CHECKS)

    def test_absent_safe_state_is_allowed(self):
        self.assertEqual(validate_safe_state(None, self.CHECKS), [])


class RunLifecycleTests(unittest.TestCase):
    """The safe state runs after every scenario and the run is archived."""

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.runs = self.root / "runs"
        self.runs.mkdir()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def runner(self, steps, safe_state, optional=False):
        catalog = self.root / "scenarios.json"
        catalog.write_text(json.dumps({
            "hardware_checks": {
                "Modem": {"type": "command", "address": "http://10.0.1.154", "command": ["true"]},
            },
            "safe_state": [
                {"name": "Disable transmitter", "command": safe_state, "optional": optional},
            ],
            "scenarios": [{
                "id": "demo", "name": "Demo", "equipment": ["Modem"],
                "arguments": [{"id": "level", "type": "number", "default": -20, "min": -60, "max": 10}],
                "steps": steps,
            }],
        }), encoding="utf-8")
        return Runner(catalog, self.runs)

    @staticmethod
    def finish(runner, timeout=30):
        deadline = time.monotonic() + timeout
        while runner.status()["state"] in ACTIVE_RUN_STATES:
            if time.monotonic() > deadline:
                raise AssertionError("run did not finish")
            time.sleep(0.02)
        return runner.status()

    def test_successful_run_reaches_the_safe_state_and_is_archived(self):
        runner = self.runner([{"name": "Transmit", "command": ["true"]}], ["true"])
        runner.start("demo", {"level": -33})
        status = self.finish(runner)

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["safe_state"], "ok")
        self.assertFalse(status["force_killed"])

        history = runner.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["arguments"], {"level": "-33"})
        self.assertEqual(history[0]["safe_state"], "ok")

    def test_archive_records_the_commands_as_they_were_executed(self):
        runner = self.runner(
            [{"name": "Configure", "command": ["echo", "{@Modem}", "{level}"]}], ["true"],
        )
        runner.start("demo", {"level": -12})
        self.finish(runner)

        record = runner.history()[0]
        self.assertEqual(
            record["steps"][0]["command"], ["echo", "http://10.0.1.154", "-12"],
        )
        entry = runner.history_entry(record["id"])
        self.assertTrue(any("http://10.0.1.154 -12" in line for line in entry["logs"]))

    def test_safe_state_runs_after_a_failed_step(self):
        runner = self.runner([{"name": "Boom", "command": ["false"]}], ["true"])
        runner.start("demo")
        status = self.finish(runner)

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["safe_state"], "ok")

    def test_unconfirmed_safe_state_fails_the_run(self):
        runner = self.runner([{"name": "Fine", "command": ["true"]}], ["false"])
        runner.start("demo")
        status = self.finish(runner)

        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["safe_state"], "failed")
        self.assertTrue(any("may still be transmitting" in line for line in status["logs"]))

    def test_optional_safe_state_failure_is_reported_as_degraded(self):
        runner = self.runner([{"name": "Fine", "command": ["true"]}], ["false"], optional=True)
        runner.start("demo")
        status = self.finish(runner)

        self.assertEqual(status["state"], "completed")
        self.assertEqual(status["safe_state"], "degraded")

    def test_a_command_that_ignores_sigterm_is_killed_and_the_carrier_cleared(self):
        # The hazard this guards: a killed command never runs its own hardware
        # cleanup, so only the safe state can confirm nothing is transmitting.
        stubborn = self.root / "stubborn.py"
        stubborn.write_text(
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('transmitting', flush=True)\n"
            "time.sleep(300)\n",
            encoding="utf-8",
        )
        runner = self.runner(
            [{"name": "Transmit", "command": [sys.executable, "-u", str(stubborn)]}], ["true"],
        )
        runner.start("demo")
        deadline = time.monotonic() + 10
        while not any("transmitting" in line for line in runner.status()["logs"]):
            if time.monotonic() > deadline:
                self.fail("the step never started")
            time.sleep(0.05)

        runner.stop()
        status = self.finish(runner)

        self.assertEqual(status["state"], "stopped")
        self.assertTrue(status["force_killed"])
        self.assertEqual(status["safe_state"], "ok")
        self.assertTrue(any("ignored SIGTERM" in line for line in status["logs"]))

    def test_a_run_interrupted_by_a_restart_is_reported_as_interrupted(self):
        runner = self.runner([{"name": "Transmit", "command": ["true"]}], ["true"])
        run = runner.start("demo")
        self.finish(runner)
        # Rewrite the archived state the way a crash mid-run would leave it.
        directory = next(self.runs.iterdir())
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        record["state"] = "running"
        (directory / "run.json").write_text(json.dumps(record), encoding="utf-8")

        self.assertEqual(runner.history()[0]["state"], "interrupted")
        self.assertEqual(runner.history_entry(run["id"])["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
