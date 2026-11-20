"""Sensors of an entry on the operator's tariff file, after a real setup:
the combined price with its calculation and price curve, the high/low
reference prices, the next tariff period's preview, the product, fee,
levy and diagnostic sensors, which of them start disabled, and what
happens when the file changes under a running entry."""
from __future__ import annotations

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.elcom_tariffs.const import (
    CONF_CANTONAL_LEVIES,
    CONF_CANTONAL_LEVIES_VAT_EXEMPT,
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPALITY,
    CONF_VAT_EXEMPT_LEVIES_RP,
    DOMAIN,
    VERSION,
)

from .common import (
    CANTONAL,
    EMOLUMENT,
    ENTRY_ID,
    GENEVA,
    LUMP_SUM,
    PREFIX,
    REDEVANCE,
    TARIFF_URL,
    TARIFFS_2026,
    TAXE,
    automatic_entry,
    device_of,
    name_of,
    next_year,
    refresh,
    sample_file,
    setup_entry,
    state_of,
    travel,
    value_of,
)
from .test_levy_product import LEVY
from .test_tariffs import ENERGY, FEES, GRID

PRICE = f"sensor.{PREFIX}_price_current"
PRICE_HIGH = f"sensor.{PREFIX}_price_high"
PRICE_LOW = f"sensor.{PREFIX}_price_low"
PREVIEW = f"sensor.{PREFIX}_price_next_period"
LEVEL = f"sensor.{PREFIX}_tariff_level"

SCHEDULE = [
    {"from": "06:00", "to": "22:00", "weekdays": ["Mo", "Tu", "We", "Th", "Fr"], "rp_kwh": 12.0, "level": "high"},
    {"from": "22:00", "to": "06:00", "weekdays": ["all"], "rp_kwh": 9.0, "level": "low"},
]


def _enabled_by_default(hass: HomeAssistant, entity_id: str) -> bool:
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None, f"{entity_id} is not registered"
    return registered.disabled_by is None


async def test_the_current_price_adds_up_energy_grid_levies_and_vat(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry({CONF_GRID_INCLUDES_LEVIES: False}))
    state = state_of(hass, PRICE)
    # weekday 10:00: 12 Rp energy + 10 Rp grid + 0.5 Rp Musterwil + 1.5 Rp national = 24 Rp, x 1.081 = 25.944 Rp
    assert float(state.state) == 0.25944
    assert state.attributes["unit_of_measurement"] == "CHF/kWh"
    assert name_of(hass, state.entity_id) == "Current Electricity Price"
    assert state.attributes["calculation"] == (
        "12.00 Rp energy (Basic Energie) + 10.00 Rp grid (Doppel Netz) + 0.50 Rp municipality levy (Musterwil)"
        " + 1.50 Rp national levies = 24.00 Rp, × 1.081 (8.1% VAT) = 25.94 Rp/kWh = 0.2594 CHF/kWh"
    )
    assert state.attributes["energy_rp_kwh"] == 12.0
    assert state.attributes["grid_rp_kwh"] == 10.0
    assert state.attributes["municipality_rp_kwh"] == 0.5
    assert state.attributes["national_levies_rp_kwh"] == 1.5
    assert state.attributes["vat_percent"] == 8.1
    assert state.attributes["cantonal_levies"] == []
    assert state.attributes["cantonal_levies_rp_kwh"] == 0.0
    assert state.attributes["vat_exempt_levies_rp_kwh"] == 0.0
    assert state.attributes["municipality"] == "Musterwil"
    assert state.attributes["energy_product"] == "Basic Energie"
    assert state.attributes["grid_product"] == "Doppel Netz"
    assert state.attributes["tariff_level"] == "High tariff"
    assert state.attributes["grid_includes_national_levies"] is False


async def test_the_price_curve_covers_today_and_tomorrow_by_the_hour(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    attributes = state_of(hass, PRICE).attributes
    # low tariff: 9 + 6 + 0.5 + 1.5 = 17 Rp, x 1.081 = 18.377 Rp
    today = attributes["raw_today"]
    assert len(today) == 24
    assert today[0] == {"start": "2026-11-18T00:00:00+01:00", "end": "2026-11-18T01:00:00+01:00", "value": 0.18377}
    assert [slot["value"] for slot in today] == [0.18377] * 6 + [0.25944] * 16 + [0.18377] * 2
    tomorrow = attributes["raw_tomorrow"]
    assert len(tomorrow) == 24
    assert tomorrow[0]["start"] == "2026-11-19T00:00:00+01:00"
    assert tomorrow[23]["end"] == "2026-11-20T00:00:00+01:00"
    assert attributes["min_today"] == 0.18377
    assert attributes["max_today"] == 0.25944
    # 8 hours at 0.18377 and 16 at 0.25944
    assert attributes["avg_today"] == 0.23422
    assert attributes["price_next_hour"] == 0.25944
    assert attributes["next_change"] == "2026-11-18T22:00:00+01:00"
    assert attributes["next_price"] == 0.18377
    assert attributes["next_level"] == "Low tariff"
    assert "next_level_key" not in attributes


async def test_the_price_curve_has_25_hours_when_the_clocks_go_back(hass: HomeAssistant, freezer, operator_host) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to("2026-10-25 12:00:00+01:00")
    await setup_entry(hass, automatic_entry())
    attributes = state_of(hass, PRICE).attributes
    assert len(attributes["raw_today"]) == 25
    assert attributes["raw_today"][2]["start"] == "2026-10-25T02:00:00+02:00"
    assert attributes["raw_today"][3]["start"] == "2026-10-25T02:00:00+01:00"
    assert len(attributes["raw_tomorrow"]) == 24
    # a Sunday: no high-tariff window, every hour at the low rate
    assert {slot["value"] for slot in attributes["raw_today"]} == {0.18377}


async def test_the_price_drops_to_the_low_rate_at_22_00(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    assert value_of(hass, PRICE) == 0.25944
    assert state_of(hass, LEVEL).state == "High tariff"
    assert state_of(hass, LEVEL).attributes["next_change"] == "2026-11-18T22:00:00+01:00"
    assert state_of(hass, LEVEL).attributes["next_level"] == "Low tariff"
    assert state_of(hass, LEVEL).attributes["schedule"] == SCHEDULE
    assert state_of(hass, LEVEL).attributes["energy_product"] == "Basic Energie"

    await travel(hass, wednesday_morning, "2026-11-18 22:00:01+01:00")
    assert value_of(hass, PRICE) == 0.18377
    assert state_of(hass, LEVEL).state == "Low tariff"
    assert state_of(hass, LEVEL).attributes["next_change"] == "2026-11-19T06:00:00+01:00"
    assert value_of(hass, f"sensor.{PREFIX}_energy_basic_energie") == 0.09
    assert operator_host.fetched == [TARIFF_URL]  # pushed from the data at hand, not fetched again

    # the timer is armed again for the next boundary
    await travel(hass, wednesday_morning, "2026-11-19 06:00:01+01:00")
    assert value_of(hass, PRICE) == 0.25944
    assert state_of(hass, LEVEL).state == "High tariff"


async def test_the_reference_prices_do_not_follow_the_clock(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    high, low = state_of(hass, PRICE_HIGH), state_of(hass, PRICE_LOW)
    assert float(high.state) == 0.25944
    assert float(low.state) == 0.18377
    assert name_of(hass, high.entity_id) == "Electricity Price High tariff"
    assert name_of(hass, low.entity_id) == "Electricity Price Low tariff"
    assert low.attributes["calculation"] == (
        "9.00 Rp energy (Basic Energie) + 6.00 Rp grid (Doppel Netz) + 0.50 Rp municipality levy (Musterwil)"
        " + 1.50 Rp national levies = 17.00 Rp, × 1.081 (8.1% VAT) = 18.38 Rp/kWh = 0.1838 CHF/kWh"
    )
    assert low.attributes["energy_rp_kwh"] == 9.0 and low.attributes["grid_rp_kwh"] == 6.0

    await travel(hass, wednesday_morning, "2026-11-18 23:00:00+01:00")
    assert value_of(hass, PRICE_HIGH) == 0.25944
    assert value_of(hass, PRICE_LOW) == 0.18377


async def test_a_flat_grid_product_leaves_the_reference_prices_unknown(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    # 12 + 8 + 0.5 + 1.5 = 22 Rp, x 1.081 = 23.782 Rp
    assert value_of(hass, PRICE) == 0.23782
    assert state_of(hass, PRICE_HIGH).state == STATE_UNKNOWN
    assert state_of(hass, PRICE_LOW).state == STATE_UNKNOWN
    assert "calculation" not in state_of(hass, PRICE_HIGH).attributes


async def test_every_product_of_the_file_gets_its_rate_sensor(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    basic = state_of(hass, f"sensor.{PREFIX}_energy_basic_energie")
    assert float(basic.state) == 0.12
    assert name_of(hass, basic.entity_id) == "Basic Energie"
    assert basic.attributes["current_level"] == "High tariff"
    assert basic.attributes["base_price_chf_month"] == 5.0
    assert basic.attributes["customer_type"] == "H4"
    assert basic.attributes["valid_from"] == "2026-01-01" and basic.attributes["valid_to"] == "2026-12-31"
    assert basic.attributes["schedule"] == SCHEDULE

    high = state_of(hass, f"sensor.{PREFIX}_energy_basic_energie_high")
    assert float(high.state) == 0.12
    assert name_of(hass, high.entity_id) == "Basic Energie (High tariff)"
    assert high.attributes["valid_to"] == "2026-12-31"
    assert value_of(hass, f"sensor.{PREFIX}_energy_basic_energie_low") == 0.09
    assert value_of(hass, f"sensor.{PREFIX}_grid_doppel_netz") == 0.10
    assert value_of(hass, f"sensor.{PREFIX}_grid_doppel_netz_high") == 0.10
    assert value_of(hass, f"sensor.{PREFIX}_grid_doppel_netz_low") == 0.06

    green = state_of(hass, f"sensor.{PREFIX}_energy_green_energie")
    assert float(green.state) == 0.14
    assert green.attributes["current_level"] == "Flat tariff"
    assert value_of(hass, f"sensor.{PREFIX}_grid_basic_netz") == 0.08
    # a flat product has no high and low reference, a product without a base price no fee sensor
    registry = er.async_get(hass)
    assert registry.async_get(f"sensor.{PREFIX}_energy_green_energie_high") is None
    assert registry.async_get(f"sensor.{PREFIX}_energy_green_energie_base_fee") is None


async def test_fee_sensors_carry_the_unit_of_the_file(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    base_fee = state_of(hass, f"sensor.{PREFIX}_energy_basic_energie_base_fee")
    assert float(base_fee.state) == 5.0
    assert base_fee.attributes["unit_of_measurement"] == "CHF/month"
    assert name_of(hass, base_fee.entity_id) == "Basic Energie (Base Fee)"
    assert base_fee.attributes["price_unit"] == "CHF/M"
    assert base_fee.attributes["customer_type"] == "H4"

    monthly = state_of(hass, f"sensor.{PREFIX}_metering_messung_standard")
    assert float(monthly.state) == 3.5
    assert monthly.attributes["unit_of_measurement"] == "CHF/month"
    assert monthly.attributes["customer_type"] == "H4"
    assert monthly.attributes["valid_from"] == "2026-01-01"
    yearly = state_of(hass, f"sensor.{PREFIX}_metering_messung_lastgang")
    assert float(yearly.state) == 120.0
    assert yearly.attributes["unit_of_measurement"] == "CHF/year"
    # a unit the format does not know is shown as the operator wrote it
    assert state_of(hass, f"sensor.{PREFIX}_metering_messung_quartal").attributes["unit_of_measurement"] == "CHF/Quartal"


async def test_a_fee_without_a_unit_is_per_month(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    metering = {"tariffType": "metering", "tariffName": "Messung", "startDate": "2026-01-01", "prices": {"base": {"price": 4.0}}}
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, FEES, metering)
    await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    assert state_of(hass, f"sensor.{PREFIX}_metering_messung").attributes["unit_of_measurement"] == "CHF/month"


async def test_only_what_the_entry_is_set_to_starts_enabled(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    musterwil = state_of(hass, f"sensor.{PREFIX}_municipality_levy_musterwil")
    assert float(musterwil.state) == 0.5
    assert musterwil.attributes["unit_of_measurement"] == "Rp/kWh"
    assert name_of(hass, musterwil.entity_id) == "Municipality Levy Musterwil"
    assert musterwil.attributes["base_price_chf_month"] == 0
    assert musterwil.attributes["tariff_record"] == "Gemeindeabgaben"
    assert musterwil.attributes["resolved_by"] == "single_record"
    # the other municipality of the operator and the base fee of the grid product not chosen
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_municipality_levy_beispielingen") is False
    assert hass.states.get(f"sensor.{PREFIX}_municipality_levy_beispielingen") is None
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_grid_basic_netz_base_fee") is False
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_energy_basic_energie_base_fee") is True


async def test_diagnostic_sensors_tell_where_the_file_comes_from(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    host = state_of(hass, f"sensor.{PREFIX}_diagnostics_host")
    assert host.state == "example.invalid"
    assert host.attributes["tariff_file_url"] == TARIFF_URL
    assert host.attributes["registered_url"] == TARIFF_URL
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_path").state == "/files"
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_filename").state == "tarife_2026.json"
    year = state_of(hass, f"sensor.{PREFIX}_diagnostics_year")
    assert year.state == "2026"
    assert year.attributes["valid_to"] == "2026-12-31"
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_last_checked").state == "2026-11-18T09:00:00+00:00"
    registered = er.async_get(hass).async_get(f"sensor.{PREFIX}_diagnostics_host")
    assert registered.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_the_validity_year_falls_back_to_the_file_name(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    undated = [{key: value for key, value in record.items() if key not in ("startDate", "endDate")} for record in (ENERGY, GRID, FEES)]
    operator_host.files["https://example.invalid/tarife_2026.json"] = sample_file(*undated)
    entry = automatic_entry({"tariff_url": "https://example.invalid/tarife_2026.json", CONF_GRID_PRODUCT: "Basic Netz"})
    await setup_entry(hass, entry)
    year = state_of(hass, f"sensor.{PREFIX}_diagnostics_year")
    assert year.state == "2026"
    assert "valid_to" not in year.attributes
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_path").state == "/"  # the file sits at the root of the host
    assert entry.runtime_data.data["tariff_selection"] == "undated"


async def test_the_device_is_named_after_operator_place_and_product(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    device = device_of(hass, entry)
    assert device.identifiers == {(DOMAIN, ENTRY_ID)}
    assert device.name == "Musterwerk AG Electricity Prices (Musterwil, Basic Energie)"
    assert device.manufacturer == "Musterwerk AG"
    assert device.model == "Machine-Readable Tariffs (ElCom / VSE Standard)"
    assert device.sw_version == VERSION
    assert device.entry_type is dr.DeviceEntryType.SERVICE


async def test_the_preview_prices_the_next_tariff_period(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    preview = state_of(hass, PREVIEW)
    # 2027, weekday 10:00: 13 + 11 + 0.6 (next year's levy) + 1.5 = 26.1 Rp, x 1.081 = 28.2141 Rp
    assert float(preview.state) == 0.28214
    assert name_of(hass, preview.entity_id) == "Electricity Price Next Tariff Period"
    assert preview.attributes["valid_from"] == "2027-01-01"
    assert preview.attributes["energy_product"] == "Basic Energie"
    assert preview.attributes["grid_product"] == "Doppel Netz"
    assert preview.attributes["municipality"] == "Musterwil"
    assert preview.attributes["energy_rp_kwh_high"] == 13.0 and preview.attributes["energy_rp_kwh_low"] == 10.0
    assert preview.attributes["grid_rp_kwh_high"] == 11.0 and preview.attributes["grid_rp_kwh_low"] == 7.0
    assert preview.attributes["municipality_rp_kwh"] == 0.6
    assert preview.attributes["price_high"] == 0.28214
    # 10 + 7 + 0.6 + 1.5 = 19.1 Rp, x 1.081 = 20.6471 Rp
    assert preview.attributes["price_low"] == 0.20647
    assert preview.attributes["current_price_high"] == 0.25944
    assert preview.attributes["current_price_low"] == 0.18377
    # 0.28214 / 0.25944 and 0.20647 / 0.18377
    assert preview.attributes["change_high_percent"] == 8.7
    assert preview.attributes["change_low_percent"] == 12.4

    await travel(hass, wednesday_morning, "2026-11-18 22:30:00+01:00")
    assert value_of(hass, PREVIEW) == 0.20647


async def test_the_preview_of_a_flat_product_uses_its_one_rate(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    """Next year's set without a municipality section: today's levy stays."""
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, FEES, next_year(ENERGY), next_year(GRID, {"energy": [{"from": "00:00", "to": "00:00", "price": 0.085}]}))
    await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    preview = state_of(hass, PREVIEW)
    # 12 + 8.5 + 0.5 (today's levy) + 1.5 = 22.5 Rp, x 1.081 = 24.3225 Rp
    assert float(preview.state) == pytest.approx(0.24322, abs=1e-5)
    assert preview.attributes["grid_rp_kwh_high"] == 8.5 and preview.attributes["grid_rp_kwh_low"] == 8.5
    assert preview.attributes["municipality_rp_kwh"] == 0.5
    # low: 9 + 8.5 + 0.5 + 1.5 = 19.5 Rp, x 1.081 = 21.0795 Rp; today 9 + 8 + 0.5 + 1.5 = 19 Rp, x 1.081 = 20.539 Rp
    assert preview.attributes["price_low"] == pytest.approx(0.21079, abs=1e-5)
    assert preview.attributes["current_price_low"] == 0.20539
    assert preview.attributes["change_low_percent"] == 2.6


@pytest.mark.parametrize(
    ("data", "upcoming"),
    [
        # the grid product is not in next year's set
        ({CONF_GRID_PRODUCT: "Basic Netz"}, None),
        # the municipality is not in next year's municipality section
        ({CONF_MUNICIPALITY: "Beispielingen"}, None),
        # nothing published beyond this year
        ({}, TARIFFS_2026),
    ],
)
async def test_no_preview_without_a_comparable_next_period(hass: HomeAssistant, wednesday_morning, operator_host, data, upcoming) -> None:
    if upcoming:
        operator_host.files[TARIFF_URL] = sample_file(*upcoming)
    await setup_entry(hass, automatic_entry(data))
    assert state_of(hass, PRICE).state != STATE_UNAVAILABLE
    assert state_of(hass, PREVIEW).state == STATE_UNAVAILABLE


async def test_the_preview_of_a_municipality_that_is_new_next_year_has_nothing_to_compare_with(
    hass: HomeAssistant, wednesday_morning, operator_host
) -> None:
    fees_2027 = next_year(FEES, {"municipalityTaxes": [{"municipalityName": "Neuwil", "municipalityEnergy": [{"price": 0.004}]}]})
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, next_year(ENERGY), next_year(GRID), fees_2027)
    await setup_entry(hass, automatic_entry({CONF_MUNICIPALITY: "Neuwil", CONF_GRID_PRODUCT: "Basic Netz"}))
    assert state_of(hass, PRICE).state == STATE_UNAVAILABLE
    preview = state_of(hass, PREVIEW)
    # 12 + 8 + 0.4 + 1.5 = 21.9 Rp, x 1.081 = 23.6739 Rp
    assert float(preview.state) == 0.23674
    assert preview.attributes["price_high"] == 0.23674
    assert "current_price_high" not in preview.attributes
    assert "change_high_percent" not in preview.attributes


async def test_the_new_tariffs_take_over_at_new_year(hass: HomeAssistant, freezer, operator_host) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to("2026-12-31 23:30:00+01:00")
    entry = await setup_entry(hass, automatic_entry())
    assert value_of(hass, PRICE) == 0.18377
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_year").state == "2026"
    assert value_of(hass, PREVIEW) == 0.20647

    await travel(hass, freezer, "2027-01-01 00:00:05+01:00")
    # the file is read again and the 2027 set selected: 10 + 7 + 0.6 + 1.5 = 19.1 Rp, x 1.081
    assert operator_host.fetched == [TARIFF_URL, TARIFF_URL]
    assert entry.runtime_data.data["tariff_period_end"] == "2027-12-31"
    assert value_of(hass, PRICE) == 0.20647
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_year").state == "2027"
    assert state_of(hass, PREVIEW).state == STATE_UNAVAILABLE


async def test_a_product_that_leaves_the_file_turns_unavailable_and_a_new_one_appears(
    hass: HomeAssistant, wednesday_morning, operator_host
) -> None:
    entry = await setup_entry(hass, automatic_entry())
    renamed = {**ENERGY, "tariffName": "Musterwerk Basic"}
    cantonal_only = {**CANTONAL, "prices": {"cantonalTaxes": CANTONAL["prices"]["cantonalTaxes"][:1]}}
    operator_host.files[TARIFF_URL] = sample_file(renamed, GRID, cantonal_only)
    await refresh(hass, entry)

    for gone in (
        f"sensor.{PREFIX}_energy_basic_energie",
        f"sensor.{PREFIX}_energy_basic_energie_high",
        f"sensor.{PREFIX}_energy_basic_energie_base_fee",
        f"sensor.{PREFIX}_grid_doppel_netz",
        f"sensor.{PREFIX}_metering_messung_standard",
        f"sensor.{PREFIX}_municipality_levy_musterwil",
        PRICE,
        PRICE_HIGH,
        PREVIEW,
        LEVEL,
        f"binary_sensor.{PREFIX}_high_tariff",
        f"calendar.{PREFIX}_tariff",
    ):
        assert state_of(hass, gone).state == STATE_UNAVAILABLE, gone
    # what the file still has, and what it has now, is there without a reload
    assert value_of(hass, f"sensor.{PREFIX}_grid_basic_netz") == 0.08
    assert value_of(hass, f"sensor.{PREFIX}_energy_musterwerk_basic") == 0.12
    assert value_of(hass, f"sensor.{PREFIX}_energy_musterwerk_basic_low") == 0.09
    assert value_of(hass, f"sensor.{PREFIX}_cantonal_levy_vd_emolument_cantonal") == 0.2
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_host").state == "example.invalid"

    operator_host.files[TARIFF_URL] = sample_file()
    await refresh(hass, entry)
    assert value_of(hass, PRICE) == 0.25944
    assert state_of(hass, f"sensor.{PREFIX}_cantonal_levy_vd_emolument_cantonal").state == STATE_UNAVAILABLE


async def test_products_that_differ_only_in_signs_get_ids_of_their_own(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    under = {**GRID, "tariffName": "Benutzungsdauer <= 3500h", "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.07}]}}
    over = {**GRID, "tariffName": "Benutzungsdauer > 3500h", "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.05}]}}
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, FEES, over, under)
    await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    by_name = {name_of(hass, state.entity_id): state for state in hass.states.async_all("sensor")}
    # the first name in sorted order keeps the plain slug, the second gets a hash of its name appended
    assert by_name["Benutzungsdauer <= 3500h"].entity_id == f"sensor.{PREFIX}_grid_benutzungsdauer_3500h"
    assert by_name["Benutzungsdauer > 3500h"].entity_id.startswith(f"sensor.{PREFIX}_grid_benutzungsdauer_3500h_")
    assert float(by_name["Benutzungsdauer <= 3500h"].state) == 0.07
    assert float(by_name["Benutzungsdauer > 3500h"].state) == 0.05


async def test_chosen_cantonal_levies_are_part_of_the_price(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    entry = automatic_entry(
        {CONF_CANTONAL_LEVIES: [EMOLUMENT, TAXE, GENEVA], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [TAXE]},
        options={CONF_VAT_EXEMPT_LEVIES_RP: 0.3},
    )
    await setup_entry(hass, entry)
    state = state_of(hass, PRICE)
    # 12 + 10 + 0.5 + 1.5 + cantonal with VAT (0.2 + 1.2 high) = 25.4 Rp, x 1.081 = 27.4574 Rp,
    # + 0.6 Rp cantonal tax without VAT + 0.3 Rp exempt amount = 28.3574 Rp
    assert float(state.state) == 0.28357
    assert state.attributes["calculation"] == (
        "12.00 Rp energy (Basic Energie) + 10.00 Rp grid (Doppel Netz) + 0.50 Rp municipality levy (Musterwil)"
        " + 1.50 Rp national levies + 1.400 Rp cantonal levies = 25.40 Rp, × 1.081 (8.1% VAT)"
        " = 27.457 Rp + 0.900 Rp levies exempt from VAT = 28.36 Rp/kWh = 0.2836 CHF/kWh"
    )
    assert state.attributes["cantonal_levies"] == [EMOLUMENT, TAXE, GENEVA]
    assert state.attributes["cantonal_levies_vat_exempt"] == [TAXE]
    assert state.attributes["cantonal_levies_rp_kwh"] == 1.4
    assert state.attributes["cantonal_levies_vat_exempt_rp_kwh"] == 0.6
    assert state.attributes["vat_exempt_levies_rp_kwh"] == 0.3
    # low: 9 + 6 + 0.5 + 1.5 + 0.2 + 0.8 = 18 Rp, x 1.081 = 19.458 Rp, + 0.9 Rp = 20.358 Rp
    assert state.attributes["next_price"] == 0.20358
    assert value_of(hass, PRICE_HIGH) == 0.28357
    assert value_of(hass, PRICE_LOW) == 0.20358

    await travel(hass, wednesday_morning, "2026-11-18 22:00:01+01:00")
    assert value_of(hass, PRICE) == 0.20358
    assert value_of(hass, f"sensor.{PREFIX}_cantonal_levy_ge_kantonale_abgaben") == 0.8


async def test_cantonal_levy_sensors_show_what_the_file_lists(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    entry = await setup_entry(hass, automatic_entry({CONF_CANTONAL_LEVIES: [TAXE, GENEVA], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [TAXE]}))
    assert list(entry.runtime_data.data["cantonal"]) == [EMOLUMENT, TAXE, GENEVA, LUMP_SUM, REDEVANCE]

    taxe = state_of(hass, f"sensor.{PREFIX}_cantonal_levy_vd_taxe_cantonale")
    assert float(taxe.state) == 0.6
    assert taxe.attributes["unit_of_measurement"] == "Rp/kWh"
    assert name_of(hass, taxe.entity_id) == "Cantonal Levy VD – Taxe cantonale"
    assert taxe.attributes["canton"] == "VD"
    assert taxe.attributes["comment"] == "Taxe cantonale"
    assert taxe.attributes["tariff_name"] == "Kantonale Abgaben"
    assert taxe.attributes["included_in_price"] is True
    assert taxe.attributes["vat_exempt"] is True
    assert taxe.attributes["high_rp_kwh"] is None

    geneva = state_of(hass, f"sensor.{PREFIX}_cantonal_levy_ge_kantonale_abgaben")
    assert float(geneva.state) == 1.2
    assert geneva.attributes["high_rp_kwh"] == 1.2 and geneva.attributes["low_rp_kwh"] == 0.8
    assert geneva.attributes["comment"] is None
    assert geneva.attributes["vat_exempt"] is False
    assert len(geneva.attributes["schedule"]) == 2

    # with more than four entries in the file only the chosen ones start enabled
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_cantonal_levy_vd_emolument_cantonal") is False
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_cantonal_levy_sg_pauschale") is False
    assert _enabled_by_default(hass, f"sensor.{PREFIX}_cantonal_levy_ne_redevance") is False
    # a lump sum has no rate per kWh; 70 is read as 0.70 Rp
    assert entry.runtime_data.data["cantonal"][LUMP_SUM]["base_price_chf_month"] == 1.5
    assert entry.runtime_data.data["cantonal"][LUMP_SUM]["windows"][0]["rp_kwh"] == 0.0
    assert entry.runtime_data.data["cantonal"][REDEVANCE]["windows"][0]["rp_kwh"] == 0.7
    # the nameless municipality entry of the record is no municipality
    assert sorted(entry.runtime_data.data["municipalities"]) == ["Beispielingen", "Musterwil"]


async def test_a_handful_of_cantonal_levies_all_start_enabled(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    few = {**CANTONAL, "prices": {"cantonalTaxes": CANTONAL["prices"]["cantonalTaxes"][:2]}}
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, few)
    await setup_entry(hass, automatic_entry())
    emolument = state_of(hass, f"sensor.{PREFIX}_cantonal_levy_vd_emolument_cantonal")
    assert float(emolument.state) == 0.2
    assert emolument.attributes["included_in_price"] is False
    assert value_of(hass, PRICE) == 0.25944  # shown, not added unasked


async def test_the_preview_takes_the_cantonal_levies_of_its_own_period(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    raised = next_year(CANTONAL, {"cantonalTaxes": [{"cantonName": "VD", "cantonComment": "Taxe cantonale", "cantonEnergy": [{"from": "00:00", "to": "00:00", "price": 0.008}]}]})
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL, next_year(ENERGY), next_year(GRID), raised)
    entry = automatic_entry({CONF_GRID_PRODUCT: "Basic Netz", CONF_CANTONAL_LEVIES: [TAXE], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [TAXE]})
    await setup_entry(hass, entry)
    # today: 12 + 8 + 0.5 + 1.5 = 22 Rp, x 1.081 = 23.782 Rp, + 0.6 Rp tax = 24.382 Rp
    assert value_of(hass, PRICE) == 0.24382
    # 2027: the same rates and levy, the tax at 0.8 Rp = 24.582 Rp
    assert value_of(hass, PREVIEW) == 0.24582


async def test_without_a_municipality_section_the_levy_product_is_added(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, LEVY)
    entry = automatic_entry({CONF_MUNICIPALITY: "", CONF_GRID_PRODUCT: "Basic Netz"})
    await setup_entry(hass, entry)
    state = state_of(hass, PRICE)
    # 12 + 8 + 0.6 (levy published as a product) + 1.5 = 22.1 Rp, x 1.081 = 23.8901 Rp
    assert float(state.state) == 0.2389
    assert state.attributes["municipality"] == "EW Abgabe an die Gemeinde"
    assert "0.60 Rp municipality levy (EW Abgabe an die Gemeinde)" in state.attributes["calculation"]
    device = device_of(hass, entry)
    assert device.name == "Musterwerk AG Electricity Prices (Basic Energie)"


async def test_an_entry_without_products_shows_the_file_but_no_combined_price(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    """An entry from the first version whose tariff choice matched nothing."""
    entry = automatic_entry({CONF_ENERGY_PRODUCT: None, CONF_GRID_PRODUCT: None, CONF_MUNICIPALITY: None}, unique_id=None)
    await setup_entry(hass, entry)
    assert value_of(hass, f"sensor.{PREFIX}_energy_basic_energie") == 0.12
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_year").state == "2026"
    registry = er.async_get(hass)
    for absent in (PRICE, PRICE_HIGH, PREVIEW, LEVEL, f"binary_sensor.{PREFIX}_high_tariff", f"calendar.{PREFIX}_tariff", f"number.{PREFIX}_vat_percent"):
        assert registry.async_get(absent) is None, absent
    # without a chosen municipality or product every levy and base fee starts enabled
    assert value_of(hass, f"sensor.{PREFIX}_municipality_levy_beispielingen") == 1.0
    assert value_of(hass, f"sensor.{PREFIX}_grid_basic_netz_base_fee") == 9.0
    device = device_of(hass, entry)
    assert device.name == "Musterwerk AG Electricity Prices (manual)"
