# RF Control Panel

A small web control panel for running RF scenarios with control of the following:
* SignalHound VGS60A
* Quintech Switches
* Modems
* Ettus Radios

## Start


```bash
chmod +x modem_control.sh
```

```bash
python3 server.py
```

## Configure scenarios

Edit `scenarios.json` to define a scenario and what commands and equipment are needed to run it.

Hardware listed in a scenario appears in the connection status section. Configure its
probe once in the top-level `hardware_checks` object. Supported probes are:

```json
"hardware_checks": {
  "Signal Hound VSG60A": {"type": "signalhound"},
  "Ettus X310": {"type": "command", "command": ["uhd_find_devices"]},
  "Modem": {"type": "tcp", "host": "192.0.2.10", "port": 23},
  "Quintech switch": {"type": "tcp", "host": "192.0.2.20", "port": 9100}
}
```

The equipment name in `hardware_checks` must exactly match the name in a scenario.
An unconfigured device is reported as such rather than assumed to be connected.

Scenarios can expose numeric arguments in the panel. Put a full placeholder in a
command value to substitute the validated input:

```json
"arguments": [
  {"id": "center", "label": "Center frequency", "type": "number", "default": 1000000000, "min": 50000000, "max": 6000000000, "step": 1000000, "unit": "Hz"}
],
"steps": [
  {"name": "Transmit", "command": ["python3", "sweep.py", "--center", "{center}"]}
]
```

```json
{
  "id": "lab-test",
  "name": "Lab test",
  "description": "Configure the path, modem, and radio, then transmit.",
  "equipment": ["Quintech", "modem", "X310", "VSG60A"],
  "steps": [
    {"name": "Select RF path", "command": ["./scripts/quintech.sh", "path-a"]},
    {"name": "Configure modem", "command": ["./scripts/modem.sh", "test-profile"]},
    {"name": "Configure X310", "command": ["python3", "-u", "scripts/x310_setup.py", "--profile", "test"]},
    {"name": "Transmit", "command": ["python3", "-u", "sweep.py", "--mode", "chirp", "--duration", "10"]}
  ]
}
```

Currently need to ensure the Signal Hound library can access the USB device (typically through the vendor's udev rules), and run the panel from this directory so the provided library path resolves correctly.

Make sure `libvsg_api.so.1.2.1` and `vsg_api.py` are in the PATH if using SignalHound.the vsgdevice folder with `the vsgdevice folder with `