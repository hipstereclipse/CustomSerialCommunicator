"""
DeviceRegistry — loads YAML device specs and instantiates protocol codecs.

Usage::

    from serial_comm.device_registry import DeviceRegistry

    registry = DeviceRegistry()            # loads all YAML files from default location
    spec = registry.get_spec("PPG550")     # DeviceSpec
    protocol = registry.make_protocol(spec, address=254)  # GaugeProtocol
"""

from __future__ import annotations

import importlib.resources
import logging
from pathlib import Path
from typing import Any

import yaml

from serial_comm.models import CommandSpec, DeviceSpec
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

# Resolve the device_specs/ directory relative to the repo root.
# This works whether the package is installed or run from source.
_REPO_ROOT = Path(__file__).resolve().parents[2]  # src/serial_comm/device_registry.py → repo root
_GAUGE_SPECS_DIR = _REPO_ROOT / "device_specs" / "gauges"
_TURBO_SPECS_DIR = _REPO_ROOT / "device_specs" / "turbos"


class DeviceNotFound(KeyError):
    """Raised when a model name is not in the registry."""


class DeviceRegistry:
    """
    Loads all *.yaml files from the device_specs/ directories and provides
    DeviceSpec lookup and GaugeProtocol instantiation.
    """

    def __init__(
        self,
        gauge_specs_dir: Path | None = None,
        turbo_specs_dir: Path | None = None,
    ) -> None:
        self._specs: dict[str, DeviceSpec] = {}
        self._load_dir(gauge_specs_dir or _GAUGE_SPECS_DIR, device_class="gauge")
        self._load_dir(turbo_specs_dir or _TURBO_SPECS_DIR, device_class="turbo")
        logger.info("DeviceRegistry loaded %d device specs", len(self._specs))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_spec(self, model: str) -> DeviceSpec:
        try:
            return self._specs[model.upper()]
        except KeyError:
            available = ", ".join(sorted(self._specs))
            raise DeviceNotFound(
                f"Unknown model '{model}'. Available: {available}"
            ) from None

    def all_models(self) -> list[str]:
        return sorted(self._specs)

    def stable_models(self) -> list[str]:
        return sorted(m for m, s in self._specs.items() if not s.experimental)

    def experimental_models(self) -> list[str]:
        return sorted(m for m, s in self._specs.items() if s.experimental)

    def make_protocol(self, spec: DeviceSpec, address: int | None = None) -> GaugeProtocol:
        """Instantiate the correct GaugeProtocol subclass for *spec*."""
        addr = address if address is not None else spec.default_address

        if spec.protocol == "ppg_ascii":
            from serial_comm.protocols.ppg_ascii import PPGProtocol
            param_table = self._build_param_table(spec)
            return PPGProtocol(address=addr, gauge_type=spec.model, param_table=param_table)

        if spec.protocol == "pfeiffer_ascii":
            from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol
            param_table = self._build_param_table(spec)
            return PfeifferAsciiProtocol(address=addr, param_table=param_table)

        if spec.protocol == "pfeiffer_binary":
            from serial_comm.protocols.pfeiffer_binary import PfeifferBinaryProtocol
            param_table = self._build_param_table(spec)
            # device_id and pressure_enc are stored in _raw_extra
            raw = spec.__dict__.get("_raw_extra", {})
            device_id = int(raw.get("device_id", 0x02), 16) if isinstance(
                raw.get("device_id"), str
            ) else raw.get("device_id", 0x02)
            pressure_enc = raw.get("pressure_enc", "fixs32en20")
            return PfeifferBinaryProtocol(
                address=addr,
                device_id=device_id,
                pressure_enc=pressure_enc,
                param_table=param_table,
            )

        if spec.protocol == "cdg_serial":
            from serial_comm.protocols.cdg_serial import CDGProtocol
            return CDGProtocol(gauge_type=spec.model, address=addr)

        raise ValueError(f"Unknown protocol '{spec.protocol}' for model '{spec.model}'")

    # ------------------------------------------------------------------
    # YAML loading
    # ------------------------------------------------------------------

    def _load_dir(self, directory: Path, device_class: str) -> None:
        if not directory.is_dir():
            logger.warning("Device spec directory not found: %s", directory)
            return
        for yaml_file in sorted(directory.glob("*.yaml")):
            try:
                spec = self._load_yaml(yaml_file, device_class)
                self._specs[spec.model.upper()] = spec
                logger.debug("Loaded spec: %s (%s)", spec.model,
                             "experimental" if spec.experimental else "stable")
            except Exception as exc:
                logger.error("Failed to load %s: %s", yaml_file.name, exc)

    def _load_yaml(self, path: Path, device_class: str) -> DeviceSpec:
        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)

        transport = raw.get("transport", {})
        addr_range = transport.get("rs485_address_range")
        commands: dict[str, CommandSpec] = {}

        for cmd_name, cmd_raw in raw.get("commands", {}).items():
            commands[cmd_name] = CommandSpec(
                name=cmd_name,
                read=cmd_raw.get("read", False),
                write=cmd_raw.get("write", False),
                unit=cmd_raw.get("unit", ""),
                description=cmd_raw.get("description", ""),
                pid=cmd_raw.get("pid"),
                data_type=cmd_raw.get("data_type"),
                mnemonic=cmd_raw.get("mnemonic"),
                min_value=cmd_raw.get("min_value"),
                max_value=cmd_raw.get("max_value"),
                scale=cmd_raw.get("scale", 1.0),
                options=cmd_raw.get("options", []),
                experimental=cmd_raw.get("experimental", False),
            )

        spec = DeviceSpec(
            model=raw["model"],
            family=raw["family"],
            protocol=raw["protocol"],
            default_baud=transport.get("default_baud", 9600),
            parity=transport.get("parity", "N"),
            data_bits=transport.get("data_bits", 8),
            stop_bits=transport.get("stop_bits", 1),
            rs_modes=transport.get("rs_modes", ["RS232"]),
            default_address=transport.get("default_address", 254),
            rs485_address_range=(
                tuple(addr_range) if addr_range else None  # type: ignore[arg-type]
            ),
            commands=commands,
            experimental=raw.get("experimental", False),
        )
        # Stash extra fields (device_id, pressure_enc) for pfeiffer_binary
        spec.__dict__["_raw_extra"] = raw
        return spec

    @staticmethod
    def _build_param_table(spec: DeviceSpec) -> dict[str, Any]:
        """Convert CommandSpec objects back to the dict format protocols expect."""
        table: dict[str, Any] = {}
        for name, cmd in spec.commands.items():
            entry: dict[str, Any] = {
                "read": cmd.read,
                "write": cmd.write,
                "unit": cmd.unit,
            }
            if cmd.pid is not None:
                entry["pid"] = cmd.pid
            if cmd.data_type is not None:
                entry["data_type"] = cmd.data_type
            if cmd.mnemonic is not None:
                entry["mnemonic"] = cmd.mnemonic
            raw_extra = spec.__dict__.get("_raw_extra", {})
            raw_cmds = raw_extra.get("commands", {}).get(name, {})
            entry.update({
                k: v for k, v in raw_cmds.items()
                if k not in entry and k not in ("read", "write", "description",
                                                "mnemonic", "experimental")
            })
            table[name] = entry
        return table
