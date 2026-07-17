# Custom Serial Communicator

Custom Serial Communicator is a desktop application and Python runtime for interacting with vacuum gauges and turbo controllers over serial transport. It is designed for lab and production environments where users need consistent device configuration, live monitoring, protocol-level visibility, and repeatable data capture.

The project combines:

- A PyQt GUI for day-to-day operation.
- A registry-driven driver layer (YAML specs + protocol codecs).
- Real and simulated acquisition workers for testing and training.
- Session persistence and data export tooling.

## Core Capabilities

- Connect to serial devices over RS-232 and RS-485.
- Auto-scan COM ports and identify responsive instruments.
- Select one or many scanned gauges and add them in a single action.
- Automatically map scanned gauges to the right model configuration.
- Poll one or multiple commands per device on a configurable interval.
- Display data in per-device tabs and combined multi-gauge dashboards.
- Open a dedicated turbo controller workspace for TC600-family workflows.
- Run realistic simulation scenarios with configurable leak/humidity/recipes.
- Save and restore sessions, including real and simulated devices.
- Export captured readings for external analysis.

## High-Level Architecture

### Application Entry

- GUI startup: [main.py](main.py)
- Main window orchestration: [GUI/main_window.py](GUI/main_window.py)
- App bootstrap and Qt wiring: [GUI/main_app.py](GUI/main_app.py)

### Device Description and Driver Resolution

- Gauge specs: [device_specs/gauges](device_specs/gauges)
- Turbo specs: [device_specs/turbos](device_specs/turbos)
- Spec loader and protocol factory: [src/serial_comm/device_registry.py](src/serial_comm/device_registry.py)

Specs define transport defaults, command metadata, protocol family, address constraints, and whether support is experimental.

### Runtime Protocol Layer

- Protocol interface contract: [src/serial_comm/protocols/base.py](src/serial_comm/protocols/base.py)
- PPG ASCII protocol: [src/serial_comm/protocols/ppg_ascii.py](src/serial_comm/protocols/ppg_ascii.py)
- Pfeiffer ASCII protocol: [src/serial_comm/protocols/pfeiffer_ascii.py](src/serial_comm/protocols/pfeiffer_ascii.py)
- Pfeiffer binary protocol: [src/serial_comm/protocols/pfeiffer_binary.py](src/serial_comm/protocols/pfeiffer_binary.py)
- CDG serial protocol: [src/serial_comm/protocols/cdg_serial.py](src/serial_comm/protocols/cdg_serial.py)

### Acquisition and Transport

- Worker lifecycle and polling loop: [src/serial_comm/acquisition.py](src/serial_comm/acquisition.py)
- Serial transport configuration: [src/serial_comm/transport.py](src/serial_comm/transport.py)
- Shared runtime models: [src/serial_comm/models.py](src/serial_comm/models.py)

### Simulation Subsystem

- Simulation engine: [src/serial_comm/simulation_engine.py](src/serial_comm/simulation_engine.py)
- Simulation worker: [src/serial_comm/simulated_worker.py](src/serial_comm/simulated_worker.py)
- Simulation data models: [src/serial_comm/simulation_models.py](src/serial_comm/simulation_models.py)
- OPG optical-spectrum helpers: [src/serial_comm/opg_spectrum.py](src/serial_comm/opg_spectrum.py)
- Scenario catalog: [src/serial_comm/simulation_scenarios.py](src/serial_comm/simulation_scenarios.py)

## How the Program Works

At runtime, the application follows this path:

1. You add a gauge manually or via COM scan.
2. The selected model resolves to a YAML spec in the registry.
3. The registry creates the matching protocol codec.
4. A worker opens the serial transport using spec/override settings.
5. The worker polls configured commands and parses responses.
6. Parsed `GaugeReading` values are emitted to GUI tabs and combined plots.
7. Errors are reported as `DeviceError` events without crashing UI threads.

This separation keeps model-specific behavior in specs/codecs, while GUI code remains focused on visualization and operator workflow.

## GUI Workspaces and Workflow

### Main Workspace

The main workspace is managed by [GUI/main_window.py](GUI/main_window.py) and includes:

- Device list panel.
- Add gauge / simulate / turbo actions.
- Per-gauge tabs with values and terminal output.
- Combined real-gauge plot tab.

### Add Gauge Dialog

The add dialog in [GUI/gauge_workspace/add_gauge_dialog.py](GUI/gauge_workspace/add_gauge_dialog.py) supports:

- Model selection (stable + experimental).
- COM port selection with refresh.
- Background COM scan using [GUI/gauge_workspace/port_scanner.py](GUI/gauge_workspace/port_scanner.py).
- Multi-select scan results so multiple gauges can be connected at once.
- Automatic model/port alignment when selecting scanned items.
- Advanced transport settings (baud, RS mode, address).
- Command selection for polling and interval tuning.

### Scan Intelligence

The scanner actively probes protocols and emits structured metadata (not just a display string). This enables smarter model/config resolution:

- CDG family devices use explicit type/range probing and frame validation.
- CDG model mapping avoids unsafe assumptions from sensor code alone.
- Full-scale hints are propagated to configuration when confidence is sufficient.
- PPG550 and PPG570 are offered as a combined selection to avoid false split-identification while preserving compatibility.
- TC600 detections are routed to the dedicated turbo workflow.

## Device Families and Protocol Notes

### PPG Series

- Supports ASCII command/response workflows with ACK/NAK handling.
- Handles both `@ACK...` and address-prefixed ACK variants.
- Includes pressure mnemonic fallback behavior for firmware variants.
- Supports `query_param` and `write_prefix` command metadata for indexed writes (for example per-setpoint writes on PPG570).

### CDG/HPG Serial Family

- Uses fixed-length binary frames with checksums.
- Pressure values are interpreted via full-scale calibration metadata.
- Distinguishes model hinting from range/type encoding to reduce misidentification.

### Pfeiffer ASCII/Binary Families

- Uses frame validation and checksum parsing per protocol family.
- Supports model-specific parameter tables from YAML specs.

### Turbo Controllers

- TC600 workflows are separated under turbo workspace components in [GUI/turbo_workspace](GUI/turbo_workspace) and runtime support in [src/serial_comm/turbos](src/serial_comm/turbos).

## Simulation Functionality

Simulation is first-class and designed for realistic operator practice and UI testing:

- Add simulated gauges from [GUI/gauge_workspace/add_simulated_gauge_dialog.py](GUI/gauge_workspace/add_simulated_gauge_dialog.py).
- Control and monitor simulated systems in [GUI/gauge_workspace/simulation_tab.py](GUI/gauge_workspace/simulation_tab.py).
- Run recipe-driven and scenario-driven pressure dynamics.
- Use humidity, gas type, leak-rate, and base-pressure inputs.
- Simulate OPG optical spectra and identify likely species from synthetic wavelength signatures.
- Plot simulated gauges in a dedicated combined simulation view.

## Data Views and Plotting

- Per-gauge live views: [GUI/gauge_workspace/gauge_tab.py](GUI/gauge_workspace/gauge_tab.py)
- Combined plotting controls: [GUI/gauge_workspace/combined_tab.py](GUI/gauge_workspace/combined_tab.py)
- In-app terminal traffic viewer: [GUI/gauge_workspace/terminal_widget.py](GUI/gauge_workspace/terminal_widget.py)

Combined plotting supports overlay/stacked/grid layouts, visibility toggles, per-device color assignment, and synchronized chart navigation.
Combined plots keep the full collected session history for each pressure series, so gauges with different polling rates still share comparable timestamp axes instead of losing older data by sample count.

Spectrum Studio (OPG550 advanced workspace) also includes a shared bottom hover-information bar with stable positioning:

- The cursor X value is always rendered first at the far left of the info bar, so time/wavelength stays in a fixed location while hovering.
- In Advanced Analysis mode, the hover bar shows the instantaneous pressure delta and signed percent delta between comparison sources at the same cursor time: $\Delta(A-B)$ and $\Delta\%$.
- OPG550 Advanced Analysis keeps the full collected peer-pressure history for correlation plots, preserving meaningful comparison windows when gauges poll at different speeds.
- OPG550 auto-plasma ignition thresholds display in the active pressure unit while continuing to store and evaluate the safety limits internally in mbar.

## Session and Export

- Session save/load model: [src/serial_comm/session.py](src/serial_comm/session.py)
- Export UI: [GUI/gauge_workspace/export_dialog.py](GUI/gauge_workspace/export_dialog.py)

Sessions preserve model/port/protocol settings and can restore multiple gauges and simulated devices in one operation.

## Installation and Quick Start

### Option A: Install script (Windows)

[scripts/install.ps1](scripts/install.ps1) sets up everything needed to run and develop the app in one step. It uses `uv` (matching the committed [uv.lock](uv.lock)) if installed, otherwise falls back to a standard `python -m venv` + `pip` workflow. It installs runtime dependencies plus the `[dev]` extras (pytest, ruff, mypy, pytest-qt) by default.

```powershell
.\scripts\install.ps1
```

Options:

```powershell
.\scripts\install.ps1 -SkipDev      # runtime dependencies only, skip test/lint tools
.\scripts\install.ps1 -PreferPip    # use pip + venv even if uv is installed
```

### Option B: Manual setup

1) Create and activate a virtual environment

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

2) Install dependencies

```bash
pip install -e .
```

### Launch the app

```bash
python main.py
```

## Testing

Run full tests:

```bash
python -m pytest -q
```

Run focused protocol and registry tests:

```bash
python -m pytest tests/test_cdg_serial.py tests/test_ppg_ascii.py tests/test_opg_spectrum.py tests/test_device_registry.py -q
```

## Driver Development

For adding or extending drivers, use:

- [docs/DRIVER_DEVELOPMENT.md](docs/DRIVER_DEVELOPMENT.md)

That guide covers:

- When to add YAML only vs new codec code.
- Spec schema fields and command metadata.
- Protocol-specific implementation patterns.
- Validation strategy and troubleshooting checklists.

## Troubleshooting Guide

### Device is found but values are wrong

- Verify selected model and protocol family.
- For CDG/HPG class devices, verify configured full-scale exactly matches installed head calibration.
- Confirm units and command selection in the add dialog.

### Device echoes requests but does not respond

- Check RS mode and address assumptions.
- Validate baud/parity/data bits/stop bits.
- Verify physical wiring and half-duplex bus behavior for RS-485.

### Scan identifies model family but not exact variant

- This can happen with protocol-compatible or range-encoded families.
- Use scan result metadata and manually select exact model if needed.
- Keep specs updated as firmware-specific distinctions are confirmed.

### Intermittent parse errors

- Review terminal traffic for framing/terminator mismatches.
- Increase timeout cautiously for slower devices.
- Validate that command mnemonics align with device firmware revision.

## Practical Engineering Notes

- Worker threads isolate transport/protocol failures from UI threads.
- Experimental models remain available but visually marked for caution.
- Protocol parsing is defensive and returns structured error readings rather than hard-failing acquisition loops.
- The design intentionally favors robust long-running sessions over strict fail-fast behavior.

## Contributing

1. Create a branch from your current working branch.
2. Implement feature or fix with tests.
3. Run target and full test suites.
4. Include protocol logs or reproduction notes in PRs when behavior is device/firmware-dependent.
