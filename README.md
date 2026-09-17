# RF Control Panel

A small web control panel for running RF scenarios with control of the following:
* SignalHound VGS60A
* Quintech RF Switches
* Modems
* Ettus X310 Radio
* Ettus X410 Radio

## Start

```bash
chmod +x modem_control.sh
```

```bash
python3 src/server.py
```

Run the tests with:

```bash
python3 -m unittest tests.test_server
```

To password-protect the RF switch config tab, set `RF_SWITCH_PASSWORD` before
starting the server. The password is checked by the server and is never sent to
the browser as configuration:

```bash
RF_SWITCH_PASSWORD='use-a-strong-password' python3 src/server.py
```

## Configure scenarios

Use **Scenarios → Add Scenario** in the control panel to build and save a scenario. The editor supports:

* one or more devices from `hardware_checks`
* numeric or integer arguments with defaults, ranges, step sizes, and units
* ordered command arguments, background commands, and environment variables
* ordered cleanup commands that run after a stop or failure

Created scenarios are validated and saved to `scenarios/scenarios.json`; they are immediately available in the scenario picker. Command items are passed directly to the process, so enter the executable as the first item and each command-line argument as a separate item. Use a full `{argument_id}` item to substitute a configured scenario argument.

You can also edit `scenarios/scenarios.json` directly to define a scenario and what commands and equipment are needed to run it.

Hardware listed in a scenario appears in the connection status section. Configure its
probe once in the top-level `hardware_checks` object. Current supported probes are:

```json
"hardware_checks": {
  "Signal Hound VSG60A": {"type": "signalhound"},
  "Modem": {"type": "command", "address": "http://192.0.2.10", "command": ["./modem_control.sh", "{@Modem}", "status"]},
  "Quintech switch": {"type": "tcp", "address": "192.0.2.20", "port": 9100}
}
```

A `signalhound` check opens and closes the generator, a `command` check runs a
command and treats a non-zero exit as disconnected, and a `tcp` check opens a
socket.

The equipment name in `hardware_checks` must exactly match the name in a scenario.

## Device addresses

Give each device an `address` in `hardware_checks` and refer to it from any
command with `{@Device name}`.

```json
"hardware_checks": {
  "Comtech modem 1": {
    "type": "command",
    "address": "http://10.0.1.154",
    "command": ["./modem_control.sh", "{@Comtech modem 1}", "status"]
  }
},
"scenarios": [
  {
    "steps": [
      {"name": "Enable transmitter", "command": ["./modem_control.sh", "{@Comtech modem 1}", "enable-transmit", "{level}"]}
    ]
  }
]
```

Scenarios can expose numeric arguments in the panel. Put a full placeholder in a
command value to substitute the validated input:

```json
"arguments": [
  {"id": "center", "label": "Center frequency", "type": "number", "default": 1000000000, "min": 50000000, "max": 6000000000, "step": 1000000, "unit": "Hz"}
],
"steps": [
  {"name": "Transmit", "command": ["python3", "-m", "src.signalhound.sweep", "--center", "{center}"]}
]
```

Steps normally run one at a time. Set `"background": true` on a long-running
step when the following step must execute while it is still active. Background
steps continue streaming logs and must succeed before the scenario completes:

```json
"steps": [
  {"name": "Transmit", "background": true, "command": ["./scripts/transmit.sh"]},
  {"name": "Verify RF levels", "command": ["./scripts/verify-rf.sh"]}
]
```

To run cleanup commands when the user clicks **Stop scenario** or when a regular
step fails, add optional `stop_steps`. They run in order after active foreground
and background processes have been terminated. Stop steps may use scenario
argument placeholders and `environment` in the same way as regular steps:

```json
"stop_steps": [
  {"name": "Disable transmitter", "command": ["./scripts/modem.sh", "disable", "{level}"]}
]
```

## Run history

Every run is archived under `runs/<date>-<time>-<run id>/`:


When using Signal Hound, place `vsg_api.py` and `libvsg_api.so.1.2.1` in the project-root `vsgdevice/` directory.


## Things to add 

- Multiple scenarios open at as tabs
- More modem config control (voltage on RF out)
- Record measured RF levels as structured data in the run archive, rather than leaving them only in the log text
