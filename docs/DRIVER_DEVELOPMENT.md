# Driver Development Guide

This document explains how to add support for additional serial instruments (gauges, gauge controllers, and turbo controllers) in this project.

## 1. Driver Model In This Codebase

A "driver" is split into two layers:

1. Model specification (YAML): command set, transport defaults, addressing, metadata.
2. Protocol codec (Python): frame encoding/decoding logic.

Most new instruments can be added by YAML only if they match an existing protocol codec.

Key extension points:

- Protocol base contract: [src/serial_comm/protocols/base.py](../src/serial_comm/protocols/base.py)
- Registry protocol routing: [src/serial_comm/device_registry.py](../src/serial_comm/device_registry.py)
- Poll/read framing logic: [src/serial_comm/acquisition.py](../src/serial_comm/acquisition.py)

## 2. Decide If You Need New Python Code

Use this decision path:

1. Does instrument wire framing exactly match existing `ppg_ascii`, `inficon_ascii`, `inficon_binary`, or `cdg_serial` handling?
- Yes: add YAML spec only.
- No: implement a new protocol codec.

2. Does response framing differ (terminator, fixed length, binary length byte, CRC)?
- Yes: you likely need codec updates and possibly acquisition framing updates.

3. Is the instrument continuous-output instead of request-response?
- Override `supports_continuous_output()` and `parse_continuous()` in codec.

## 3. Add A New Instrument Via YAML (No New Codec)

### 3.1 Create spec file

- Gauges/controllers: [device_specs/gauges](../device_specs/gauges)
- Turbos: [device_specs/turbos](../device_specs/turbos)

Filename convention: lowercase model, for example `abc123.yaml`.

### 3.2 Required fields

```yaml
model: ABC123
manufacturer: INFICON
family: inficon_ascii
protocol: inficon_ascii
transport:
  default_baud: 9600
  parity: N
  data_bits: 8
  stop_bits: 1
  rs_modes: [RS232, RS485]
  default_address: 1
  rs485_address_range: [1, 253]
commands:
  pressure:
    pid: 340
    read: true
    write: false
    data_type: u_expo_new
    unit: mbar
    description: Primary pressure channel
```

### 3.3 Command fields reference

- `read`, `write`: command capabilities.
- `pid`: numeric parameter ID for binary/parameterized ASCII protocols.
- `mnemonic`: command mnemonic for PPG-style ASCII protocols.
- `data_type`: parse strategy (examples: `u_expo_new`, `u_integer`, `string`).
- `unit`, `description`: UI and diagnostics metadata.
- `experimental`: optional, for incomplete/early support.

## 4. Add A New Protocol Codec (When Needed)

### 4.1 Create new codec module

Add file under [src/serial_comm/protocols](../src/serial_comm/protocols), subclassing `GaugeProtocol`.

Minimal template:

```python
from typing import Any
from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

class MyProtocol(GaugeProtocol):
    TERMINATOR = b"\r"

    def build_request(self, command: str, value: Any = None) -> bytes:
        # map command -> frame
        return b"..."

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if not raw:
            return self._err("No response", raw)
        # decode and return _ok(...)
        return self._ok(value=1.23, unit="mbar", formatted="1.230E+00 mbar", raw=raw)
```

### 4.2 Register codec in DeviceRegistry

Update [src/serial_comm/device_registry.py](../src/serial_comm/device_registry.py):

- Add protocol string branch in `make_protocol()`.
- Import your codec and return an instance.
- Preserve address behavior (`address` override or `spec.default_address`).

### 4.3 Update read framing if required

If your protocol uses different response framing, update [src/serial_comm/acquisition.py](../src/serial_comm/acquisition.py):

- `_read_response()` for normal polling.
- `_read_terminal_response()` for terminal/raw command mode.
- Add branch based on `spec.protocol` or codec class.

## 5. Add New Turbo Driver

Turbo support follows the same pattern but with turbo-oriented command sets and workers.

Relevant modules:

- [src/serial_comm/turbos/turbo_worker.py](../src/serial_comm/turbos/turbo_worker.py)
- [src/serial_comm/turbos/tc600_protocol.py](../src/serial_comm/turbos/tc600_protocol.py)
- [device_specs/turbos](../device_specs/turbos)

Approach:

1. If turbo wire protocol is identical to existing implementation, add a new turbo YAML spec.
2. If not, create new turbo protocol class and route it in the turbo layer similarly to gauge registry routing.
3. Include safety-critical commands as write-protected unless explicit UI confirmation is present.

## 6. Validation Checklist

Before merging a new driver:

1. Spec loads in registry without errors.
2. Model appears in add-device dialog model list.
3. Basic read command returns parsed value.
4. Invalid/short response returns recoverable `DeviceError`, not crash.
5. Disconnect/reconnect works repeatedly.
6. At least one test added for registry loading or protocol parsing.

## 7. Recommended Tests

Target files:

- [tests/test_device_registry.py](../tests/test_device_registry.py)
- Existing protocol tests under [tests](../tests)

Typical test additions:

1. Registry loads model and metadata (`manufacturer`, `family`, `protocol`).
2. `make_protocol()` returns expected codec type.
3. Parser handles a valid frame.
4. Parser handles malformed checksum/terminator.

## 8. Common Failure Modes

### 8.1 Request echoed as response

Symptom: received bytes equal sent request.

Likely causes:

- Wrong address.
- Wrong serial mode (RS-232 vs RS-485).
- Wrong baud/parity.
- Wrong protocol selected for model.

### 8.2 Continuous parse errors

Likely causes:

- Incorrect frame length assumptions.
- Wrong terminator.
- Wrong checksum/CRC implementation.
- Command `data_type` mismatch in YAML.

### 8.3 Thread warning on crash path

Symptom: worker thread destroyed while running.

Mitigation:

- Ensure UI catches setup exceptions before worker startup.
- Use graceful stop and wait in disconnect path.

### 8.4 CDG full-scale mismatch (Torr vs mbar)

Symptom: CDG readings are scaled incorrectly by about 1.333x or 0.75x.

Likely cause:

- `full_scale_mbar` does not match the installed CDG head calibration.
- Assuming `10 Torr` and `10 mbar` are equivalent (they are not).

Mitigation:

- Set YAML `full_scale_mbar` to the exact factory full-scale of the installed head.
- Keep option lists explicit about units when presenting choices in UI.

## 9. Versioning New Drivers Safely

Use staged rollout:

1. Start with `experimental: true`.
2. Validate against multiple physical units/firmware revisions.
3. Promote to stable once field-tested.

## 10. Driver Submission Template

When contributing a new driver, include:

1. Manufacturer, model, and manual revision.
2. Serial settings (baud/parity/data/stop, RS mode).
3. Frame examples for at least one read and one write command.
4. Added YAML file path.
5. Added/updated Python files.
6. Test evidence and sample serial logs.
