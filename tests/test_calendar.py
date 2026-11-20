"""The tariff calendar: one event per stretch of high or low tariff, with
the combined price of that stretch, for a tariff with two levels."""
from __future__ import annotations

from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.elcom_tariffs.const import CONF_ENERGY_PRODUCT, CONF_GRID_PRODUCT

from .common import MANUAL_PREFIX, PREFIX, automatic_entry, manual_entry, name_of, setup_entry, state_of

CALENDAR = f"calendar.{PREFIX}_tariff"


async def _events(hass: HomeAssistant, entity_id: str, start: str, end: str) -> list[dict]:
    response = await hass.services.async_call(
        "calendar", "get_events",
        {"entity_id": entity_id, "start_date_time": start, "end_date_time": end},
        blocking=True, return_response=True,
    )
    return response[entity_id]["events"]


async def test_the_current_stretch_is_the_calendar_state(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    state = state_of(hass, CALENDAR)
    assert state.state == STATE_ON
    assert name_of(hass, CALENDAR) == "Tariff Calendar"
    assert state.attributes["message"] == "High tariff"
    # 12 + 10 + 0.5 + 1.5 = 24 Rp, x 1.081 = 25.944 Rp
    assert state.attributes["description"] == "0.2594 CHF/kWh"
    assert state.attributes["start_time"] == "2026-11-18 06:00:00"
    assert state.attributes["end_time"] == "2026-11-18 22:00:00"


async def test_a_weekday_has_a_high_stretch_between_two_low_ones(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    events = await _events(hass, CALENDAR, "2026-11-18 00:00:00", "2026-11-19 00:00:00")
    # low: 9 + 6 + 0.5 + 1.5 = 17 Rp, x 1.081 = 18.377 Rp
    assert events == [
        {"start": "2026-11-17T22:00:00+01:00", "end": "2026-11-18T06:00:00+01:00", "summary": "Low tariff", "description": "0.1838 CHF/kWh"},
        {"start": "2026-11-18T06:00:00+01:00", "end": "2026-11-18T22:00:00+01:00", "summary": "High tariff", "description": "0.2594 CHF/kWh"},
        {"start": "2026-11-18T22:00:00+01:00", "end": "2026-11-19T06:00:00+01:00", "summary": "Low tariff", "description": "0.1838 CHF/kWh"},
    ]


async def test_the_weekend_is_one_low_stretch(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    events = await _events(hass, CALENDAR, "2026-11-21 08:00:00", "2026-11-22 20:00:00")
    assert events == [
        {"start": "2026-11-20T22:00:00+01:00", "end": "2026-11-23T06:00:00+01:00", "summary": "Low tariff", "description": "0.1838 CHF/kWh"},
    ]


async def test_without_a_price_the_stretches_are_still_listed(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    """The grid product left the file: the levels of the energy product are known, the combined price is not."""
    await setup_entry(hass, automatic_entry({CONF_GRID_PRODUCT: "Renamed away"}))
    state = state_of(hass, CALENDAR)
    assert state.attributes["message"] == "High tariff"
    assert state.attributes["description"] == ""
    events = await _events(hass, CALENDAR, "2026-11-18 12:00:00", "2026-11-18 13:00:00")
    assert events == [{"start": "2026-11-18T06:00:00+01:00", "end": "2026-11-18T22:00:00+01:00", "summary": "High tariff", "description": ""}]


async def test_no_calendar_for_a_flat_tariff(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Green Energie"}))
    assert er.async_get(hass).async_get(CALENDAR) is None


async def test_manual_rates_have_their_calendar_too(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, manual_entry())
    calendar = f"calendar.{MANUAL_PREFIX}_tariff"
    assert state_of(hass, calendar).attributes["message"] == "High tariff"
    events = await _events(hass, calendar, "2026-11-18 21:00:00", "2026-11-18 23:00:00")
    # 25 + 3 = 28 Rp, x 1.081 = 30.268 Rp; 18 + 3 = 21 Rp, x 1.081 = 22.701 Rp
    assert events == [
        {"start": "2026-11-18T06:00:00+01:00", "end": "2026-11-18T22:00:00+01:00", "summary": "High tariff", "description": "0.3027 CHF/kWh"},
        {"start": "2026-11-18T22:00:00+01:00", "end": "2026-11-19T06:00:00+01:00", "summary": "Low tariff", "description": "0.2270 CHF/kWh"},
    ]
