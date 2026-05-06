from serial_comm.opg_spectrum import (
    SpectrumMode,
    identify_optical_species,
    optical_signature_wavelengths,
    simulate_optical_spectrum,
)


def _peak_at_nm(spectrum: list[float], wavelength_nm: float) -> float:
    n = len(spectrum)
    idx = int(round(((wavelength_nm - 380.0) / 400.0) * (n - 1)))
    lo = max(0, idx - 3)
    hi = min(n - 1, idx + 3)
    return max(spectrum[lo : hi + 1])


def test_helium_mode_has_strong_588nm_line() -> None:
    spec = simulate_optical_spectrum(
        pressure_mbar=2.0e-4,
        trend_mbar_per_s=6.0e-7,
        elapsed_s=120.0,
        mode=SpectrumMode.HELIUM_LEAK,
    )
    assert _peak_at_nm(spec, 588.0) > 0.55


def test_air_leak_identifies_n2_or_o2() -> None:
    spec = simulate_optical_spectrum(
        pressure_mbar=6.0e-2,
        trend_mbar_per_s=1.2e-4,
        elapsed_s=90.0,
        mode=SpectrumMode.AIR_LEAK,
    )
    matches = identify_optical_species(spec, top_k=3)
    names = {m.name for m in matches}
    assert "N2" in names or "O2" in names


def test_auto_mode_changes_with_rising_pressure() -> None:
    calm = simulate_optical_spectrum(
        pressure_mbar=1.0e-5,
        trend_mbar_per_s=0.0,
        elapsed_s=1200.0,
        mode=SpectrumMode.AUTO,
    )
    leaking = simulate_optical_spectrum(
        pressure_mbar=1.0e-3,
        trend_mbar_per_s=8.0e-6,
        elapsed_s=1250.0,
        mode=SpectrumMode.AUTO,
    )
    calm_ratio = _peak_at_nm(calm, 630.0) / max(_peak_at_nm(calm, 742.0), 1e-9)
    leaking_ratio = _peak_at_nm(leaking, 630.0) / max(_peak_at_nm(leaking, 742.0), 1e-9)
    assert leaking_ratio > calm_ratio


def test_signature_wavelengths_exposes_known_lines() -> None:
    assert 656.0 in optical_signature_wavelengths("H2")
    assert optical_signature_wavelengths("not-a-gas") == ()
