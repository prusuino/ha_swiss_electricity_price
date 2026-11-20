"""Fetching the operator's tariff file for a running entry: the request
itself, the fall-back to next year's registration, a registration that
changes, files that are unusable or hold another year, the repair issue
for a product or municipality that left the file, and the one-off
notification about next year's tariffs."""
from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.elcom_tariffs.const import (
    CONF_CANTONAL_LEVIES,
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MUNICIPALITY,
    CONF_NEXT_PERIOD_NOTIFIED,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    DOMAIN,
)
from custom_components.elcom_tariffs.coordinator import async_fetch_raw_tariffs, selection_issue_id

from .common import (
    CANTONAL,
    COORDINATOR,
    GRID_TWO_RATES,
    NEXT_YEAR_URL,
    PREFIX,
    TARIFF_URL,
    TARIFFS_2026,
    TARIFFS_2027,
    TAXE,
    automatic_entry,
    next_year,
    refresh,
    sample_file,
    setup_entry,
    state_of,
    value_of,
)
from .test_directory import FakeResponse, FakeSession, HttpError, fake_session
from .test_tariffs import ENERGY, FEES, GRID

PRICE = f"sensor.{PREFIX}_price_current"
PREVIEW = f"sensor.{PREFIX}_price_next_period"
FILENAME = f"sensor.{PREFIX}_diagnostics_filename"
NEW_URL = "https://example.invalid/neu/tarife.json"


def _selection_issue(hass: HomeAssistant, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, selection_issue_id(entry))


async def test_the_file_is_requested_with_the_integrations_own_user_agent(hass: HomeAssistant) -> None:
    session = FakeSession({TARIFF_URL: FakeResponse('{"dsoName": "Elektrizitätswerk Musterwil"}'.encode("cp1252"))})
    with fake_session(session, "coordinator"):
        assert await async_fetch_raw_tariffs(hass, TARIFF_URL) == {"dsoName": "Elektrizitätswerk Musterwil"}
    assert session.requested == [TARIFF_URL]
    assert session.headers[0]["User-Agent"].startswith("SwissElectricityPrice/")
    assert "aiohttp" not in session.headers[0]["User-Agent"]

    with fake_session(FakeSession({TARIFF_URL: FakeResponse(status=406)}), "coordinator"), pytest.raises(HttpError, match="HTTP 406"):
        await async_fetch_raw_tariffs(hass, TARIFF_URL)


async def test_a_dead_link_falls_back_to_next_years_registration(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files = {NEXT_YEAR_URL: sample_file()}
    operator_host.registered = {None: TARIFF_URL, 2027: NEXT_YEAR_URL}
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.LOADED
    assert operator_host.lookups == [None, 2027]
    assert state_of(hass, FILENAME).state == "tarife_2027.json"
    assert value_of(hass, PRICE) == 0.25944  # the file carries this year's tariffs as well
    # for this fetch only: the stored registration stays and is tried first again tomorrow
    assert entry.data[CONF_TARIFF_URL] == TARIFF_URL
    assert entry.runtime_data.data["registered_url"] == TARIFF_URL


@pytest.mark.parametrize("next_year_registration", [None, TARIFF_URL, "https://example.invalid/also/gone.json"])
async def test_without_a_working_registration_the_entry_is_retried(
    hass: HomeAssistant, wednesday_morning, operator_host, caplog, next_year_registration
) -> None:
    operator_host.files = {}
    operator_host.registered = {2027: next_year_registration}
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert operator_host.lookups == [None, 2027]
    assert "Operator tariff data unreachable" in caplog.text


async def test_with_the_directory_down_the_problem_of_the_link_is_reported(hass: HomeAssistant, wednesday_morning, operator_host, caplog) -> None:
    operator_host.files = {}
    operator_host.directory_error = TimeoutError("directory down")
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert f"Operator tariff data unreachable: No machine-readable tariff file could be found at {TARIFF_URL}" in caplog.text


async def test_a_link_given_by_the_user_is_never_replaced(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    own_url = "https://example.invalid/own/tarife.json"
    operator_host.files[own_url] = operator_host.files[TARIFF_URL]
    operator_host.registered = {None: TARIFF_URL, 2027: NEXT_YEAR_URL}
    entry = await setup_entry(hass, automatic_entry({CONF_TARIFF_URL: own_url, CONF_URL_IS_MANUAL: True}))
    assert entry.state is ConfigEntryState.LOADED
    assert operator_host.fetched == [own_url]
    assert operator_host.lookups == []  # the directory is not even asked

    del operator_host.files[own_url]
    await refresh(hass, entry)
    assert state_of(hass, PRICE).state == STATE_UNAVAILABLE
    assert operator_host.lookups == []
    assert entry.data[CONF_TARIFF_URL] == own_url


async def test_a_new_registration_that_works_replaces_the_stored_link(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    one_rate = {**ENERGY, "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.2}]}}
    operator_host.files[NEW_URL] = sample_file(one_rate, GRID_TWO_RATES, FEES)
    operator_host.registered[None] = NEW_URL
    await refresh(hass, entry)
    assert entry.data[CONF_TARIFF_URL] == NEW_URL
    assert entry.runtime_data.data["registered_url"] == NEW_URL
    # the new file: 20 + 10 + 0.5 + 1.5 = 32 Rp, x 1.081 = 34.592 Rp
    assert value_of(hass, PRICE) == 0.34592


@pytest.mark.parametrize("new_file", [None, HttpError("HTTP 500")])
async def test_a_new_registration_that_does_not_work_yet_is_left_aside(hass: HomeAssistant, wednesday_morning, operator_host, new_file) -> None:
    entry = await setup_entry(hass, automatic_entry())
    if new_file is not None:
        operator_host.files[NEW_URL] = new_file
    operator_host.registered[None] = NEW_URL
    await refresh(hass, entry)
    assert entry.data[CONF_TARIFF_URL] == TARIFF_URL
    assert value_of(hass, PRICE) == 0.25944


async def test_the_registration_is_kept_while_the_directory_is_down(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.directory_error = TimeoutError("directory down")
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data[CONF_TARIFF_URL] == TARIFF_URL
    assert value_of(hass, PRICE) == 0.25944


@pytest.mark.parametrize(
    ("file", "problem"),
    [
        # a window without its times
        ({"tariffs": [{**ENERGY, "prices": {"energy": [{"price": 0.12}]}}]}, "Operator tariff data is malformed (KeyError"),
        # times that are no times
        ({"tariffs": [{**ENERGY, "prices": {"energy": [{"from": "six", "to": "22:00", "price": 0.12}]}}]}, "Operator tariff data is malformed (ValueError"),
        # no list of records
        ({"tariffs": "see our website"}, "Operator tariff data is malformed (AttributeError"),
        # a price that is no number
        ({"tariffs": [{**ENERGY, "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": "0.12"}]}}]}, "Operator tariff data is malformed (TypeError"),
        # grid tariffs only
        ({"tariffs": [GRID]}, "No energy tariffs found in the operator's tariff data"),
        # an empty file
        ({}, "No energy tariffs found in the operator's tariff data"),
    ],
)
async def test_a_file_nothing_can_be_read_from_is_retried_with_a_clear_reason(
    hass: HomeAssistant, wednesday_morning, operator_host, caplog, file, problem
) -> None:
    operator_host.files[TARIFF_URL] = file
    entry = await setup_entry(hass, automatic_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert problem in caplog.text
    # a reason for the retry, not an unexpected error with a traceback in the log
    assert "Unexpected error" not in caplog.text
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


async def test_a_file_with_only_next_years_tariffs_shows_those(hass: HomeAssistant, wednesday_morning, operator_host, caplog) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2027)
    entry = await setup_entry(hass, automatic_entry())
    assert "no tariffs valid on 2026-11-18 in the operator's file, showing the next ones instead" in caplog.text
    assert entry.runtime_data.data["tariff_selection"] == "upcoming"
    assert state_of(hass, f"sensor.{PREFIX}_diagnostics_year").state == "2027"
    # 13 + 11 + 0.6 + 1.5 = 26.1 Rp, x 1.081 = 28.2141 Rp
    assert value_of(hass, PRICE) == 0.28214
    assert state_of(hass, PREVIEW).state == STATE_UNAVAILABLE  # what is shown already is no preview


async def test_a_file_that_was_not_replaced_shows_the_most_recent_tariffs(hass: HomeAssistant, freezer, operator_host, caplog) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to("2028-02-02 10:00:00+01:00")  # a Wednesday
    entry = await setup_entry(hass, automatic_entry())
    assert "no tariffs valid on 2028-02-02 in the operator's file, showing the most recent ones instead" in caplog.text
    assert entry.runtime_data.data["tariff_selection"] == "expired"
    assert value_of(hass, PRICE) == 0.28214


async def test_a_selection_that_left_the_file_is_a_repair_issue(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2026, CANTONAL)
    entry = automatic_entry({
        CONF_ENERGY_PRODUCT: "Alt Energie", CONF_GRID_PRODUCT: "Alt Netz", CONF_MUNICIPALITY: "Altwil",
        CONF_CANTONAL_LEVIES: [TAXE, "VD – Alte Taxe"],
    })
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    issue = _selection_issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "selection_missing"
    assert issue.translation_placeholders == {"operator": "Musterwerk AG", "missing": "Alt Energie, Alt Netz, Altwil, VD – Alte Taxe"}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert state_of(hass, PRICE).state == STATE_UNAVAILABLE
    assert value_of(hass, f"sensor.{PREFIX}_energy_basic_energie") == 0.12

    # the operator's next file has the names again: the issue goes by itself
    old_energy = {**ENERGY, "tariffName": "Alt Energie"}
    old_grid = {**GRID, "tariffName": "Alt Netz"}
    old_fees = {**FEES, "prices": {
        "municipalityTaxes": [{"municipalityName": "Altwil", "municipalityEnergy": [{"price": 0.005}]}],
        "cantonalTaxes": [{"cantonName": "VD", "cantonComment": comment, "cantonEnergy": [{"from": "00:00", "to": "00:00", "price": 0.006}]}
                          for comment in ("Taxe cantonale", "Alte Taxe")],
    }}
    operator_host.files[TARIFF_URL] = sample_file(old_energy, old_grid, old_fees)
    await refresh(hass, entry)
    assert _selection_issue(hass, entry) is None
    # 12 + 8 + 0.5 + 1.5 + two cantonal levies of 0.6 = 23.2 Rp, x 1.081 = 25.0792 Rp
    assert value_of(hass, PRICE) == 0.25079


async def test_a_vanished_cantonal_levy_is_named_but_the_price_stays(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry({CONF_CANTONAL_LEVIES: [TAXE]}))
    assert _selection_issue(hass, entry).translation_placeholders["missing"] == TAXE
    assert value_of(hass, PRICE) == 0.25944  # too low by the levy rather than unavailable


async def test_a_municipality_is_not_missed_in_a_file_without_municipalities(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID)
    entry = await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    assert _selection_issue(hass, entry) is None
    # 12 + 8 + 1.5 = 21.5 Rp, x 1.081 = 23.2415 Rp
    assert value_of(hass, PRICE) == pytest.approx(0.23241, abs=1e-5)


async def test_next_years_tariffs_are_announced_once(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    with patch(COORDINATOR + "persistent_notification.async_create") as notify:
        entry = await setup_entry(hass, automatic_entry())
        assert entry.data[CONF_NEXT_PERIOD_NOTIFIED] == "2027-01-01"
        await refresh(hass, entry)
    notify.assert_called_once()
    # energy + grid: 12 + 10 -> 13 + 11 Rp high (+2 of 22), 9 + 6 -> 10 + 7 Rp low (+2 of 15)
    assert notify.call_args.args[1] == (
        "Musterwerk AG has published its tariffs from 01.01.2027. Energy + grid (Basic Energie + Doppel Netz), before levies and VAT:"
        " high tariff 22.00 → 24.00 Rp/kWh (+9.1 %), low tariff 15.00 → 17.00 Rp/kWh (+13.3 %)."
        ' Details in the sensor "Electricity Price Next Tariff Period".'
    )
    assert notify.call_args.kwargs["title"] == "New electricity tariffs from 01.01.2027"
    assert notify.call_args.kwargs["notification_id"] == f"{DOMAIN}_next_period_{entry.entry_id}"


async def test_the_announcement_of_a_flat_product_compares_its_one_rate(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    lower = next_year(GRID, {"energy": [{"from": "00:00", "to": "00:00", "price": 0.06}]})
    operator_host.files[TARIFF_URL] = sample_file(ENERGY, GRID, FEES, next_year(ENERGY), lower)
    with patch(COORDINATOR + "persistent_notification.async_create") as notify:
        await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Basic Netz"}))
    # 12 + 8 -> 12 + 6 Rp high (-2 of 20), 9 + 8 -> 9 + 6 Rp low (-2 of 17)
    assert "high tariff 20.00 → 18.00 Rp/kWh (-10.0 %), low tariff 17.00 → 15.00 Rp/kWh (-11.8 %)" in notify.call_args.args[1]


@pytest.mark.parametrize(
    ("data", "tariffs"),
    [
        # the chosen grid product is not in next year's set
        ({CONF_GRID_PRODUCT: "Basic Netz"}, None),
        # the chosen energy product is not in the file at all
        ({CONF_ENERGY_PRODUCT: "Alt Energie"}, None),
        # next year's set holds no product at all, only levies
        ({}, [*TARIFFS_2026, next_year(FEES)]),
    ],
)
async def test_nothing_is_announced_without_both_products_in_both_years(hass: HomeAssistant, wednesday_morning, operator_host, data, tariffs) -> None:
    if tariffs:
        operator_host.files[TARIFF_URL] = sample_file(*tariffs)
    with patch(COORDINATOR + "persistent_notification.async_create") as notify:
        entry = await setup_entry(hass, automatic_entry(data))
    notify.assert_not_called()
    assert CONF_NEXT_PERIOD_NOTIFIED not in entry.data
