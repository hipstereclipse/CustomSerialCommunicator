"""
Live verification script: iterate every OPG550 READ command on COM11,
send via InficonP3V02Protocol + SerialTransport, decode via the real codec,
print results. Does NOT issue any write commands.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import serial

from serial_comm.device_registry import DeviceRegistry
from serial_comm.protocols.inficon_p3_v02 import parse_frame


PORT = "COM11"
BAUD = 115200


def read_frame(ser: serial.Serial) -> bytes:
    header = ser.read(5)
    if len(header) < 5:
        return b""
    apdu_len = (header[3] << 8) | header[4]
    if apdu_len < 5 or apdu_len > 1024:
        return b""
    rest = ser.read(apdu_len + 2)
    if len(rest) < apdu_len + 2:
        return header + rest  # truncated; return anyway for diagnostics
    return bytes(header) + bytes(rest)


def main() -> int:
    registry = DeviceRegistry()
    spec = registry.get_spec("OPG550")
    protocol = registry.make_protocol(spec, address=0)

    read_cmds = [c for c, info in spec.commands.items() if info.read]
    print(f"Found {len(read_cmds)} readable commands on OPG550")
    print(f"Opening {PORT} at {BAUD} baud...")

    ok = 0
    fail = 0
    with serial.Serial(PORT, BAUD, bytesize=8, parity="N", stopbits=1,
                       timeout=0.8, write_timeout=1.0) as ser:
        for cmd in read_cmds:
            try:
                req = protocol.build_request(cmd)
            except Exception as exc:
                print(f"  [BUILD-FAIL] {cmd}: {exc}")
                fail += 1
                continue

            ser.reset_input_buffer()
            ser.write(req)
            raw = read_frame(ser)

            if not raw:
                print(f"  [NO-RESP ] {cmd}")
                fail += 1
                continue

            result = protocol.parse_response(raw, cmd)
            try:
                parsed = parse_frame(raw)
                pid_rx = parsed["pid"]
                crc_ok = parsed["crc_ok"]
            except Exception:
                pid_rx, crc_ok = -1, False

            if result.success:
                print(f"  [OK      ] {cmd:28s} pid={pid_rx:<5d} -> {result.formatted}")
                ok += 1
            else:
                print(f"  [ERR     ] {cmd:28s} pid={pid_rx:<5d} crc_ok={crc_ok} -> {result.error}")
                fail += 1

            time.sleep(0.03)

    print(f"\nResult: {ok}/{ok+fail} commands succeeded")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
