"""The Configure dialog: the levy and VAT figures, and for an entry on the
operator's file the municipality, the products and the cantonal levies as
the file lists them today. A changed selection reloads the entry, changed
figures alone do not."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.elcom_tariffs.const import (
    CONF_CANTONAL_LEVIES,
    CONF_CANTONAL_LEVIES_VAT_EXEMPT,
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
)

from .common import (
    CANTONAL,
    EMOLUMENT,
    MANUAL_PREFIX,
    OPERATOR_ID,
    PREFIX,
    TARIFF_URL,
    TARIFFS_2026,
    TAXE,
    automatic_entry,
    manual_entry,
    sample_file,
    setup_entry,
    value_of,
)
from .test_tariffs import ENERGY, GRID

PRICE = f"sensor.{PREFIX}_price_current"
FIGURES = {CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0}
SELECTION = {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Doppel Netz"}


def _defaults(result) -> dict:
    """Every field of the form with the value it starts with."""
    return {str(key): key.default() for key in result["data_schema"].schema}


def _values(result, field: str) -> list[str]:
    selector = next(value for key, value in result["data_schema"].schema.items() if str(key) == field)
    return [option["value"] for option in selector.config["options"]]


async def _open(hass: HomeAssistant, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    return result


async def _save(hass: HomeAssistant, result, user_input: dict):
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


async def test_the_dialog_starts_with_the_entry_as_it_is(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    result = await _open(hass, entry)
    assert result["description_placeholders"] == {"operator": "Musterwerk AG"}
    assert _defaults(result) == {**SELECTION, **FIGURES}
    assert _values(result, CONF_MUNICIPALITY) == ["Beispielingen", "Musterwil"]
    assert _values(result, CONF_ENERGY_PRODUCT) == ["Basic Energie", "Green Energie"]
    assert _values(result, CONF_GRID_PRODUCT) == ["Basic Netz", "Doppel Netz"]


async def test_new_figures_reach_the_price_without_a_reload(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    coordinator = entry.runtime_data
    result = await _save(hass, await _open(hass, entry), {**SELECTION, CONF_NATIONAL_LEVIES_RP: 2.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.4})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_NATIONAL_LEVIES_RP: 2.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.4}
    assert entry.runtime_data is coordinator
    assert operator_host.fetched == [TARIFF_URL]
    # 12 + 10 + 0.5 + 2.5 = 25 Rp, x 1.081 = 27.025 Rp, + 0.4 Rp = 27.425 Rp
    assert value_of(hass, PRICE) == 0.27425
    assert value_of(hass, f"number.{PREFIX}_national_levies") == 2.5


async def test_another_product_renames_and_reloads_the_entry(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    coordinator = entry.runtime_data
    result = await _save(hass, await _open(hass, entry), {**SELECTION, CONF_ENERGY_PRODUCT: "Green Energie", **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_ENERGY_PRODUCT] == "Green Energie"
    assert entry.unique_id == f"{OPERATOR_ID}_Musterwil_Green Energie_Doppel Netz"
    assert entry.title == "Musterwerk AG Electricity Prices (Musterwil, Green Energie)"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
    # 14 + 10 + 0.5 + 1.5 = 26 Rp, x 1.081 = 28.106 Rp
    assert value_of(hass, PRICE) == 0.28106


async def test_another_municipality_brings_its_own_levy(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    await _save(hass, await _open(hass, entry), {**SELECTION, CONF_MUNICIPALITY: "Beispielingen", **FIGURES})
    assert entry.data[CONF_MUNICIPALITY] == "Beispielingen"
    assert entry.title == "Musterwerk AG Electricity Prices (Beispielingen, Basic Energie)"
    # 12 + 10 + 1.0 + 1.5 = 24.5 Rp, x 1.081 = 26.4845 Rp
    assert value_of(hass, PRICE) == pytest.approx(0.264845, abs=1e-5)


async def test_the_selection_of_another_entry_cannot_be_taken(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Green Energie"}, entry_id="01JELCOMTESTENTRY000000EFGH"))
    result = await _save(hass, await _open(hass, entry), {**SELECTION, CONF_ENERGY_PRODUCT: "Green Energie", **FIGURES, CONF_VAT_PERCENT: 7.7})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_configured"}
    assert entry.data[CONF_ENERGY_PRODUCT] == "Basic Energie"
    assert entry.options[CONF_VAT_PERCENT] == 8.1  # nothing of the refused input is kept


async def test_a_renamed_product_is_chosen_again_from_the_current_names(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    """The operator put its name in front of every product and dropped the municipality section."""
    operator_host.files[TARIFF_URL] = sample_file({**ENERGY, "tariffName": "MW Basic Energie"}, {**GRID, "tariffName": "MW Basic Netz"})
    entry = await setup_entry(hass, automatic_entry())
    assert hass.states.get(PRICE).state == "unavailable"

    result = await _open(hass, entry)
    # no municipality to choose, and the first current name stands in for the one that is gone
    assert _defaults(result) == {CONF_ENERGY_PRODUCT: "MW Basic Energie", CONF_GRID_PRODUCT: "MW Basic Netz", **FIGURES}
    result = await _save(hass, result, {CONF_ENERGY_PRODUCT: "MW Basic Energie", CONF_GRID_PRODUCT: "MW Basic Netz", **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_MUNICIPALITY] == "Musterwil"  # kept for the day the section comes back
    assert entry.unique_id == f"{OPERATOR_ID}_Musterwil_MW Basic Energie_MW Basic Netz"
    # 12 + 8 + 1.5 = 21.5 Rp, x 1.081 = 23.2415 Rp
    assert round(value_of(hass, PRICE), 4) == 0.2324


async def test_cantonal_levies_are_chosen_in_the_dialog(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    entry = await setup_entry(hass, automatic_entry({CONF_CANTONAL_LEVIES: [EMOLUMENT, "VD – Alte Taxe"], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [EMOLUMENT, TAXE]}))
    result = await _open(hass, entry)
    defaults = _defaults(result)
    # what the file no longer lists, or what is not part of the price, is not ticked
    assert defaults[CONF_CANTONAL_LEVIES] == [EMOLUMENT]
    assert defaults[CONF_CANTONAL_LEVIES_VAT_EXEMPT] == [EMOLUMENT]
    assert len(_values(result, CONF_CANTONAL_LEVIES)) == 5

    result = await _save(hass, result, {**SELECTION, CONF_CANTONAL_LEVIES: [EMOLUMENT, TAXE], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [TAXE], **FIGURES})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_CANTONAL_LEVIES] == [EMOLUMENT, TAXE]
    assert entry.data[CONF_CANTONAL_LEVIES_VAT_EXEMPT] == [TAXE]
    # 12 + 10 + 0.5 + 1.5 + 0.2 = 24.2 Rp, x 1.081 = 26.1602 Rp, + 0.6 Rp = 26.7602 Rp
    assert value_of(hass, PRICE) == 0.2676


async def test_a_manual_entry_has_the_figures_only(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry())
    result = await _open(hass, entry)
    assert _defaults(result) == {CONF_NATIONAL_LEVIES_RP: 3.0, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0}
    result = await _save(hass, result, {CONF_NATIONAL_LEVIES_RP: 0, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NATIONAL_LEVIES_RP] == 0.0
    # 25 Rp x 1.081 = 27.025 Rp
    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_current") == 0.27025


async def test_an_entry_that_is_not_loaded_has_the_figures_only(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = automatic_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options={})
    result = await _open(hass, entry)
    # without stored figures the form starts with the defaults
    assert _defaults(result) == {CONF_NATIONAL_LEVIES_RP: 3.03, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0}
    result = await _save(hass, result, {CONF_NATIONAL_LEVIES_RP: 0.0, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_NATIONAL_LEVIES_RP] == 0.0
    assert entry.data[CONF_ENERGY_PRODUCT] == "Basic Energie"
