"""
SimulatedGaugeWorker — a QThread that emits fake :class:`DeviceReading` signals
by sampling the shared :class:`SimulationEngine`.

The worker mirrors :class:`serial_comm.acquisition.GaugeWorker` 1:1 on its
public interface — same signals, same ``start()`` / ``stop()`` lifecycle, and
same ``send_terminal_command()`` / ``set_polling_enabled()`` entry points —
so code that was written against the real gauge worker (the GaugeTab, session
save/load, the terminal widget, etc.) can treat a simulated gauge exactly as
a real one.

No real serial port is ever opened.  The worker loop reads the current
"real" pressure from the engine, applies a family-specific response model
(gas correction for Pirani, saturation/underrange clamping for cold cathode,
etc.), and emits the resulting :class:`DeviceReading`.
"""

from __future__ import annotations

import logging
import math
import queue as _queue
import random
import struct
import time
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec, TerminalEntry
from serial_comm.protocols.base import GaugeProtocol
from serial_comm.simulation_engine import SimulationEngine, get_engine
from serial_comm.simulation_models import (
    CDG_FULL_SCALE_OPTIONS_MBAR,
    CDGFullScaleOption,
    GaugeFamily,
    GasType,
    PIRANI_GAS_FACTOR,
    SimulatedGaugeConfig,
    classify_family,
    cdg_full_scale_options,
    get_simulation_spec,
)

logger = logging.getLogger(__name__)


# Cold-cathode "valid" range, in mbar.  Outside this band we emit a
# saturation / underrange sentinel value (mirrors real cold-cathode gauge
# behaviour where the controller clamps the reading and flags an alarm).
_CC_LOWER_MBAR: float = 1e-10
_CC_UPPER_MBAR: float = 1e-2
#: Sentinel returned when a cold-cathode gauge is driven out of range.
_CC_OVERRANGE: float = 9.9e9
_CC_UNDERRANGE: float = 0.0

# Noise model tuning — chosen to match spec bullet-point requirements:
# CDG ±0.05 %, CC ±5 % log-space, Pirani ±1 % mid-range.
_CC_LOG_NOISE_SIGMA: float = 0.05

# Units considered pressure measurements (mirrors command_utils.PRESSURE_UNITS).
_PRESSURE_UNITS: frozenset[str] = frozenset({"mbar", "Torr", "torr", "Pa", "hPa", "psi"})


class SimulatedGaugeWorker(QThread):
    """Virtual :class:`QThread` worker feeding a simulated gauge tab.

    Parameters
    ----------
    spec:
        The resolved :class:`DeviceSpec` for this model.  Used only to pick
        the display unit and to craft plausible fake terminal responses —
        never used to open a real port.
    config:
        Per-gauge simulation configuration (ID, model, display name, etc.).
    engine:
        Override the shared :class:`SimulationEngine` singleton.  Production
        callers pass ``None`` (the default) which resolves to
        :func:`serial_comm.simulation_engine.get_engine`.  Tests can inject a
        mock here.
    """

    reading_ready = pyqtSignal(object)     # DeviceReading
    error_occurred = pyqtSignal(object)    # DeviceError
    terminal_response = pyqtSignal(object)  # TerminalEntry
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(
        self,
        spec: DeviceSpec,
        config: SimulatedGaugeConfig,
        engine: SimulationEngine | None = None,
        protocol: GaugeProtocol | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._config = config
        self._engine = engine if engine is not None else get_engine()
        self._family = classify_family(config.model)
        self._sim_spec = get_simulation_spec(config.model)
        self._device_id = config.sim_id
        self._poll_interval = max(float(config.poll_interval_s), 0.05)
        self._terminal_queue: _queue.SimpleQueue = _queue.SimpleQueue()
        self._polling_enabled = True
        self._protocol = protocol
        self._rng = random.Random(hash(config.sim_id) & 0xFFFFFFFF)
        self._last_emit_mono: float | None = None
        self._last_modelled: float | None = None
        # Auto-poll mock terminal: emit one exchange every this many polls.
        # Keeps the terminal readable without flooding it at high poll rates.
        self._mock_terminal_every: int = max(1, round(2.0 / max(self._poll_interval, 0.05)))
        self._poll_count: int = 0
        self._cycle_count: int = 0
        self._sim_command_state: dict[str, str] = {}

        # Fixed per-sensor calibration-like bias for realism.
        self._cal_bias_rel = self._rng.gauss(0.0, self._sim_spec.accuracy_rel / 3.0)
        self._cdg_full_scale_mbar = self._resolve_cdg_full_scale_mbar()

        # The primary command name/unit to report on each poll.  We prefer
        # the spec's explicit "pressure" entry but fall back to the first
        # readable command if the spec uses different naming.
        self._primary_command, self._primary_unit = self._pick_primary_command()
        self._init_sim_command_state()

        # Mirror GaugeWorker._commands so callers can duck-type the two workers.
        self._commands: list[str] = [
            name for name, cs in self._spec.commands.items()
            if getattr(cs, "read", False)
        ]

    # ------------------------------------------------------------------
    # Convenience accessors (mirror GaugeWorker so callers can duck-type)
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def config(self) -> SimulatedGaugeConfig:
        return self._config

    @property
    def spec(self) -> DeviceSpec:
        return self._spec

    @property
    def is_simulated(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Public control API (call from GUI thread)
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Request the worker to exit cleanly.  Returns immediately."""
        self.requestInterruption()

    def send_terminal_command(self, frame: bytes, command: str = "") -> None:
        """Queue a fake terminal exchange to be emitted on the next cycle.

        A simulated worker never touches a real serial port; instead the
        response is synthesised from the current engine pressure.  The
        request bytes are shown verbatim in the terminal UI, so what the
        user typed is what they see.
        """
        self._terminal_queue.put_nowait((frame, command))

    def set_polling_enabled(self, enabled: bool) -> None:
        """Pause/resume automatic polling (fake terminal commands still run)."""
        self._polling_enabled = bool(enabled)

    def set_commands(self, commands) -> None:
        """Replace the automatic polling command list (mirrors GaugeWorker)."""
        self._commands = [
            cmd for cmd in commands
            if cmd in self._spec.commands and getattr(self._spec.commands[cmd], "read", False)
        ]

    def set_poll_interval(self, interval_s: float) -> None:
        """Update the automatic polling interval in seconds."""
        self._poll_interval = max(float(interval_s), 0.01)
        self._mock_terminal_every = max(1, round(2.0 / max(self._poll_interval, 0.05)))

    # ------------------------------------------------------------------
    # QThread.run — runs on the worker thread
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Emit fake readings on a timer until stop() is requested."""
        self.connected.emit()
        logger.info("[%s] simulated gauge started (%s)", self._device_id, self._family.value)
        # Stagger the first poll by a random fraction of the poll interval so
        # multiple gauges are not polled simultaneously (mirrors real RS-485
        # round-robin polling where each gauge is queried in turn).
        jitter = self._rng.uniform(0.0, self._poll_interval * 0.9)
        self._sleep_interruptible(jitter)
        try:
            while not self.isInterruptionRequested():
                self._drain_terminal_queue()
                if self._polling_enabled:
                    self._cycle_count += 1
                    emit_terminal = (self._cycle_count % self._mock_terminal_every == 0)
                    for command in list(self._commands) or [self._primary_command]:
                        if self.isInterruptionRequested():
                            break
                        try:
                            reading = self._build_reading(command)
                            if reading is not None:
                                self.reading_ready.emit(reading)
                                self._poll_count += 1
                                if emit_terminal:
                                    self._emit_auto_poll_entry(reading.raw, command)
                        except Exception as exc:                   # pragma: no cover
                            logger.exception("[%s] simulation error", self._device_id)
                            self._emit_error(f"Simulation error: {exc}", recoverable=True)
                self._sleep_interruptible(self._poll_interval)
        finally:
            self.disconnected.emit()
            logger.info("[%s] simulated gauge stopped", self._device_id)

    # ------------------------------------------------------------------
    # Reading construction
    # ------------------------------------------------------------------

    def _build_reading(self, command: str | None = None) -> DeviceReading | None:
        """Sample the engine and build a :class:`DeviceReading` for *command*.

        When *command* is ``None`` the primary command is used (backwards-
        compatible with callers that were written before multi-command polling).
        """
        if command is None:
            command = self._primary_command
        real_mbar = self._engine.current_real_pressure()
        gas = self._engine.current_gas()
        now_mono = time.monotonic()

        cs = self._spec.commands.get(command)
        unit = (cs.unit if cs is not None else None) or "mbar"
        is_pressure = unit in _PRESSURE_UNITS

        if is_pressure:
            value = self._apply_response_model(real_mbar, gas, command)
            # Apply first-order sensor lag only to the primary command so
            # multiple sub-sensor reads don't interfere with the lag state.
            if command == self._primary_command:
                value = self._apply_sensor_dynamics(value, now_mono)
        else:
            value = self._synthesize_non_pressure_value(command, real_mbar)

        return DeviceReading(
            device_id=self._device_id,
            timestamp_mono=now_mono,
            timestamp_wall=datetime.now(tz=timezone.utc),
            value=value,
            unit=unit,
            command=command,
            raw=self._fake_response_bytes(value if is_pressure else real_mbar),
        )

    def _synthesize_non_pressure_value(self, command: str, real_mbar: float) -> float:
        """Return a plausible simulated float for non-pressure telemetry commands."""
        lname = command.lower()
        # Temperature: rises slightly with pump-down workload (28–50 °C range).
        if "temperature" in lname:
            p = max(real_mbar, 1e-9)
            norm = max(0.0, min(1.0, (math.log10(p) + 9.0) / 12.0))
            return 28.0 + norm * 22.0
        # Status / error / alarm fields: report OK (0).
        if any(tok in lname for tok in ("error", "status", "fault", "alarm", "state")):
            return 0.0
        # Serial / firmware strings: fixed pseudo-unique integer.
        if any(tok in lname for tok in ("serial", "version", "firmware", "product", "manufacturer")):
            return float(abs(hash(self._device_id)) % 1_000_000)
        return 0.0

    # ------------------------------------------------------------------
    # Family-specific response models
    # ------------------------------------------------------------------

    def _apply_response_model(
        self,
        real_mbar: float,
        gas: GasType,
        command: str,
    ) -> float:
        """Turn the engine's "real" pressure into what this gauge would read."""
        family = self._family

        # OPG550 is explicitly modelled as a Pirani + cold-cathode combination
        # gauge with a pressure-dependent crossover and banded accuracy.
        if self._config.model.upper() == "OPG550":
            return self._model_opg550_combination(real_mbar, gas, command)

        # Combination gauges branch per-command first; whichever sub-family
        # applies drives the noise/range behaviour below.
        if family is GaugeFamily.COMBINATION:
            lc = command.lower()
            if "pirani" in lc or "thermal" in lc:
                family = GaugeFamily.PIRANI
            elif any(k in lc for k in ("ion", "cold", "cc", "bag", "cath")):
                family = GaugeFamily.COLD_CATHODE
            else:
                lo = self._sim_spec.blend_low_mbar or 6e-4
                hi = self._sim_spec.blend_high_mbar or 2e-3
                if real_mbar >= hi:
                    family = GaugeFamily.PIRANI
                elif real_mbar <= lo:
                    family = GaugeFamily.COLD_CATHODE
                else:
                    # In crossover region, return a weighted blend.
                    span = max(hi - lo, 1e-12)
                    frac = (real_mbar - lo) / span
                    p_pirani = self._model_pirani(real_mbar, gas)
                    p_cc = self._model_cold_cathode(real_mbar)
                    if p_cc <= 0.0 or p_cc >= _CC_OVERRANGE:
                        return p_pirani
                    return p_cc * (1.0 - frac) + p_pirani * frac

        if family is GaugeFamily.PIRANI:
            return self._model_pirani(real_mbar, gas)

        if family is GaugeFamily.CDG:
            return self._model_cdg(real_mbar)

        if family is GaugeFamily.COLD_CATHODE:
            return self._model_cold_cathode(real_mbar)

        # VGC controllers and unknown families: pass-through, no noise.
        return max(min(real_mbar, self._sim_spec.max_mbar), self._sim_spec.min_mbar)

    def _model_pirani(self, real_mbar: float, gas: GasType) -> float:
        factor = PIRANI_GAS_FACTOR.get(gas, 1.0)
        pressure = real_mbar * factor
        if pressure > self._sim_spec.max_mbar:
            return _CC_OVERRANGE
        if pressure < self._sim_spec.min_mbar:
            return self._sim_spec.min_mbar
        noise = self._rng.gauss(0.0, self._sim_spec.repeatability_rel / 3.0)
        val = pressure * (1.0 + self._cal_bias_rel + noise)
        return max(min(val, self._sim_spec.max_mbar), self._sim_spec.min_mbar)

    def _model_cdg(self, real_mbar: float) -> float:
        fs = max(self._cdg_full_scale_mbar, 1e-9)
        # CDG minimum reading: 0.05% of full scale per INFICON specs
        cdg_min_mbar = fs * 0.5e-3  # 0.05% of full scale
        p = max(real_mbar, cdg_min_mbar)

        # First two decades below full-scale keep nominal accuracy.
        # Below that, log-linear analog output and sensor nonlinearity degrade.
        acc_abs = fs * self._sim_spec.accuracy_rel
        two_dec_floor = fs / 100.0
        if p < two_dec_floor:
            decades_below = math.log10(two_dec_floor / p)
            acc_abs *= (1.0 + 1.0 * decades_below)

        rep_abs = fs * self._sim_spec.repeatability_rel
        # Clamp absolute noise so readings at the bottom of the range don't jump by
        # full decades.  Peak-to-peak fluctuation is bounded to ±1.5× the CDG minimum
        # detectable pressure (0.05 % FS), keeping the simulated signal realistic near
        # the lowest suggested reading rather than spanning multiple pressure decades.
        max_noise_abs = cdg_min_mbar * 1.5
        bias = max(-max_noise_abs, min(max_noise_abs, self._rng.gauss(0.0, acc_abs / 3.0)))
        noise = max(-max_noise_abs, min(max_noise_abs, self._rng.gauss(0.0, rep_abs / 3.0)))
        return max(min(p + bias + noise, fs), 0.0)

    def _model_cold_cathode(self, real_mbar: float) -> float:
        lower = max(_CC_LOWER_MBAR, self._sim_spec.min_mbar)
        upper = min(_CC_UPPER_MBAR, self._sim_spec.max_mbar)
        if real_mbar > upper:
            return _CC_OVERRANGE
        if real_mbar < lower:
            return _CC_UNDERRANGE

        log_p = math.log10(max(real_mbar, 1e-30))
        log_noise = self._rng.gauss(0.0, _CC_LOG_NOISE_SIGMA)
        return 10 ** (log_p + log_noise)

    def _model_opg550_combination(self, real_mbar: float, gas: GasType, command: str) -> float:
        lc = command.lower()
        if "pirani" in lc or "thermal" in lc:
            base = self._model_pirani(real_mbar, gas)
        elif any(k in lc for k in ("ion", "cold", "cc", "cath")):
            base = self._model_cold_cathode(real_mbar)
        else:
            lo = self._sim_spec.blend_low_mbar or 7e-4
            hi = self._sim_spec.blend_high_mbar or 2e-3
            if real_mbar >= hi:
                base = self._model_pirani(real_mbar, gas)
            elif real_mbar <= lo:
                base = self._model_cold_cathode(real_mbar)
            else:
                span = max(hi - lo, 1e-12)
                frac = (real_mbar - lo) / span
                p_pirani = self._model_pirani(real_mbar, gas)
                p_cc = self._model_cold_cathode(real_mbar)
                if p_cc <= 0.0 or p_cc >= _CC_OVERRANGE:
                    base = p_pirani
                else:
                    base = p_cc * (1.0 - frac) + p_pirani * frac

        if base <= 0.0 or base >= _CC_OVERRANGE:
            return base
        rel_acc = self._opg550_accuracy_rel(max(real_mbar, 1e-12))
        return self._apply_relative_accuracy(base, rel_acc)

    @staticmethod
    def _opg550_accuracy_rel(pressure_mbar: float) -> float:
        """Piecewise relative accuracy envelope used for OPG550 simulation."""
        p = max(pressure_mbar, 1e-12)
        if p >= 100.0:
            return 0.005
        if p >= 2.0:
            return 0.01
        if p >= 1e-4:
            return 0.05
        if p >= 1e-5:
            return 0.25
        return 0.45

    def _apply_relative_accuracy(self, reading_mbar: float, rel_accuracy: float) -> float:
        """Apply bounded bias/noise so readings stay within the target accuracy band."""
        if reading_mbar <= 0.0:
            return reading_mbar
        repeat_rel = min(self._sim_spec.repeatability_rel, rel_accuracy * 0.5)
        bias = self._rng.gauss(0.0, rel_accuracy / 3.0)
        noise = self._rng.gauss(0.0, repeat_rel / 3.0)
        adjusted = reading_mbar * (1.0 + (0.35 * self._cal_bias_rel) + bias + noise)
        return max(min(adjusted, self._sim_spec.max_mbar), self._sim_spec.min_mbar)

    def _apply_sensor_dynamics(self, target: float, now_mono: float) -> float:
        """First-order lag so readings move like real sensors, not instant jumps."""
        if self._last_modelled is None:
            self._last_modelled = target
            self._last_emit_mono = now_mono
            return target
        dt = 0.0 if self._last_emit_mono is None else max(0.0, now_mono - self._last_emit_mono)
        self._last_emit_mono = now_mono
        tau = max(self._sim_spec.response_tau_s, 0.02)
        alpha = 1.0 - math.exp(-dt / tau) if dt > 0.0 else 1.0
        self._last_modelled += alpha * (target - self._last_modelled)
        return self._last_modelled

    def _resolve_cdg_full_scale_mbar(self) -> float:
        """Resolve CDG full scale from config, then YAML, then per-model defaults."""
        if self._family is not GaugeFamily.CDG:
            return self._sim_spec.max_mbar
        if self._config.cdg_full_scale_mbar is not None:
            return max(float(self._config.cdg_full_scale_mbar), 1e-9)

        raw_extra = self._spec.__dict__.get("_raw_extra", {})
        if raw_extra.get("full_scale_mbar") is not None:
            return max(float(raw_extra["full_scale_mbar"]), 1e-9)

        # Use per-model option table (CDG025D vs CDG045D have distinct heads).
        # cdg_full_scale_options() returns CDGFullScaleOption tuples; fall back
        # to the YAML list (plain floats) when present.
        yaml_options = raw_extra.get("full_scale_options_mbar")
        if yaml_options:
            try:
                return max(float(yaml_options[0]), 1e-9)
            except Exception:
                pass
        options = cdg_full_scale_options(self._config.model)
        try:
            # Default to the first 1 mbar / 1 Torr-range option (index 2).
            opt = options[min(2, len(options) - 1)]
            return max(opt.mbar if isinstance(opt, CDGFullScaleOption) else float(opt), 1e-9)
        except Exception:
            return 1.333

    # ------------------------------------------------------------------
    # Fake terminal responses
    # ------------------------------------------------------------------

    def _drain_terminal_queue(self) -> None:
        while not self._terminal_queue.empty():
            try:
                frame, command = self._terminal_queue.get_nowait()
            except _queue.Empty:
                break
            response = self._fake_terminal_response(frame, command)
            self.terminal_response.emit(
                TerminalEntry(
                    request=frame,
                    response=response,
                    timestamp=datetime.now(tz=timezone.utc),
                    command=command,
                )
            )

    def _fake_terminal_response(self, frame: bytes, command: str) -> bytes:
        """Synthesise protocol-plausible response bytes for a terminal command.

        This is cosmetic — the goal is that the terminal view "looks real"
        for a simulated gauge.  We never parse the request; we just craft a
        response in the framing expected by this spec's protocol.
        """
        real = self._engine.current_real_pressure()
        value = self._apply_response_model(real, self._engine.current_gas(),
                                           command or self._primary_command)
        protocol = (self._spec.protocol or "").lower()

        if command:
            custom = self._fake_stateful_command_response(frame, command, protocol)
            if custom is not None:
                return custom

        if protocol == "ppg_ascii":
            return f"@{self._spec.default_address:03d}ACK{value:.3E}\\".encode("ascii")

        if protocol in ("pfeiffer_ascii", "inficon_ascii"):
            cmd_name = command or self._primary_command
            payload = self._format_ascii_payload(cmd_name, value)
            pid = 0
            cmd_spec = self._spec.commands.get(cmd_name)
            if cmd_spec is not None and cmd_spec.pid is not None:
                pid = int(cmd_spec.pid)
            core = (
                f"{self._spec.default_address:03d}10{pid:03d}"
                f"{len(payload):02d}{payload}"
            )
            chk = sum(core.encode("ascii")) % 256
            return f"{core}{chk:03d}\r".encode("ascii")

        if protocol in ("pfeiffer_binary", "inficon_binary"):
            # 4-byte header + 2-byte payload + 2-byte CRC = 8 bytes.
            header = bytes([self._spec.default_address & 0xFF, 0x02, 0x00, 0x02])
            payload_i = max(0, min(int(value * 100), 0xFFFF))
            payload = payload_i.to_bytes(2, "big")
            return header + payload + b"\x00\x00"

        if protocol == "cdg_serial":
            return self._build_cdg_frame(value)

        if protocol == "inficon_p3_v02":
            cmd_name = command or self._primary_command
            cs = self._spec.commands.get(cmd_name)
            unit = (cs.unit if cs is not None else None) or "mbar"
            if unit in _PRESSURE_UNITS:
                display_value = self._apply_response_model(
                    real, self._engine.current_gas(), cmd_name
                )
            else:
                display_value = self._synthesize_non_pressure_value(cmd_name, real)
            return self._build_p3v02_fake_response(cmd_name, display_value)

        return self._fake_response_bytes(value)

    def _build_p3v02_fake_response(self, command: str, value: float) -> bytes:
        """Build a valid INFICON P3 V02 read-response frame for *command*."""
        from serial_comm.protocols.inficon_p3_v02 import (
            build_frame as _p3_build,
            CMD_READ_RESP as _P3_RESP,
        )
        cs = self._spec.commands.get(command)
        pid = int(getattr(cs, "pid", 0) or 0) if cs is not None else 0
        data_type = (
            (getattr(cs, "data_type", None) or "float32_be").lower()
            if cs is not None
            else "float32_be"
        )
        payload = self._encode_p3v02_payload(value, data_type, command)
        # sender_id=0x0B is the OPG550 slave ID; ack=1 marks a slave response.
        return _p3_build(_P3_RESP, pid, payload, addr=0x00, sender_id=0x0B, ack=1)

    def _encode_p3v02_payload(self, value: float, data_type: str, command: str = "") -> bytes:
        """Encode *value* as *data_type* bytes for a P3 V02 response payload."""
        lname = command.lower()
        if data_type == "float32_be":
            return struct.pack(">f", float(value))
        if data_type in ("uint8", "enum_uint8", "bool_uint8"):
            return bytes([max(0, min(255, int(round(value))))])
        if data_type == "uint16_be":
            return struct.pack(">H", max(0, min(65535, int(round(value)))))
        if data_type == "uint32_be":
            return struct.pack(">I", max(0, int(round(value))))
        if data_type == "int32_be":
            return struct.pack(">i", int(round(value)))
        if data_type == "string":
            if any(tok in lname for tok in ("product", "manufacturer")):
                return f"{self._config.model}".encode("ascii") + b"\x00"
            if "version" in lname:
                return b"SIM-1.0\x00"
            if "serial" in lname:
                serial = f"SIM{abs(hash(self._device_id)) % 1_000_000:06d}"
                return serial.encode("ascii") + b"\x00"
            return b"SIM\x00"
        if data_type == "uint16_be_array":
            # Return a minimal 2-element array so the parser sees valid data.
            return struct.pack(">HH", 0, 0)
        # Fallback: encode as float32
        return struct.pack(">f", float(value))

    def _init_sim_command_state(self) -> None:
        for name in self._spec.commands:
            lname = name.lower()
            if "setpoint" in lname:
                if "direction" in lname:
                    self._sim_command_state[name] = "ABOVE"
                elif "enable" in lname:
                    self._sim_command_state[name] = "OFF"
                else:
                    self._sim_command_state[name] = "0"

    def _fake_stateful_command_response(
        self,
        frame: bytes,
        command: str,
        protocol: str,
    ) -> bytes | None:
        if protocol != "ppg_ascii":
            return None
        if command not in self._sim_command_state:
            return None

        text = frame.decode("ascii", errors="ignore")
        is_write = "!" in text
        if is_write:
            payload = text.split("!", 1)[1].rstrip("\\").strip()
            if "," in payload:
                payload = payload.split(",")[-1].strip()
            self._sim_command_state[command] = payload.upper() if payload else self._sim_command_state[command]

        value = self._sim_command_state.get(command, "0")
        return f"@{self._spec.default_address:03d}ACK{value}\\".encode("ascii")

    def _format_ascii_payload(self, command: str, value: float) -> str:
        """Return a protocol-appropriate ASCII data payload for *command*."""
        cmd_spec = self._spec.commands.get(command)
        data_type = (getattr(cmd_spec, "data_type", None) or "string").lower()

        if command == "software_version":
            return "SIM-1.0"
        if command == "serial_number":
            return f"SIM{abs(hash(self._device_id)) % 1000000:06d}"
        if command == "error_status":
            return "OK"
        if command == "temperature":
            p = max(self._engine.current_real_pressure(), 1e-9)
            norm = max(0.0, min(1.0, (math.log10(p) + 9.0) / 12.0))
            temp_c = 28.0 + (norm * 22.0)
            return f"{int(round(temp_c * 100.0)):06d}"

        if data_type in ("u_expo_new", "u_expo"):
            return self._encode_u_expo_new(value)
        if data_type == "u_real":
            return f"{int(round(max(value, 0.0) * 100.0)):06d}"
        if data_type == "u_integer":
            return f"{int(round(max(value, 0.0))):06d}"
        if data_type == "u_short_int":
            return f"{int(round(max(value, 0.0))):03d}"
        if data_type == "boolean_new":
            return "1" if value > 0.5 else "0"
        if data_type == "boolean_old":
            return "111111" if value > 0.5 else "000000"
        return f"{value:.3E}"

    @staticmethod
    def _encode_u_expo_new(value: float) -> str:
        """Encode a positive pressure value into 6-digit u_expo_new form."""
        v = max(float(value), 1e-12)
        exp = int(math.floor(math.log10(v)))
        mant = int(round((v / (10 ** exp)) * 1000.0))
        if mant >= 10000:
            mant //= 10
            exp += 1
        exp_code = max(0, min(99, exp + 20))
        return f"{mant:04d}{exp_code:02d}"

    # Map CDG model name -> sensor type code (byte 7 of CDG frame)
    _CDG_SENSOR_TYPE: dict[str, int] = {
        "CDG025D": 0,
        "CDG045D": 1,
        "CDG100D": 2,
        "CDG160D": 3,
        "CDG200D": 4,
    }

    def _build_cdg_frame(self, modelled_mbar: float) -> bytes:
        """Build a correct 9-byte CDG streaming frame for the given pressure.

        Frame layout (TIRA49E1):
          [0] 0x07  sync
          [1] page (0x00 for continuous stream)
          [2] status byte (setpoint/unit bits — 0x00 = mbar, no SP active)
          [3] error byte (0x01=underrange, 0x02=overrange, 0x00=normal)
          [4] measurement high byte  ⎫ signed int16: value / full_scale * 16384
          [5] measurement low byte   ⎭
          [6] read command echo (0x00)
          [7] sensor type code
          [8] checksum = sum(bytes[1:8]) % 256
        """
        fs = max(self._cdg_full_scale_mbar, 1e-9)
        model_upper = self._config.model.upper()
        sensor_code = self._CDG_SENSOR_TYPE.get(model_upper, 1)

        err_byte = 0x00
        if modelled_mbar >= _CC_OVERRANGE:
            err_byte = 0x02
            meas_i16 = 0x7FFF
        elif modelled_mbar <= 0.0:
            err_byte = 0x01
            meas_i16 = 0
        else:
            raw = int(round(modelled_mbar / fs * 16384.0))
            meas_i16 = max(-32768, min(32767, raw))

        page = 0x00
        status = 0x00  # mbar unit, no setpoints triggered
        echo = 0x00

        if meas_i16 < 0:
            meas_bytes = (meas_i16 & 0xFFFF).to_bytes(2, "big")
        else:
            meas_bytes = meas_i16.to_bytes(2, "big")

        frame = bytearray([0x07, page, status, err_byte,
                           meas_bytes[0], meas_bytes[1],
                           echo, sensor_code])
        checksum = sum(frame[1:8]) & 0xFF
        frame.append(checksum)
        return bytes(frame)

    def _fake_response_bytes(self, value: float) -> bytes:
        """Short generic text representation attached to :class:`DeviceReading`."""
        protocol = (self._spec.protocol or "").lower()
        if protocol == "cdg_serial":
            return self._build_cdg_frame(value)
        return f"SIM:{value:.4E}".encode("ascii")

    def _fake_request_bytes(self, command: str) -> bytes:
        """Synthesise a protocol-plausible request frame for *command*.

        Mirrors the real protocol ``build_request`` framing so the terminal
        displays convincing TX bytes without needing a live serial port.
        When a real protocol codec is attached to this worker it is used
        directly so the bytes are byte-perfect.
        """
        if self._protocol is not None:
            try:
                return self._protocol.build_request(command)
            except Exception:
                pass  # fall through to manual framing below
        protocol = (self._spec.protocol or "").lower()
        addr = self._spec.default_address

        if protocol == "ppg_ascii":
            # @{addr:03d}{mnemonic}?\
            cmds = self._spec.commands
            cs = cmds.get(command)
            mnemonic = (getattr(cs, "mnemonic", None) or command.upper())[:6]
            return f"@{addr:03d}{mnemonic}?\\".encode("ascii")

        if protocol in ("pfeiffer_ascii", "inficon_ascii"):
            # {addr:03d}00{pid:03d}02=?{chk:03d}\r
            cmds = self._spec.commands
            cs = cmds.get(command)
            pid = getattr(cs, "pid", None) or 0
            core = f"{addr:03d}00{pid:03d}02=?"
            chk = sum(core.encode("ascii")) % 256
            return f"{core}{chk:03d}\r".encode("ascii")

        if protocol in ("pfeiffer_binary", "inficon_binary"):
            # Minimal 6-byte read request: addr + action(0x01) + param(0) + len(0)
            return bytes([addr & 0xFF, 0x01, 0x00, 0x00, 0x00, 0x00])

        if protocol == "cdg_serial":
            # 5-byte read command: START=0x03, service=0x00, addr=0x00, data=0x00, checksum=0x00
            return bytes([0x03, 0x00, 0x00, 0x00, 0x00])

        return f"CMD:{command}".encode("ascii")

    def _emit_auto_poll_entry(self, response_bytes: bytes, command: str) -> None:
        """Emit a synthetic TX+RX terminal entry representing an automatic poll."""
        request = self._fake_request_bytes(command)
        # Build a proper protocol-framed response so the terminal parser can
        # decode it without checksum / format errors.  The generic reading.raw
        # bytes are fine for internal accounting but not for display.
        framed = self._fake_terminal_response(request, command)
        self.terminal_response.emit(
            TerminalEntry(
                request=request,
                response=framed,
                timestamp=datetime.now(tz=timezone.utc),
                command=command,
                auto_poll=True,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pick_primary_command(self) -> tuple[str, str]:
        """Return ``(command_name, unit)`` for the per-cycle reading emit."""
        cmds = self._spec.commands
        if "pressure" in cmds:
            cs = cmds["pressure"]
            return "pressure", cs.unit or "mbar"
        for name, cs in cmds.items():
            if getattr(cs, "read", False):
                return name, cs.unit or "mbar"
        return "pressure", "mbar"

    def _emit_error(self, message: str, *, recoverable: bool) -> None:
        now = time.monotonic()
        err = DeviceError(
            device_id=self._device_id,
            timestamp_mono=now,
            timestamp_wall=datetime.now(tz=timezone.utc),
            message=message,
            recoverable=recoverable,
        )
        self.error_occurred.emit(err)

    def _sleep_interruptible(self, seconds: float, chunk: float = 0.05) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.isInterruptionRequested():
                break
            time.sleep(min(chunk, max(0.0, end - time.monotonic())))
