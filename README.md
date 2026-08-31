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
