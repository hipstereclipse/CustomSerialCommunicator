"""
Tests for DeviceRegistry — YAML spec loading and protocol instantiation.
"""

import pytest

from serial_comm.device_registry import DeviceRegistry, DeviceNotFound


@pytest.fixture(scope="module")
def registry() -> DeviceRegistry:
    return DeviceRegistry()


class TestSpecLoading:
    def test_ppg550_loaded(self, registry):
        spec = registry.get_spec("PPG550")
        assert spec.model == "PPG550"
        assert spec.family == "ppg_ascii"

    def test_bcg450_loaded(self, registry):
        spec = registry.get_spec("BCG450")
        assert spec.family == "pfeiffer_ascii"

    def test_pcg550_loaded(self, registry):
        spec = registry.get_spec("PCG550")
        assert spec.family == "pfeiffer_binary"

    def test_cdg045d_loaded(self, registry):
        spec = registry.get_spec("CDG045D")
        assert spec.family == "cdg_serial"

    def test_tc600_loaded(self, registry):
        spec = registry.get_spec("TC600")
        assert spec.model == "TC600"

    def test_opg550_is_experimental(self, registry):
        spec = registry.get_spec("OPG550")
        assert spec.experimental is True

    def test_ppg550_not_experimental(self, registry):
        spec = registry.get_spec("PPG550")
        assert spec.experimental is False

    def test_unknown_model_raises(self, registry):
        with pytest.raises(DeviceNotFound):
            registry.get_spec("NONEXISTENT_GAUGE_9999")

    def test_case_insensitive_lookup(self, registry):
        spec = registry.get_spec("ppg550")
        assert spec.model == "PPG550"

    def test_all_models_non_empty(self, registry):
        assert len(registry.all_models()) > 5

    def test_experimental_models_listed(self, registry):
        exp = registry.experimental_models()
        assert "OPG550" in exp

    def test_ppg550_commands(self, registry):
        spec = registry.get_spec("PPG550")
        assert "pressure" in spec.commands
        assert "temperature" in spec.commands
        assert spec.commands["pressure"].read is True
        assert spec.commands["pressure"].unit == "mbar"

    def test_bcg450_has_pid(self, registry):
        spec = registry.get_spec("BCG450")
        assert spec.commands["pressure"].pid == 340

    def test_ppg550_has_mnemonic(self, registry):
        spec = registry.get_spec("PPG550")
        assert spec.commands["pressure"].mnemonic == "PR3"


class TestMakeProtocol:
    def test_ppg550_protocol_type(self, registry):
        from serial_comm.protocols.ppg_ascii import PPGProtocol
        spec = registry.get_spec("PPG550")
        proto = registry.make_protocol(spec)
        assert isinstance(proto, PPGProtocol)

    def test_bcg450_protocol_type(self, registry):
        from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol
        spec = registry.get_spec("BCG450")
        proto = registry.make_protocol(spec)
        assert isinstance(proto, PfeifferAsciiProtocol)

    def test_pcg550_protocol_type(self, registry):
        from serial_comm.protocols.pfeiffer_binary import PfeifferBinaryProtocol
        spec = registry.get_spec("PCG550")
        proto = registry.make_protocol(spec)
        assert isinstance(proto, PfeifferBinaryProtocol)

    def test_cdg045d_protocol_type(self, registry):
        from serial_comm.protocols.cdg_serial import CDGProtocol
        spec = registry.get_spec("CDG045D")
        proto = registry.make_protocol(spec, address=0)
        assert isinstance(proto, CDGProtocol)

    def test_custom_address_propagated(self, registry):
        spec = registry.get_spec("PPG550")
        proto = registry.make_protocol(spec, address=12)
        assert proto.address == 12

    def test_pcg550_device_id(self, registry):
        from serial_comm.protocols.pfeiffer_binary import PfeifferBinaryProtocol
        spec = registry.get_spec("PCG550")
        proto = registry.make_protocol(spec)
        assert isinstance(proto, PfeifferBinaryProtocol)
        assert proto.device_id == 0x02
