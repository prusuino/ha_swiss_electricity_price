"""The diagnostics download: what the entry is set to and what was read
from the operator's file, with the entry title and unique id redacted."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.elcom_tariffs.const import CONF_CANTONAL_LEVIES, CONF_GRID_PRODUCT, CONF_MUNICIPALITY, CONF_NATIONAL_LEVIES_RP
from custom_components.elcom_tariffs.diagnostics import async_get_config_entry_diagnostics

from .common import CANTONAL, OPERATOR_ID, TARIFF_URL, TARIFFS_2026, TARIFFS_2027, TAXE, automatic_entry, manual_entry, sample_file, setup_entry
from .test_levy_product import LEVY
from .test_tariffs import ENERGY, GRID


async def test_an_automatic_entry_reports_its_selection_and_the_parsed_file(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL, *TARIFFS_2027)
    entry = await setup_entry(hass, automatic_entry({CONF_CANTONAL_LEVIES: [TAXE]}))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["integration"].startswith("SwissElectricityPrice/")
    assert diagnostics["entry"]["title"] == "**REDACTED**"
    assert diagnostics["entry"]["unique_id"] == "**REDACTED**"
    assert OPERATOR_ID not in str(diagnostics["entry"]["unique_id"])
    assert diagnostics["entry"]["version"] == 2
    assert diagnostics["entry"]["data"][CONF_MUNICIPALITY] == "Musterwil"
    assert diagnostics["entry"]["options"][CONF_NATIONAL_LEVIES_RP] == 1.5

    coordinator = diagnostics["coordinator"]
    assert coordinator["loaded"] is True
    assert coordinator["last_update_success"] is True
    assert coordinator["dso_name"] == "Musterwerk AG"
    assert coordinator["tariff_url"] == TARIFF_URL
    assert coordinator["registered_url"] == TARIFF_URL
    assert coordinator["last_checked"] == "2026-11-18T10:00:00+01:00"
    assert coordinator["tariff_valid_from"] == "2026-01-01"
    assert coordinator["tariff_period_end"] == "2026-12-31"
    assert coordinator["tariff_selection"] == "current"
    assert sorted(coordinator["energy_products"]) == ["Basic Energie", "Green Energie"]
    assert coordinator["energy_products"]["Basic Energie"] == {
        "high_rp_kwh": 12.0, "low_rp_kwh": 9.0, "base_price": 5.0, "base_price_unit": "CHF/M",
        "customer_type": "H4", "voltage_level": None, "valid_from": "2026-01-01", "valid_to": "2026-12-31",
        "schedule": [
            {"from": "06:00", "to": "22:00", "weekdays": ["Mo", "Tu", "We", "Th", "Fr"], "rp_kwh": 12.0, "level": "high"},
            {"from": "22:00", "to": "06:00", "weekdays": ["all"], "rp_kwh": 9.0, "level": "low"},
        ],
    }
    assert sorted(coordinator["grid_products"]) == ["Basic Netz", "Doppel Netz"]
    assert coordinator["metering"]["Messung Standard"]["price_chf"] == 3.5
    assert coordinator["municipality_count"] == 2
    assert coordinator["municipality_names_sample"] == ["Beispielingen", "Musterwil"]
    assert coordinator["municipal_levy"]["rp_kwh"] == 0.5
    assert coordinator["product_levy"] is None
    assert coordinator["cantonal_levies"][TAXE]["schedule"][0]["rp_kwh"] == 0.6
    assert coordinator["next_period"] == {
        "valid_from": "2027-01-01", "energy_products": ["Basic Energie"], "grid_products": ["Doppel Netz"], "municipality_count": 1,
    }
    assert coordinator["manual_windows"] is None


async def test_a_file_without_a_next_period_or_municipalities(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, LEVY)
    entry = await setup_entry(hass, automatic_entry({CONF_MUNICIPALITY: "", CONF_GRID_PRODUCT: "Basic Netz"}, unique_id=None))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["entry"]["unique_id"] is None
    coordinator = diagnostics["coordinator"]
    assert coordinator["next_period"] is None
    assert coordinator["municipality_count"] == 0
    assert coordinator["municipal_levy"] is None
    assert coordinator["product_levy"] == {
        "rp_kwh": 0.6, "product_name": "EW Abgabe an die Gemeinde", "valid_from": "2026-01-01", "valid_to": "2026-12-31",
    }


async def test_a_manual_entry_reports_its_windows(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry())
    coordinator = (await async_get_config_entry_diagnostics(hass, entry))["coordinator"]
    assert coordinator["loaded"] is True
    assert coordinator["energy_products"] == {}
    assert [(window["from"], window["to"], window["rp_kwh"], window["level"]) for window in coordinator["manual_windows"]] == [
        ("06:00", "22:00", 25.0, "high"), ("00:00", "00:00", 18.0, "low"),
    ]


async def test_an_entry_that_never_loaded_still_gives_its_settings(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = automatic_entry()
    entry.add_to_hass(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["coordinator"]["loaded"] is False
    assert diagnostics["coordinator"]["last_update_success"] is None
    assert diagnostics["coordinator"]["energy_products"] == {}
    assert diagnostics["entry"]["data"][CONF_MUNICIPALITY] == "Musterwil"
