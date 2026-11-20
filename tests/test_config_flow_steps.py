"""The setup steps beyond the plain case: narrowing the operator search, a
tariff link of one's own, the fall-back to next year's registration, the
manual rates form, an operator file without municipalities, the cantonal
levies step, and a reconfigure of an entry that is running."""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.elcom_tariffs.const import (
    CONF_CANTONAL_LEVIES,
    CONF_CANTONAL_LEVIES_VAT_EXEMPT,
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_GRID_FEE_CHF,
    CONF_MANUAL_HIGH_FROM,
    CONF_MANUAL_HIGH_RP,
    CONF_MANUAL_HIGH_TO,
    CONF_MANUAL_HIGH_WEEKDAYS,
    CONF_MANUAL_LOW_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
    DOMAIN,
)

from .common import (
    CANTONAL,
    EMOLUMENT,
    GENEVA,
    LUMP_SUM,
    MANUAL_PREFIX,
    NEXT_YEAR_URL,
    OPERATOR_ID,
    PREFIX,
    REDEVANCE,
    TARIFF_URL,
    TARIFFS_2026,
    TAXE,
    automatic_entry,
    manual_entry,
    sample_file,
    setup_entry,
    value_of,
)
from .test_levy_product import LEVY
from .test_tariffs import ENERGY, FEES, GRID

OWN_URL = "https://example.invalid/own/tarife.json"
LOCATION = {CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Doppel Netz", CONF_GRID_INCLUDES_LEVIES: True}
RATES = {CONF_MANUAL_HIGH_RP: 25.0, CONF_MANUAL_LOW_RP: 18.0, CONF_MANUAL_HIGH_FROM: "07:00", CONF_MANUAL_HIGH_TO: "20:00"}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


async def _step(hass: HomeAssistant, result, user_input: dict):
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def _pick(hass: HomeAssistant, result, manual_url: str = ""):
    return await _step(hass, result, {"operator_search": "Muster", CONF_OPERATOR_ID: OPERATOR_ID, "manual_url": manual_url})


def _labels(result, field: str) -> list[str]:
    """The labels of a selection field as the form shows them."""
    selector = next(value for key, value in result["data_schema"].schema.items() if str(key) == field)
    return [option["label"] for option in selector.config["options"]]


def _fields(result) -> list[str]:
    return [str(key) for key in result["data_schema"].schema]


async def test_the_search_narrows_the_list_of_operators(hass: HomeAssistant, operator_host) -> None:
    result = await _start(hass)
    assert _labels(result, CONF_OPERATOR_ID) == ["Musterwerk AG (direct link)", "Anderes Werk (auto-detect, may not work)"]
    assert operator_host.verified == [["Musterwerk AG", "Anderes Werk"]]

    result = await _step(hass, result, {"operator_search": "  ander "})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"count": "1"}
    assert _labels(result, CONF_OPERATOR_ID) == ["Anderes Werk (auto-detect, may not work)"]

    result = await _step(hass, result, {"operator_search": ""})
    assert result["description_placeholders"] == {"count": "2"}
    # every operator's link is checked once per flow, not again for each search
    assert operator_host.verified == [["Musterwerk AG", "Anderes Werk"]]

    result = await _step(hass, result, {"operator_search": "Atlantis"})
    assert result["errors"] == {"base": "no_matches"}
    assert _fields(result) == ["operator_search"]


async def test_an_empty_directory_ends_the_setup(hass: HomeAssistant, operator_host) -> None:
    operator_host.operators = []
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_data"


async def test_a_link_of_ones_own_needs_an_operator(hass: HomeAssistant, operator_host) -> None:
    result = await _start(hass)
    result = await _step(hass, result, {"operator_search": "Muster", "manual_url": OWN_URL})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    assert result["errors"] == {"base": "manual_url_needs_operator"}


async def test_a_link_of_ones_own_is_used_and_marked(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files = {OWN_URL: sample_file()}
    result = await _start(hass)
    result = await _pick(hass, result, f" {OWN_URL} ")
    assert result["step_id"] == "location"
    result = await _step(hass, result, LOCATION)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARIFF_URL] == OWN_URL
    assert result["data"][CONF_URL_IS_MANUAL] is True
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED
    assert operator_host.lookups == []  # the registration is not consulted for a link of one's own


async def test_a_link_of_ones_own_that_fails_leads_to_the_manual_rates(hass: HomeAssistant, operator_host) -> None:
    operator_host.registered[2027] = NEXT_YEAR_URL
    operator_host.files[NEXT_YEAR_URL] = sample_file()
    result = await _start(hass)
    result = await _pick(hass, result, OWN_URL)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    assert result["description_placeholders"] == {"operator": "Musterwerk AG"}
    assert operator_host.lookups == []  # next year's registration is no substitute for the link the user gave


async def test_a_dead_registration_falls_back_to_next_years(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files = {NEXT_YEAR_URL: sample_file()}
    operator_host.registered[2027] = NEXT_YEAR_URL
    result = await _start(hass)
    result = await _pick(hass, result)
    assert result["step_id"] == "location"
    assert operator_host.lookups == [2027]
    result = await _step(hass, result, LOCATION)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TARIFF_URL] == NEXT_YEAR_URL
    assert result["data"][CONF_URL_IS_MANUAL] is False


async def test_a_file_without_grid_products_leads_to_the_manual_rates(hass: HomeAssistant, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, FEES)
    result = await _start(hass)
    result = await _pick(hass, result)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"


async def test_manual_rates_are_checked_and_stored(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files = {}
    result = await _start(hass)
    result = await _pick(hass, result)
    assert result["step_id"] == "manual"
    assert _labels(result, CONF_MANUAL_HIGH_WEEKDAYS) == ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    for wrong in ("25:00", "7 Uhr", "07:60"):
        result = await _step(hass, result, {**RATES, CONF_MANUAL_HIGH_FROM: wrong})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
        assert result["errors"] == {"base": "invalid_time"}, wrong
    result = await _step(hass, result, {**RATES, CONF_MANUAL_HIGH_TO: "late"})
    assert result["errors"] == {"base": "invalid_time"}

    result = await _step(hass, result, {**RATES, CONF_MANUAL_HIGH_WEEKDAYS: ["Mo", "Tu", "We", "Th", "Fr", "Sa"], CONF_MANUAL_GRID_FEE_CHF: 9.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Musterwerk AG Electricity Prices (manual)"
    assert result["data"] == {
        CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: "Musterwerk AG",
        CONF_MANUAL_HIGH_RP: 25.0, CONF_MANUAL_LOW_RP: 18.0, CONF_MANUAL_HIGH_FROM: "07:00", CONF_MANUAL_HIGH_TO: "20:00",
        CONF_MANUAL_HIGH_WEEKDAYS: ["Mo", "Tu", "We", "Th", "Fr", "Sa"], CONF_MANUAL_GRID_FEE_CHF: 9.5,
    }
    assert result["options"] == {CONF_NATIONAL_LEVIES_RP: 3.03, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0}
    assert result["result"].unique_id == f"{OPERATOR_ID}_manual"
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED
    states = [state for state in hass.states.async_all("sensor") if state.entity_id.endswith("_price_current")]
    # Wednesday 10:00 inside 07:00-20:00: 25 + 3.03 = 28.03 Rp, x 1.081 = 30.30043 Rp
    assert [float(state.state) for state in states] == [0.303]


async def test_a_single_manual_rate_needs_nothing_but_the_rate(hass: HomeAssistant, operator_host) -> None:
    operator_host.files = {}
    result = await _start(hass)
    result = await _pick(hass, result)
    result = await _step(hass, result, {CONF_MANUAL_HIGH_RP: 27.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MANUAL_LOW_RP] is None
    assert result["data"][CONF_MANUAL_GRID_FEE_CHF] is None
    assert result["data"][CONF_MANUAL_HIGH_FROM] == "06:00" and result["data"][CONF_MANUAL_HIGH_TO] == "22:00"
    assert result["data"][CONF_MANUAL_HIGH_WEEKDAYS] == ["Mo", "Tu", "We", "Th", "Fr"]


async def test_manual_rates_for_one_operator_are_set_up_once(hass: HomeAssistant, operator_host) -> None:
    await setup_entry(hass, manual_entry())
    operator_host.files = {}
    result = await _start(hass)
    result = await _pick(hass, result)
    result = await _step(hass, result, RATES)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_a_file_without_municipalities_asks_for_the_products_only(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, LEVY)
    result = await _start(hass)
    result = await _pick(hass, result)
    assert result["step_id"] == "location"
    assert _fields(result) == [CONF_ENERGY_PRODUCT, CONF_GRID_PRODUCT, CONF_GRID_INCLUDES_LEVIES]
    result = await _step(hass, result, {CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_GRID_INCLUDES_LEVIES: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Musterwerk AG Electricity Prices (Basic Energie)"
    assert result["data"][CONF_MUNICIPALITY] == ""
    assert result["options"][CONF_NATIONAL_LEVIES_RP] == 3.03
    assert result["result"].unique_id == f"{OPERATOR_ID}__Basic Energie_Basic Netz"
    await hass.async_block_till_done()
    price = next(state for state in hass.states.async_all("sensor") if state.entity_id.endswith("_price_current"))
    # 12 + 8 + 0.6 (levy published as a product) + 3.03 = 23.63 Rp, x 1.081 = 25.54403 Rp
    assert float(price.state) == 0.25544


async def test_cantonal_levies_are_offered_with_their_rates_and_none_preselected(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    result = await _start(hass)
    result = await _pick(hass, result)
    result = await _step(hass, result, LOCATION)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "cantonal"
    assert result["description_placeholders"] == {"operator": "Musterwerk AG", "count": "5"}
    assert _labels(result, CONF_CANTONAL_LEVIES) == [
        f"{EMOLUMENT} (0.2 Rp/kWh)",
        f"{TAXE} (0.6 Rp/kWh)",
        f"{GENEVA} (1.2 / 0.8 Rp/kWh)",
        f"{LUMP_SUM} (1.5 CHF/M)",
        f"{REDEVANCE} (0.7 Rp/kWh)",
    ]
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == {CONF_CANTONAL_LEVIES: [], CONF_CANTONAL_LEVIES_VAT_EXEMPT: []}

    # a levy ticked as exempt from VAT but not as part of the bill is not kept
    result = await _step(hass, result, {CONF_CANTONAL_LEVIES: [TAXE, GENEVA], CONF_CANTONAL_LEVIES_VAT_EXEMPT: [TAXE, EMOLUMENT]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CANTONAL_LEVIES] == [TAXE, GENEVA]
    assert result["data"][CONF_CANTONAL_LEVIES_VAT_EXEMPT] == [TAXE]
    await hass.async_block_till_done()
    price = next(state for state in hass.states.async_all("sensor") if state.entity_id.endswith("_price_current"))
    # 12 + 10 + 0.5 + 0 (national levies in the grid price) + 1.2 = 23.7 Rp, x 1.081 = 25.6197 Rp, + 0.6 Rp = 26.2197 Rp
    assert float(price.state) == 0.2622


async def test_no_cantonal_levy_has_to_be_chosen(hass: HomeAssistant, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    result = await _start(hass)
    result = await _pick(hass, result)
    result = await _step(hass, result, LOCATION)
    result = await _step(hass, result, {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CANTONAL_LEVIES] == []
    assert result["data"][CONF_CANTONAL_LEVIES_VAT_EXEMPT] == []


async def test_reconfigure_to_another_product_drops_the_entities_it_no_longer_has(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    registry = er.async_get(hass)
    registry.async_update_entity(f"sensor.{PREFIX}_price_current", new_entity_id="sensor.my_electricity_price")
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    result = await _pick(hass, result)
    assert result["step_id"] == "location"
    result = await _step(hass, result, {**LOCATION, CONF_ENERGY_PRODUCT: "Green Energie"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == f"{OPERATOR_ID}_Musterwil_Green Energie_Doppel Netz"
    assert entry.title == "Musterwerk AG Electricity Prices (Musterwil, Green Energie)"
    assert entry.options[CONF_NATIONAL_LEVIES_RP] == 1.5  # the user's options stay
    # an entity that comes back keeps the id the user gave it: 14 + 10 + 0.5 + 1.5 = 26 Rp, x 1.081 = 28.106 Rp
    assert value_of(hass, "sensor.my_electricity_price") == 0.28106
    # the flat product has no high tariff: its binary sensor and calendar are gone, not left behind unavailable
    assert registry.async_get(f"binary_sensor.{PREFIX}_high_tariff") is None
    assert registry.async_get(f"calendar.{PREFIX}_tariff") is None
    assert hass.states.get(f"binary_sensor.{PREFIX}_high_tariff") is None


async def test_reconfigure_with_the_same_selection_keeps_the_entities(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    registry = er.async_get(hass)
    registry.async_update_entity(f"sensor.{PREFIX}_price_current", new_entity_id="sensor.my_electricity_price")
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    location = await _pick(hass, result)
    # the form starts from what the operator's file lists first, the entry's own values are chosen again
    result = await _step(hass, location, LOCATION)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert value_of(hass, "sensor.my_electricity_price") == 0.25944


async def test_reconfigure_cannot_take_the_selection_of_another_entry(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Green Energie"}, entry_id="01JELCOMTESTENTRY000000EFGH"))
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    result = await _pick(hass, result)
    result = await _step(hass, result, {**LOCATION, CONF_ENERGY_PRODUCT: "Green Energie"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert entry.data[CONF_ENERGY_PRODUCT] == "Basic Energie"


async def test_a_manual_entry_is_reconfigured_in_place(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry())
    operator_host.files = {}
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id})
    result = await _pick(hass, result)
    assert result["step_id"] == "manual"
    result = await _step(hass, result, {**RATES, CONF_MANUAL_HIGH_RP: 30.0})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    assert entry.data[CONF_MANUAL_HIGH_RP] == 30.0
    # 30 + 3 = 33 Rp, x 1.081 = 35.673 Rp
    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_current") == 0.35673


async def test_a_product_listed_twice_is_offered_once(hass: HomeAssistant, operator_host) -> None:
    nameless = {**ENERGY, "tariffName": ""}
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, {**ENERGY, "customerType": "H8"}, nameless, GRID, FEES)
    result = await _start(hass)
    result = await _pick(hass, result)
    assert result["step_id"] == "location"
    offered = {str(key): value for key, value in result["data_schema"].schema.items()}
    assert offered[CONF_ENERGY_PRODUCT].container == ["Basic Energie"]
    assert offered[CONF_GRID_PRODUCT].container == ["Basic Netz"]
    assert offered[CONF_MUNICIPALITY].container == ["Beispielingen", "Musterwil"]
