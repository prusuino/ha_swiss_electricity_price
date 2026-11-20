"""The actions `get_price` and `get_price_schedule`: the price at a moment
and until when it holds, the hourly curve with its cheapest hours, for an
entry on the operator's file and one with manual rates, and the errors
for an entry that cannot answer or a period that makes no sense."""
from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.elcom_tariffs.const import CONF_ENERGY_PRODUCT, CONF_MANUAL_LOW_RP, DOMAIN

from .common import automatic_entry, manual_entry, setup_entry

TITLE = "Musterwerk AG Electricity Prices (Musterwil, Basic Energie)"


async def _call(hass: HomeAssistant, action: str, entry_id: str, **fields) -> dict:
    return await hass.services.async_call(DOMAIN, action, {"config_entry": entry_id, **fields}, blocking=True, return_response=True)


async def test_the_price_now_comes_with_the_time_it_holds_until(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    # high: 12 + 10 + 0.5 + 1.5 = 24 Rp, x 1.081 = 25.944 Rp; low: 9 + 6 + 0.5 + 1.5 = 17 Rp, x 1.081 = 18.377 Rp
    assert await _call(hass, "get_price", entry.entry_id) == {
        "entry": TITLE,
        "at": "2026-11-18T10:00:00+01:00",
        "price_chf_kwh": 0.25944,
        "tariff_level_key": "high",
        "tariff_level": "High tariff",
        "valid_until": "2026-11-18T22:00:00+01:00",
        "next_price_chf_kwh": 0.18377,
        "next_tariff_level_key": "low",
        "next_tariff_level": "Low tariff",
    }


async def test_the_price_at_a_given_moment_is_read_in_local_time(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    # a Saturday noon without a time zone: low tariff until Monday morning
    weekend = await _call(hass, "get_price", entry.entry_id, datetime="2026-11-21 12:00:00")
    assert weekend["at"] == "2026-11-21T12:00:00+01:00"
    assert weekend["price_chf_kwh"] == 0.18377
    assert weekend["tariff_level_key"] == "low"
    assert weekend["valid_until"] == "2026-11-23T06:00:00+01:00"
    assert weekend["next_price_chf_kwh"] == 0.25944
    # a moment given in UTC is 22:30 in Switzerland
    evening = await _call(hass, "get_price", entry.entry_id, datetime="2026-11-18T21:30:00+00:00")
    assert evening["at"] == "2026-11-18T22:30:00+01:00"
    assert evening["tariff_level"] == "Low tariff"
    assert evening["valid_until"] == "2026-11-19T06:00:00+01:00"


async def test_a_flat_manual_rate_never_changes(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry({CONF_MANUAL_LOW_RP: None}))
    # 25 + 3 = 28 Rp, x 1.081 = 30.268 Rp
    assert await _call(hass, "get_price", entry.entry_id) == {
        "entry": "Musterwerk AG Electricity Prices (manual)",
        "at": "2026-11-18T10:00:00+01:00",
        "price_chf_kwh": 0.30268,
        "tariff_level_key": "flat",
        "tariff_level": "Flat tariff",
        "valid_until": None,
        "next_price_chf_kwh": None,
        "next_tariff_level_key": None,
        "next_tariff_level": None,
    }


async def test_the_schedule_runs_from_this_hour_to_the_end_of_tomorrow(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    wednesday_morning.move_to("2026-11-18 10:20:00+01:00")
    schedule = await _call(hass, "get_price_schedule", entry.entry_id)
    assert schedule["entry"] == TITLE
    assert schedule["start"] == "2026-11-18T10:00:00+01:00"
    assert schedule["end"] == "2026-11-20T00:00:00+01:00"
    slots = schedule["slots"]
    assert len(slots) == 38
    assert slots[0] == {"start": "2026-11-18T10:00:00+01:00", "end": "2026-11-18T11:00:00+01:00", "value": 0.25944, "tariff_level_key": "high"}
    assert slots[12] == {"start": "2026-11-18T22:00:00+01:00", "end": "2026-11-18T23:00:00+01:00", "value": 0.18377, "tariff_level_key": "low"}
    assert schedule["min_chf_kwh"] == 0.18377
    assert schedule["max_chf_kwh"] == 0.25944
    # 28 hours at 0.25944 and 10 at 0.18377
    assert schedule["avg_chf_kwh"] == 0.23953
    assert "cheapest" not in schedule


async def test_the_cheapest_hours_come_earliest_first(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry())
    schedule = await _call(
        hass, "get_price_schedule", entry.entry_id, start="2026-11-18 19:30:00", end="2026-11-19 02:00:00", cheapest_hours=3
    )
    # the slots follow the given start, the last one ends with the period
    assert [slot["start"][11:16] for slot in schedule["slots"]] == ["19:30", "20:30", "21:30", "22:30", "23:30", "00:30", "01:30"]
    assert schedule["slots"][-1]["end"] == "2026-11-19T02:00:00+01:00"
    # 25 + 3 = 28 Rp, x 1.081 = 30.268 Rp until 22:00; 18 + 3 = 21 Rp, x 1.081 = 22.701 Rp after
    assert [slot["value"] for slot in schedule["slots"]] == [0.30268] * 3 + [0.22701] * 4
    assert [slot["start"] for slot in schedule["cheapest"]] == [
        "2026-11-18T22:30:00+01:00", "2026-11-18T23:30:00+01:00", "2026-11-19T00:30:00+01:00",
    ]
    assert all(slot["tariff_level_key"] == "low" for slot in schedule["cheapest"])


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"start": "2026-11-19 00:00:00", "end": "2026-11-18 00:00:00"}, "end_before_start"),
        ({"start": "2026-11-19 00:00:00", "end": "2026-11-19 00:00:00"}, "end_before_start"),
        ({"start": "2026-11-18 00:00:00", "end": "2026-12-02 01:00:00"}, "period_too_long"),
    ],
)
async def test_a_period_that_makes_no_sense_is_refused(hass: HomeAssistant, wednesday_morning, operator_host, fields, error) -> None:
    entry = await setup_entry(hass, automatic_entry())
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "get_price_schedule", entry.entry_id, **fields)
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == error


async def test_fourteen_days_are_the_longest_period(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    schedule = await _call(hass, "get_price_schedule", entry.entry_id, start="2026-11-18 00:00:00", end="2026-12-02 00:00:00")
    assert len(schedule["slots"]) == 14 * 24
    with pytest.raises(vol.Invalid):
        await _call(hass, "get_price_schedule", entry.entry_id, cheapest_hours=0)


async def test_an_unknown_entry_is_refused(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    other = MockConfigEntry(domain="another_integration")
    other.add_to_hass(hass)
    for entry_id in ("no-such-entry", other.entry_id):
        with pytest.raises(ServiceValidationError) as raised:
            await _call(hass, "get_price", entry_id)
        assert raised.value.translation_key == "entry_not_found"


async def test_an_entry_that_is_not_loaded_is_refused(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    assert await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "get_price_schedule", entry.entry_id)
    assert raised.value.translation_key == "entry_not_loaded"
    assert raised.value.translation_placeholders == {"title": TITLE}


async def test_an_entry_whose_product_left_the_file_has_no_price(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Renamed away"}))
    with pytest.raises(ServiceValidationError) as raised:
        await _call(hass, "get_price", entry.entry_id)
    assert raised.value.translation_key == "no_price"
    assert raised.value.translation_placeholders == {"title": TITLE}
