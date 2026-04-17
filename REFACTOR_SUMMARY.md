# Refactor Summary — Serial Communicator v2.0

## Overview

Full modernisation of an INFICON vacuum gauge serial communication desktop app.

| | Before | After |
|---|---|---|
| GUI framework | Tkinter | PyQt6 |
| Architecture | 1 gauge, no plots | N gauges, live plots |
| Device specs | Hardcoded in classes | Declarative YAML |
| Turbo support | Mixed with gauges | Architecturally separate |
| Plotting | None (matplotlib installed, never used) | pyqtgraph, log-Y, 120 s history |
| Export | None | CSV / Parquet / HDF5, unit conversion |
| Session | None | `.scj` (config) / `.scd` (config + data) |
| Tests | 0 | 138 |
| Thread model | Main thread I/O (blocking UI) | All I/O in QThread workers |

---

## Phase 1 — Codebase Audit

Discovered four critical pre-existing bugs, all fixed before the refactor:

| Bug | File | Fix |
|---|---|---|
| `eval()` on serial data | `response_handler.py` | `ast.literal_eval()` |
| `PCGProtocol.parse_response` returned `dict` not `GaugeResponse` | `pcg_protocol.py` | Returned correct type |
| `CDGProtocol.__init__` attribute order crash | `cdg_protocol.py` | Corrected init order |
| TC1200/TC700 dropdown — undefined variable crash | `turbo_frame.py` | Fixed variable reference |

Dead files identified (not deleted; preserved as reference during transition):
`base_communication.py`, `pcg550_protocol.py`, `psg550_protocol.py`, `opg_protocol.py`, `tc600_commands.py`

---

## Phase 2 — Architecture Design

Key decisions (all locked in before implementation):

- **PyQt6** (not PySide6) — same API, better typing support
- **Pfeiffer turbos architecturally separate** — TC600 is a Pfeiffer product. All turbo code lives in `serial_comm/turbos/` and `GUI/turbo_workspace/`. Never mixed with INFICON gauge code.
- **Declarative YAML specs** — every device is a `.yaml` file in `device_specs/gauges/` or `device_specs/turbos/`. `DeviceRegistry` loads and validates them; no device logic is in Python subclasses.
- **pyqtgraph** for plotting — log-Y scale, 120 s rolling window, 2000-point circular buffer per trace
- **Export units** — default mbar, configurable per export via `ExportDialog`, persisted via `QSettings`
- **Session format** — `.scj` = JSON config + styling; `.scd` = JSON config + full time-series data

---

## Phase 3 — Implementation

### New library (`src/serial_comm/`)

| Module | Purpose |
|---|---|
| `models.py` | `DeviceReading`, `DeviceError`, `GaugeReading`, `DeviceSpec`, `CommandSpec` |
| `transport.py` | `SerialTransport` — RS-232/RS-485 with RTS direction control |
| `device_registry.py` | YAML loader, `DeviceSpec` factory, `GaugeProtocol` factory |
| `acquisition.py` | `GaugeWorker(QThread)` — poll and continuous-output loops |
| `session.py` | `save_session()` / `load_session()` — `.scj` / `.scd` JSON round-trip |
| `protocols/ppg_ascii.py` | INFICON PPG ASCII (PPG550, PPG570) |
| `protocols/pfeiffer_ascii.py` | Pfeiffer ASCII (BCG450, TC600 shared base) |
| `protocols/pfeiffer_binary.py` | Pfeiffer binary CRC-16 (PCG/PSG/MAG/MPG/BPG/BCG) |
| `protocols/cdg_serial.py` | INFICON CDG serial (CDG025D, CDG045D) |
| `turbos/tc600_protocol.py` | Pfeiffer TC600 protocol (extends PfeifferAscii) |
| `turbos/turbo_worker.py` | `TurboWorker(QThread)` — status polling + command dispatch |

### Device specs (`device_specs/`)

17 YAML files covering every supported device:
PPG550, PPG570, PCG550, PSG550, BCG450, BCG552, BPG40x, BPG552, MAG500, MPG500, CDG025D, CDG045D, plus experimental: OPG550 and gap-list variants. TC600 turbo spec separate in `device_specs/turbos/`.

### New GUI (`GUI/`)

| File | Purpose |
|---|---|
| `main_app.py` | `QApplication` entry point (replaces Tkinter) |
| `main_window.py` | `QMainWindow` — menu, toolbar, device list, tab panel |
| `gauge_workspace/gauge_tab.py` | Per-gauge: pyqtgraph plot + readings table |
| `gauge_workspace/add_gauge_dialog.py` | Model / port / address / commands selector |
| `gauge_workspace/export_dialog.py` | CSV / Parquet / HDF5 export with unit conversion |
| `turbo_workspace/turbo_window.py` | Standalone TC600 dashboard (speed bar, controls) |

### Test suite (`tests/`)

138 tests, 0 failures:

| File | Count | Covers |
|---|---|---|
| `test_ppg_ascii.py` | 20 | PPG ASCII build/parse |
| `test_pfeiffer_ascii.py` | 25 | Pfeiffer ASCII checksum, build, parse |
| `test_pfeiffer_binary.py` | 20 | CRC-16, binary frame build/parse |
| `test_cdg_serial.py` | 21 | CDG build/parse, gauge type detection |
| `test_device_registry.py` | 20 | YAML loading, spec validation, protocol factory |
| `test_acquisition.py` | 12 | `GaugeWorker` lifecycle, error escalation, stop |
| `test_turbo_worker.py` | 7 | `TurboWorker` lifecycle, status emission, error handling |
| `test_session.py` | 13 | `.scj`/`.scd` round-trips, error handling |

---

## Phase 4 — Adversarial Audit

The audit subagent reviewed all new code for bugs, thread hazards, packaging errors, and security issues.

### Findings summary

| Severity | Count | Fixed |
|---|---|---|
| Critical | 2 | 2 |
| High | 4 | 3 |
| Medium | 5 | 2 |
| Low / informational | 6 | 1 |

---

## Phase 5 — Remediation

All critical and selected high/medium findings fixed:

### CRITICAL fixes

**`pyproject.toml` — wrong entry point and package path**
Both `[project.scripts]` and `[tool.hatch.build.targets.wheel]` referenced `gui` (lowercase).
On a case-sensitive filesystem the install would fail to find the `GUI` package.
Fixed: `gui.main_app:main` → `GUI.main_app:main`; `packages = ["src/serial_comm", "GUI"]`.

**`turbo_worker.py` — `_pending_command` data race**
`send_command()` wrote `_pending_command` from the GUI thread while `_run_loop()` read it
from the worker thread with no synchronisation. Fix: added `threading.Lock` (`_cmd_lock`)
around both the write (`send_command`) and the read+clear (`_run_loop`).

### HIGH fixes

**`main_window.py` — `closeEvent` ignored `wait()` return value**
A 2-second wait with no check could silently abandon a still-running thread.
Fixed: raised timeout to 3 s, log a warning if the worker doesn't stop in time.

**`export_dialog.py` — unsafe HDF5 group name for device IDs**
`dev_id.replace("/", "_")` left Windows backslashes and other HDF5-unsafe chars intact.
Fixed: `re.sub(r"[^a-zA-Z0-9_-]", "_", dev_id)`.

**`add_gauge_dialog.py` — silent `except Exception: pass` on spec load**
Spec-load failures were swallowed silently. Fixed: `logger.exception(...)` now logs
the full traceback before clearing `_spec`.

### MEDIUM fixes

**`cdg_serial.py` — manual two's-complement for signed 16-bit**
Replaced manual `if meas >= 0x8000: meas -= 0x10000` with
`int.from_bytes(raw[4:6], byteorder="big", signed=True)`.

**`turbo_window.py` — naive-datetime in status bar**
`datetime.now()` returned a naive datetime. Fixed: `datetime.now(tz=timezone.utc)`.

---

## Known limitations / future work

- **Session restore opens ports immediately** — no "connect later" option. A future version could show a confirmation with individual per-gauge toggles.
- **No conftest.py / pytest fixtures** — each test file sets up its own mocks. A shared `conftest.py` would reduce duplication.
- **Old `serial_communication/` package** — the original Tkinter code is still present in the repo but is entirely dead. Delete it once the v2 GUI is validated on hardware.
- **PPG550 live hardware test** — test fixtures for live hardware communication were not captured because the gauge was unpowered during development. Re-run `tests/fixtures/` capture when the gauge is connected.
- **`pydantic` listed as dependency but unused** — was planned for YAML validation; remove or use it.
