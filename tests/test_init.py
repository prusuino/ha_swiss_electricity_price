"""Entry life cycle: setup and retry, migration of entries from the first
version, unloading, reloading and removing an entry with its repair
issues, and two entries of one operator side by side."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elcom_tariffs.const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPALITY,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    DOMAIN,
)
from custom_components.elcom_tariffs.coordinator import manual_issue_id, selection_issue_id

from .common import (
    ENTRY_ID,
    GRID_TWO_RATES,
    MANUAL_PREFIX,
    OPERATOR_ID,
    OPERATOR_NAME,
    PREFIX,
    TARIFF_URL,
    automatic_entry,
    manual_entry,
    refresh,
    sample_file,
    setup_entry,
    state_of,
    travel,
    value_of,
)
from .test_tariffs import ENERGY, FEES

PRICE = f"sensor.{PREFIX}_price_current"
V1_DATA = {CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: OPERATOR_NAME, CONF_TARIFF_URL: TARIFF_URL, CONF_MUNICIPALITY: "Musterwil"}


async def test_the_actions_are_there_before_any_entry(hass: HomeAssistant, enable_custom_integrations) -> None:
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "get_price")
    assert hass.services.has_service(DOMAIN, "get_price_schedule")


async def test_setup_loads_the_entry_with_all_platforms(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.LOADED
    domains = {registered.domain for registered in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert domains == {"sensor", "binary_sensor", "calendar", "number"}
    assert entry.runtime_data.data["dso_name"] == "Musterwerk AG"


async def test_an_unreachable_file_is_retried_and_no_entity_is_created(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    file = operator_host.files.pop(TARIFF_URL)
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) == []

    operator_host.files[TARIFF_URL] = file
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert value_of(hass, PRICE) == 0.25944


async def test_an_entry_of_the_first_version_gets_its_products_from_the_customer_type(
    hass: HomeAssistant, wednesday_morning, operator_host
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, entry_id=ENTRY_ID, title="Musterwerk", data={**V1_DATA, "customer_type": "Basic"})
    await setup_entry(hass, entry)
    assert entry.version == 2
    assert entry.state is ConfigEntryState.LOADED
    assert "customer_type" not in entry.data
    assert entry.data[CONF_ENERGY_PRODUCT] == "Basic Energie"
    assert entry.data[CONF_GRID_PRODUCT] == "Basic Netz"
    # 12 + 8 + 0.5 Rp + the default 3.03 Rp national levies = 23.53 Rp, x 1.081 = 25.43593 Rp
    assert value_of(hass, PRICE) == 0.25436


async def test_an_entry_of_the_first_version_without_customer_type_stays_as_it_is(
    hass: HomeAssistant, wednesday_morning, operator_host
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=1, entry_id=ENTRY_ID, title="Musterwerk", data=dict(V1_DATA))
    await setup_entry(hass, entry)
    assert entry.version == 2
    assert entry.state is ConfigEntryState.LOADED
    assert dict(entry.data) == V1_DATA
    assert hass.states.get(PRICE) is None
    assert value_of(hass, f"sensor.{PREFIX}_energy_basic_energie") == 0.12


async def test_unloading_takes_the_entities_and_the_repair_issue_away(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Renamed away"}))
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, selection_issue_id(entry)) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert issues.async_get_issue(DOMAIN, selection_issue_id(entry)) is None
    assert state_of(hass, f"sensor.{PREFIX}_grid_doppel_netz").state == STATE_UNAVAILABLE

    # no timer of the unloaded entry is left to fire at the next tariff window
    await travel(hass, wednesday_morning, "2026-11-18 22:00:01+01:00")
    assert state_of(hass, f"sensor.{PREFIX}_grid_doppel_netz").state == STATE_UNAVAILABLE
    assert operator_host.fetched == [TARIFF_URL]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert value_of(hass, f"sensor.{PREFIX}_grid_doppel_netz") == 0.06
    assert issues.async_get_issue(DOMAIN, selection_issue_id(entry)) is not None


async def test_removing_an_entry_withdraws_its_repair_issues(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.registered[None] = TARIFF_URL
    automatic = await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Renamed away"}))
    manual = await setup_entry(hass, manual_entry())
    await hass.async_block_till_done(wait_background_tasks=True)
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, selection_issue_id(automatic)) is not None
    assert issues.async_get_issue(DOMAIN, manual_issue_id(manual)) is not None

    await hass.config_entries.async_remove(automatic.entry_id)
    await hass.config_entries.async_remove(manual.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, selection_issue_id(automatic)) is None
    assert issues.async_get_issue(DOMAIN, manual_issue_id(manual)) is None
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_an_automatic_and_a_manual_entry_of_one_operator_do_not_collide(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    automatic = await setup_entry(hass, automatic_entry())
    manual = await setup_entry(hass, manual_entry())
    assert automatic.state is ConfigEntryState.LOADED and manual.state is ConfigEntryState.LOADED
    assert value_of(hass, PRICE) == 0.25944
    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_current") == 0.30268


async def test_a_failed_daily_fetch_makes_the_entities_unavailable_until_it_works_again(
    hass: HomeAssistant, wednesday_morning, operator_host, caplog
) -> None:
    entry = await setup_entry(hass, automatic_entry())
    file = operator_host.files[TARIFF_URL]
    operator_host.files[TARIFF_URL] = OSError("HTTP 503")
    await refresh(hass, entry)
    assert state_of(hass, PRICE).state == STATE_UNAVAILABLE
    assert state_of(hass, f"number.{PREFIX}_vat_percent").state == "8.1"  # an option, not fetched
    assert "Operator tariff data unreachable: HTTP 503" in caplog.text

    operator_host.files[TARIFF_URL] = file
    await refresh(hass, entry)
    assert value_of(hass, PRICE) == 0.25944


async def test_the_file_is_fetched_again_after_a_day(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    one_rate = {**GRID_TWO_RATES, "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.09}]}}
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, one_rate, FEES)
    await travel(hass, wednesday_morning, "2026-11-19 10:00:05+01:00")
    assert operator_host.fetched == [TARIFF_URL, TARIFF_URL]
    assert entry.runtime_data.data["last_checked"].startswith("2026-11-19T10:00:05")
    # 12 + 9 (the new grid rate) + 0.5 + 1.5 = 23 Rp, x 1.081 = 24.863 Rp
    assert value_of(hass, PRICE) == 0.24863
