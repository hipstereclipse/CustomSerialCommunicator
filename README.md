# Custom Serial Communicator

Desktop application and Python library for connecting to vacuum instruments over serial links (RS-232 / RS-485), polling measurements, and visualizing data in real time.

## What This Project Does

- Connects to supported gauges and turbo controllers through a serial COM port.
- Uses protocol codecs to build request frames and parse responses.
- Loads instrument capabilities from YAML specs (model-specific commands, transport defaults, address ranges).
- Streams measurements to a PyQt-based GUI with live plots and terminal-style diagnostics.

## Current Device Coverage

- Gauge and controller specs: [device_specs/gauges](device_specs/gauges)
- Turbo specs: [device_specs/turbos](device_specs/turbos)
- Runtime protocol implementations: [src/serial_comm/protocols](src/serial_comm/protocols)
- Turbo worker/protocol layer: [src/serial_comm/turbos](src/serial_comm/turbos)

INFICON gauge/controller labels are represented in specs via `manufacturer`, `family`, and `protocol` fields. Turbo controllers such as TC600 remain under Pfeiffer protocol handling.

## Architecture Overview

- Registry and spec loading: [src/serial_comm/device_registry.py](src/serial_comm/device_registry.py)
- Worker/polling loop and frame read strategy: [src/serial_comm/acquisition.py](src/serial_comm/acquisition.py)
- Protocol contract: [src/serial_comm/protocols/base.py](src/serial_comm/protocols/base.py)
- GUI entrypoint: [main.py](main.py)

Flow:

1. A YAML model spec is selected.
2. `DeviceRegistry` resolves the spec and instantiates the protocol codec.
3. `GaugeWorker` opens serial transport and polls configured commands.
4. Parsed readings are emitted as Qt signals to GUI tabs.

## Quick Start

### 1) Create and activate virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Git Bash:

```bash
python -m venv .venv
source .venv/Scripts/activate
```

### 2) Install dependencies

```bash
pip install -e .
```

### 3) Run application

```bash
python main.py
```

## Test Suite

Run all tests:

```bash
python -m pytest -q
```

Run focused registry/protocol tests:

```bash
python -m pytest tests/test_device_registry.py -q
```

## Add New Instrument Drivers

See the full developer guide:

- [docs/DRIVER_DEVELOPMENT.md](docs/DRIVER_DEVELOPMENT.md)

That guide includes:

- Decision tree for adding a model with no code vs new protocol codec.
- End-to-end steps to add gauge, controller, or turbo models.
- YAML schema and protocol field reference.
- Validation checklist and common failure patterns.
- Example templates for ASCII and binary protocols.

## Practical Notes

- The worker never raises protocol parse exceptions to the GUI; failures are emitted as `DeviceError` and retries continue until fatal thresholds are reached.
- Some models are intentionally marked `experimental: true` while command coverage is still being validated against device firmware variants.
- If a model connects but echoes request frames back unchanged, verify the instrument address, RS mode, baud/parity, and protocol selection in the spec.

## Contributing

1. Create a branch from your working branch.
2. Add or update specs/protocol code.
3. Add tests for new behavior.
4. Run the test suite.
5. Open a pull request with serial log samples if available.
