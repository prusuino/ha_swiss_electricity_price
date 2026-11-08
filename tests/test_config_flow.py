"""Config flow tests: operator search and product selection, duplicate
detection, the manual fallback when no tariff file works, an unreachable
operator directory and the reconfigure flow. Every network call is faked."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elcom_tariffs.const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_VAT_PERCENT,
    DOMAIN,
)

OPERATOR_ID = "https://energy.ld.admin.ch/elcom/electricityprice/operator/123"
OPERATORS = [
    {"id": OPERATOR_ID, "uid": "CHE-123.456.789", "name": "Musterwerk AG", "url": "https://example.invalid/tarife.json", "likely_direct": True},
    {"id": OPERATOR_ID + "9", "uid": "CHE-987.654.321", "name": "Anderes Werk", "url": "https://example.invalid/anderes", "likely_direct": False},
]
RAW = {"tariffs": [
    {"tariffType": "electricity", "tariffName": "Basic Energie", "startDate": "2020-01-01",
     "prices": {"energy": [{"from": "06:00", "to": "22:00", "weekdays": ["Mo", "Tu", "We", "Th", "Fr"], "price": 0.12}, {"from": "22:00", "to": "06:00", "price": 0.09}], "base": {"price": 5.0}}},
    {"tariffType": "electricity", "tariffName": "Green Energie", "startDate": "2020-01-01",
     "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.14}], "base": {"price": 5.0}}},
    {"tariffType": "grid", "tariffName": "Basic Netz", "startDate": "2020-01-01",
     "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.08}], "base": {"price": 9.0}}},
    {"tariffType": "regional_fees", "tariffName": "Gemeindeabgaben", "startDate": "2020-01-01",
     "prices": {"municipalityTaxes": [{"municipalityName": "Musterwil", "municipalityEnergy": [{"price": 0.005}], "municipalityBase": {"price": 0}}]}},
]}
FLOW = "custom_components.elcom_tariffs.config_flow."
UNIQUE_ID = f"{OPERATOR_ID}_Musterwil_Basic Energie_Basic Netz"


@pytest.fixture
def fake_network():
    with (
        patch(FLOW + "async_fetch_operator_directory", return_value=OPERATORS),
        patch(FLOW + "async_verify_direct_links", return_value=None),
        patch(FLOW + "async_resolve_tariff_url", side_effect=lambda hass, url: url),
        patch(FLOW + "async_fetch_raw_tariffs", return_value=RAW),
    ):
        yield


async def _pick_operator(hass, result):
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator_search": "Muster", CONF_OPERATOR_ID: OPERATOR_ID, "manual_url": ""}
    )


async def test_setup_with_operator_and_products(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "user"
    assert result["description_placeholders"] == {"count": "2"}
    result = await _pick_operator(hass, result)
    assert result["step_id"] == "location"
    assert result["description_placeholders"] == {"operator": "Musterwerk AG"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_GRID_INCLUDES_LEVIES: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OPERATOR_NAME] == "Musterwerk AG"
    assert result["data"][CONF_TARIFF_URL] == "https://example.invalid/tarife.json"
    assert result["data"][CONF_ENERGY_PRODUCT] == "Basic Energie"
    assert result["options"][CONF_NATIONAL_LEVIES_RP] == 0.0  # included in the grid tariff
    assert result["options"][CONF_VAT_PERCENT] == 8.1
    assert result["result"].unique_id == UNIQUE_ID


async def test_grid_without_levies_adds_the_federal_total(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await _pick_operator(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Green Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_GRID_INCLUDES_LEVIES: False},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_NATIONAL_LEVIES_RP] > 0


async def test_no_operator_matches_is_a_form_error(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"operator_search": "Atlantis"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_matches"}


async def test_same_selection_twice_is_refused(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID, title="Musterwerk", data={CONF_OPERATOR_ID: OPERATOR_ID}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await _pick_operator(hass, result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_GRID_INCLUDES_LEVIES: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_unusable_tariff_file_offers_manual_rates(hass: HomeAssistant, enable_custom_integrations) -> None:
    with (
        patch(FLOW + "async_fetch_operator_directory", return_value=OPERATORS),
        patch(FLOW + "async_verify_direct_links", return_value=None),
        patch(FLOW + "async_resolve_tariff_url", side_effect=OSError("no link")),
        patch(FLOW + "async_fetch_operator_url", return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await _pick_operator(hass, result)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"


async def test_unreachable_directory_aborts(hass: HomeAssistant, enable_custom_integrations) -> None:
    with patch(FLOW + "async_fetch_operator_directory", side_effect=OSError("down")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_reconfigure_switches_the_products(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=UNIQUE_ID, title="Musterwerk", version=2,
        data={CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: "Musterwerk AG", CONF_TARIFF_URL: "https://example.invalid/tarife.json",
              CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Basic Netz"},
        options={CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1},
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] is FlowResultType.FORM
    result = await _pick_operator(hass, result)
    assert result["step_id"] == "location"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Green Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_GRID_INCLUDES_LEVIES: True},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_ENERGY_PRODUCT] == "Green Energie"
    assert entry.unique_id == f"{OPERATOR_ID}_Musterwil_Green Energie_Basic Netz"
    assert entry.options[CONF_NATIONAL_LEVIES_RP] == 1.5  # the user's options stay
