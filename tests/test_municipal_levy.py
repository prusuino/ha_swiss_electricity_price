"""Municipal levies a tariff file lists more than once for a municipality:
which record applies (the grid product's customer type, the customer's own
choice, several added up), the setup step and the Configure field that ask
for it, and the repair issue for an entry where nothing settles it."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elcom_tariffs.const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPAL_LEVIES,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
    DOMAIN,
)
from custom_components.elcom_tariffs.coordinator import ElcomTariffCoordinator, levy_issue_id
from custom_components.elcom_tariffs.municipal_levy import (
    assign_keys,
    levy_attributes,
    needs_choice,
    offers_choice,
    record_labels,
    resolve,
)

LOW_VOLTAGE = "bis 100'000 kWh pro Jahr (0.4kV)"
LOW_VOLTAGE_LARGE = ">100'000 kWh (0.4kV)"
MEDIUM_VOLTAGE = "16kV"


def _energy(name: str) -> dict:
    return {"tariffType": "electricity", "tariffName": name, "startDate": "2020-01-01",
            "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.12}], "base": {"price": 5.0}}}


def _grid(name: str, customer_type: str | None) -> dict:
    return {"tariffType": "grid", "tariffName": name, "customerType": customer_type, "startDate": "2020-01-01",
            "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.08}], "base": {"price": 9.0}}}


def _fee(name: str, customer_type: str | None, *entries: tuple) -> dict:
    """A regional_fees record; every entry is (municipality, price[, comment])."""
    taxes = []
    for municipality, price, *comment in entries:
        tax = {"municipalityName": municipality, "municipalityEnergy": [{"price": price}], "municipalityBase": {"price": 0}}
        if comment:
            tax["municipalityComment"] = comment[0]
        taxes.append(tax)
    return {"tariffType": "regional_fees", "tariffName": name, "customerType": customer_type, "startDate": "2020-01-01",
            "prices": {"municipalityTaxes": taxes}}


# One levy record per grid product, tied to it by the customer type alone
# and with the medium-voltage one last in the file.
PER_PRODUCT = [
    _energy("Basic Energie"),
    _grid("NS", LOW_VOLTAGE),
    _grid("NS profi", LOW_VOLTAGE_LARGE),
    _grid("MS", MEDIUM_VOLTAGE),
    _grid("Sonder", "Sondervertrag"),
    # Spacing and case of the customer type differ from the grid product.
    _fee("Gemeindeabgabe NS", "Bis 100'000 kWh  pro Jahr (0.4kV)", ("Musterwil", 0.0175), ("Beispielingen", 0.02)),
    _fee("Gemeindeabgabe NS profi", LOW_VOLTAGE_LARGE, ("Musterwil", 0.0125), ("Beispielingen", 0.02)),
    _fee("Gemeindeabgabe MS", MEDIUM_VOLTAGE, ("Musterwil", 0.008), ("Beispielingen", 0.02)),
]
RAW = {"tariffs": PER_PRODUCT}


def _parse(tariffs: list[dict], **kwargs) -> dict:
    return ElcomTariffCoordinator._parse_tariff_records(tariffs, **kwargs)["municipalities"]


def test_a_single_record_is_the_levy():
    levy = _parse([_energy("E"), _grid("N", "H4"), _fee("Gemeindeabgaben", None, ("Musterwil", 0.005))])["Musterwil"]
    assert levy["rp_kwh"] == 0.5
    assert levy["resolved_by"] == "single_record"
    assert levy["applied"] == ["Gemeindeabgaben"]
    assert not needs_choice(levy) and not offers_choice(levy)


@pytest.mark.parametrize(("grid_product", "rp_kwh", "record"), [
    ("NS", 1.75, "Gemeindeabgabe NS"),
    ("NS profi", 1.25, "Gemeindeabgabe NS profi"),
    ("MS", 0.8, "Gemeindeabgabe MS"),
])
def test_the_record_of_the_grid_product_applies(grid_product, rp_kwh, record):
    levy = _parse(PER_PRODUCT, grid_product=grid_product)["Musterwil"]
    assert levy["rp_kwh"] == rp_kwh
    assert levy["resolved_by"] == "customer_type"
    assert levy["applied"] == [record]
    assert not needs_choice(levy) and offers_choice(levy)


@pytest.mark.parametrize("grid_product", [None, "Sonder", "No such product"])
def test_without_a_matching_record_the_last_one_stays(grid_product):
    """What the integration showed before it knew about the other records."""
    levy = _parse(PER_PRODUCT, grid_product=grid_product)["Musterwil"]
    assert levy["rp_kwh"] == 0.8
    assert levy["resolved_by"] == "unresolved"
    assert needs_choice(levy)


def test_records_with_one_rate_are_no_choice():
    levy = _parse(PER_PRODUCT, grid_product="Sonder")["Beispielingen"]
    assert levy["rp_kwh"] == 2.0
    assert levy["resolved_by"] == "single_record"
    assert not needs_choice(levy) and not offers_choice(levy)


def test_several_matching_records_with_one_rate_resolve():
    tariffs = [
        _grid("N", "Haushalt"),
        _fee("Abgabe A", "Haushalt", ("Musterwil", 0.0052)),
        _fee("Abgabe B", "haushalt", ("Musterwil", 0.0052)),
        _fee("Abgabe C", "Gewerbe", ("Musterwil", 0.003)),
    ]
    levy = _parse(tariffs, grid_product="N")["Musterwil"]
    assert levy["rp_kwh"] == 0.52 and levy["resolved_by"] == "customer_type"


def test_the_chosen_records_add_up_for_the_entry_municipality_only():
    chosen = ["Gemeindeabgabe NS", "Gemeindeabgabe MS"]
    parsed = _parse(PER_PRODUCT, grid_product="Sonder", municipality="Musterwil", municipal_levies=chosen)
    levy = parsed["Musterwil"]
    assert levy["rp_kwh"] == 2.55
    assert levy["resolved_by"] == "selection" and levy["applied"] == chosen and levy["missing"] == []
    assert not needs_choice(levy)
    assert parsed["Beispielingen"]["resolved_by"] == "single_record"


def test_a_choice_wins_over_the_grid_product():
    levy = _parse(PER_PRODUCT, grid_product="NS", municipality="Musterwil", municipal_levies=["Gemeindeabgabe MS"])["Musterwil"]
    assert levy["rp_kwh"] == 0.8 and levy["resolved_by"] == "selection"


def test_a_chosen_record_that_left_the_file_asks_again():
    partly = _parse(PER_PRODUCT, grid_product="Sonder", municipality="Musterwil",
                    municipal_levies=["Gemeindeabgabe NS", "Gemeindeabgabe 2025"])["Musterwil"]
    assert partly["rp_kwh"] == 1.75 and partly["missing"] == ["Gemeindeabgabe 2025"]
    assert needs_choice(partly)
    gone = _parse(PER_PRODUCT, grid_product="NS", municipality="Musterwil", municipal_levies=["Gemeindeabgabe 2025"])["Musterwil"]
    # Back to the record of the grid product, but the customer is told.
    assert gone["rp_kwh"] == 1.75 and gone["resolved_by"] == "customer_type"
    assert needs_choice(gone)


def test_entries_of_one_record_are_told_apart_by_their_comment():
    fee = _fee("Taxes communales", "Tous", ("Pully", 0.007, "Usage du sol"), ("Pully", 0.0103, "Eclairage public"), ("Pully", 0.004))
    levy = _parse([fee], municipality="Pully", municipal_levies=["Taxes communales | Usage du sol", "Taxes communales"])["Pully"]
    assert [r["key"] for r in levy["records"]] == [
        "Taxes communales | Usage du sol", "Taxes communales | Eclairage public", "Taxes communales",
    ]
    assert levy["rp_kwh"] == 1.1
    assert levy["comment"] == "Usage du sol"
    assert levy_attributes(levy)["tariff_record"] == "Taxes communales"


def test_repeated_titles_without_a_comment_are_numbered():
    records = [{"tariff_name": "Abgabe", "rp_kwh": 1.0}, {"tariff_name": "Abgabe", "rp_kwh": 2.0}, {"tariff_name": None, "rp_kwh": 3.0}]
    assign_keys(records)
    assert [r["key"] for r in records] == ["Abgabe", "Abgabe #2", "?"]
    assert resolve(records, None, ["Abgabe #2"])["rp_kwh"] == 2.0


def test_a_secondary_meter_levy_is_no_alternative():
    regular = _fee("Gemeindeabgaben", None, ("Musterwil", 0.005))
    secondary = _fee("Konzessionsabgabe für Zweitzähler", None, ("Musterwil", 0.001), ("Beispielingen", 0.001))
    parsed = _parse([regular, secondary])
    assert parsed["Musterwil"]["rp_kwh"] == 0.5 and len(parsed["Musterwil"]["records"]) == 1
    assert parsed["Beispielingen"]["rp_kwh"] == 0.1  # nothing but the secondary-meter levy


def test_labels_and_sensor_attributes():
    levy = _parse(PER_PRODUCT, grid_product="NS")["Musterwil"]
    labels = record_labels(levy["records"])
    assert labels["Gemeindeabgabe NS"] == "Gemeindeabgabe NS · Bis 100'000 kWh pro Jahr (0.4kV) · 1.75 Rp/kWh"
    attributes = levy_attributes(levy)
    assert attributes["tariff_record"] == "Gemeindeabgabe NS"
    assert attributes["resolved_by"] == "customer_type"
    assert [r["applied"] for r in attributes["records"]] == [True, False, False]
    single = _parse([_fee("Gemeindeabgaben", None, ("Musterwil", 0.005))])["Musterwil"]
    assert "records" not in levy_attributes(single)
    assert levy_attributes({"rp_kwh": 0.5}) == {}


# ------------------------------------------------------------ flows and entry

OPERATOR_ID = "https://energy.ld.admin.ch/elcom/electricityprice/operator/123"
OPERATORS = [
    {"id": OPERATOR_ID, "uid": "CHE-123.456.789", "name": "Musterwerk AG", "url": "https://example.invalid/tarife.json", "likely_direct": True},
]
FLOW = "custom_components.elcom_tariffs.config_flow."
COORDINATOR = "custom_components.elcom_tariffs.coordinator."


@contextmanager
def _network(raw: dict):
    with (
        patch(FLOW + "async_fetch_operator_directory", return_value=OPERATORS),
        patch(FLOW + "async_verify_direct_links", return_value=None),
        patch(FLOW + "async_resolve_tariff_url", side_effect=lambda hass, url: url),
        patch(FLOW + "async_fetch_raw_tariffs", return_value=raw),
        patch(COORDINATOR + "async_fetch_operator_url", return_value=None),
        patch(COORDINATOR + "async_resolve_tariff_url", side_effect=lambda hass, url: url),
        patch(COORDINATOR + "async_fetch_raw_tariffs", return_value=raw),
    ):
        yield


@pytest.fixture
def fake_network():
    with _network(RAW):
        yield


async def _to_location(hass: HomeAssistant, grid_product: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator_search": "Muster", CONF_OPERATOR_ID: OPERATOR_ID, "manual_url": ""}
    )
    assert result["step_id"] == "location"
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: grid_product, CONF_GRID_INCLUDES_LEVIES: True},
    )


def _entry(grid_product: str, **data) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, unique_id=f"{OPERATOR_ID}_Musterwil_Basic Energie_{grid_product}", title="Musterwerk", version=2,
        data={CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: "Musterwerk AG", CONF_TARIFF_URL: "https://example.invalid/tarife.json",
              CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: grid_product, **data},
        options={CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0},
    )


def _levy(entry) -> dict:
    return entry.runtime_data.data["municipalities"]["Musterwil"]


def _issue(hass: HomeAssistant, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, levy_issue_id(entry))


async def test_setup_does_not_ask_when_the_grid_product_tells(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    result = await _to_location(hass, "NS")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MUNICIPAL_LEVIES] == []
    await hass.async_block_till_done()
    entry = result["result"]
    assert _levy(entry)["rp_kwh"] == 1.75
    assert _issue(hass, entry) is None


async def test_setup_asks_when_nothing_tells(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    result = await _to_location(hass, "Sonder")
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "municipal"
    assert result["description_placeholders"] == {"operator": "Musterwerk AG", "municipality": "Musterwil", "count": "3"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MUNICIPAL_LEVIES: []})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "choose_levy"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_MUNICIPAL_LEVIES: ["Gemeindeabgabe NS profi"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MUNICIPAL_LEVIES] == ["Gemeindeabgabe NS profi"]
    await hass.async_block_till_done()
    entry = result["result"]
    assert _levy(entry)["rp_kwh"] == 1.25
    assert _issue(hass, entry) is None


async def test_existing_entry_gets_the_matching_record(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    """An entry from before the choice existed, whose grid product names the
    record: corrected without anyone doing anything."""
    entry = _entry("NS")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _levy(entry)["rp_kwh"] == 1.75
    assert _issue(hass, entry) is None
    states = [s for s in hass.states.async_all("sensor") if s.attributes.get("resolved_by")]
    assert len(states) == 1
    assert float(states[0].state) == 1.75
    assert states[0].attributes["tariff_record"] == "Gemeindeabgabe NS"
    assert states[0].attributes["customer_type"] == "Bis 100'000 kWh  pro Jahr (0.4kV)"
    assert len(states[0].attributes["records"]) == 3


async def test_ambiguous_entry_keeps_its_value_until_the_customer_chooses(
    hass: HomeAssistant, enable_custom_integrations, fake_network
) -> None:
    entry = _entry("Sonder")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _levy(entry)["rp_kwh"] == 0.8  # the last record of the file, as before
    issue = _issue(hass, entry)
    assert issue is not None and issue.translation_key == "municipal_levy_ambiguous"
    assert issue.translation_placeholders["municipality"] == "Musterwil"
    assert issue.translation_placeholders["applied"].startswith("Gemeindeabgabe MS")
    assert CONF_MUNICIPAL_LEVIES not in entry.data

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_MUNICIPAL_LEVIES in [str(key) for key in result["data_schema"].schema]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Sonder",
         CONF_MUNICIPAL_LEVIES: ["Gemeindeabgabe NS", "Gemeindeabgabe MS"],
         CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.data[CONF_MUNICIPAL_LEVIES] == ["Gemeindeabgabe NS", "Gemeindeabgabe MS"]
    assert entry.state is ConfigEntryState.LOADED
    assert _levy(entry)["rp_kwh"] == 2.55
    assert _issue(hass, entry) is None

    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_configure_leaves_an_entry_without_a_choice_alone(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    """Saving the dialog with nothing ticked must not write the key into an
    older entry (that would reload it for nothing)."""
    entry = _entry("NS")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "NS", CONF_MUNICIPAL_LEVIES: [],
         CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert CONF_MUNICIPAL_LEVIES not in entry.data
    assert entry.runtime_data is coordinator  # not reloaded


async def test_reconfigure_keeps_a_choice_on_offer(hass: HomeAssistant, enable_custom_integrations, fake_network) -> None:
    entry = _entry("NS", **{CONF_MUNICIPAL_LEVIES: ["Gemeindeabgabe MS"]})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"operator_search": "Muster", CONF_OPERATOR_ID: OPERATOR_ID, "manual_url": ""}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "NS", CONF_GRID_INCLUDES_LEVIES: True},
    )
    # The grid product would tell, but the entry carries its own choice.
    assert result["step_id"] == "municipal"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_MUNICIPAL_LEVIES: []})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_MUNICIPAL_LEVIES] == []
    # The reconfigure reloads the entry, so the new choice is in effect at once.
    assert entry.state is ConfigEntryState.LOADED
    assert _levy(entry)["rp_kwh"] == 1.75
    assert await hass.config_entries.async_unload(entry.entry_id)
