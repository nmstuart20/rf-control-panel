import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.server import Runner, load_catalog, validate_scenario


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
            side_effect=lambda name, check: {"name": name, "state": "connected"},
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


if __name__ == "__main__":
    unittest.main()
