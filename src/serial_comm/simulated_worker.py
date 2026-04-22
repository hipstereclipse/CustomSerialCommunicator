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
import time
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec, TerminalEntry
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
        self._rng = random.Random(hash(config.sim_id) & 0xFFFFFFFF)
        self._last_emit_mono: float | None = None
        self._last_modelled: float | None = None
        # Auto-poll mock terminal: emit one exchange every this many polls.
        # Keeps the terminal readable without flooding it at high poll rates.
        self._mock_terminal_every: int = max(1, round(2.0 / max(self._poll_interval, 0.05)))
        self._poll_count: int = 0

        # Fixed per-sensor calibration-like bias for realism.
        self._cal_bias_rel = self._rng.gauss(0.0, self._sim_spec.accuracy_rel / 3.0)
        self._cdg_full_scale_mbar = self._resolve_cdg_full_scale_mbar()

        # The primary command name/unit to report on each poll.  We prefer
        # the spec's explicit "pressure" entry but fall back to the first
        # readable command if the spec uses different naming.
        self._primary_command, self._primary_unit = self._pick_primary_command()

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
                    try:
                        reading = self._build_reading()
                        if reading is not None:
                            self.reading_ready.emit(reading)
                            self._poll_count += 1
                            if self._poll_count % self._mock_terminal_every == 0:
                                self._emit_auto_poll_entry(
                                    reading.raw, self._primary_command
                                )
                    except Exception as exc:                       # pragma: no cover
                        logger.exception("[%s] simulation error", self._device_id)
                        self._emit_error(f"Simulation error: {exc}", recoverable=True)
                self._sleep_interruptible(self._poll_interval)
        finally:
            self.disconnected.emit()
            logger.info("[%s] simulated gauge stopped", self._device_id)

    # ------------------------------------------------------------------
    # Reading construction
    # ------------------------------------------------------------------

    def _build_reading(self) -> DeviceReading | None:
        """Sample the engine and build a :class:`DeviceReading`."""
        real_mbar = self._engine.current_real_pressure()
        gas = self._engine.current_gas()
        modelled = self._apply_response_model(real_mbar, gas, self._primary_command)
        now_mono = time.monotonic()
        modelled = self._apply_sensor_dynamics(modelled, now_mono)
        return DeviceReading(
            device_id=self._device_id,
            timestamp_mono=now_mono,
            timestamp_wall=datetime.now(tz=timezone.utc),
            value=modelled,
            unit=self._primary_unit,
            command=self._primary_command,
            raw=self._fake_response_bytes(modelled),
        )

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
        if real_mbar > fs:
            return _CC_OVERRANGE
        p = max(real_mbar, fs * 1e-6)

        # First two decades below full-scale keep nominal accuracy.
        # Below that, log-linear analog output and sensor nonlinearity degrade.
        acc_abs = fs * self._sim_spec.accuracy_rel
        two_dec_floor = fs / 100.0
        if p < two_dec_floor:
            decades_below = math.log10(two_dec_floor / p)
            acc_abs *= (1.0 + 1.0 * decades_below)

        rep_abs = fs * self._sim_spec.repeatability_rel
        bias = self._rng.gauss(0.0, acc_abs / 3.0)
        noise = self._rng.gauss(0.0, rep_abs / 3.0)
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

        if protocol == "ppg_ascii":
            return f"@{self._spec.default_address:03d}ACK{value:.3E}\\".encode("ascii")

        if protocol in ("pfeiffer_ascii", "inficon_ascii"):
            # <addr>10<pid><len><payload><chk>\r — an opaque but well-formed frame.
            payload = f"{value:.3E}"
            core = f"{self._spec.default_address:03d}10{0:03d}{len(payload):02d}{payload}"
            chk = sum(core.encode("ascii")) % 256
            return f"{core}{chk:03d}\r".encode("ascii")

        if protocol in ("pfeiffer_binary", "inficon_binary"):
            # 4-byte header + 2-byte payload + 2-byte CRC = 8 bytes.
            header = bytes([self._spec.default_address & 0xFF, 0x02, 0x00, 0x02])
            payload_i = max(0, min(int(value * 100), 0xFFFF))
            payload = payload_i.to_bytes(2, "big")
            return header + payload + b"\x00\x00"

        if protocol == "cdg_serial":
            # Synchronisation byte + raw payload, fixed length 9.
            raw = int(max(0.0, min(value * 1000, 0xFFFF))).to_bytes(2, "big")
            return b"\x07" + raw + b"\x00" * 6

        return self._fake_response_bytes(value)

    def _fake_response_bytes(self, value: float) -> bytes:
        """Short generic text representation attached to :class:`DeviceReading`."""
        return f"SIM:{value:.4E}".encode("ascii")

    def _fake_request_bytes(self, command: str) -> bytes:
        """Synthesise a protocol-plausible request frame for *command*.

        Mirrors the real protocol ``build_request`` framing so the terminal
        displays convincing TX bytes without needing a live serial port.
        """
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
            # Start byte only (device responds after any master byte)
            return bytes([0x05])

        return f"READ:{command}".encode("ascii")

    def _emit_auto_poll_entry(self, response_bytes: bytes, command: str) -> None:
        """Emit a synthetic TX+RX terminal entry representing an automatic poll."""
        request = self._fake_request_bytes(command)
        self.terminal_response.emit(
            TerminalEntry(
                request=request,
                response=response_bytes,
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
