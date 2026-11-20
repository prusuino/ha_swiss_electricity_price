"""An entry with rates entered by hand (the fallback when the operator has
no usable tariff file): its price and level entities, the switch between
the two rates, a single flat rate, and the repair issue that tells when
the operator's own file starts working."""
from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er, issue_registry as ir

from custom_components.elcom_tariffs.const import (
    CONF_MANUAL_GRID_FEE_CHF,
    CONF_MANUAL_HIGH_FROM,
    CONF_MANUAL_HIGH_WEEKDAYS,
    CONF_MANUAL_LOW_RP,
    CONF_VAT_EXEMPT_LEVIES_RP,
    DOMAIN,
)
from custom_components.elcom_tariffs.coordinator import manual_issue_id

from .common import (
    MANUAL_PREFIX,
    TARIFF_URL,
    TARIFFS_2027,
    device_of,
    manual_entry,
    name_of,
    refresh,
    sample_file,
    setup_entry,
    state_of,
    travel,
    value_of,
)
from .test_tariffs import GRID

PRICE = f"sensor.{MANUAL_PREFIX}_price_current"
LEVEL = f"sensor.{MANUAL_PREFIX}_tariff_level"
HIGH_TARIFF = f"binary_sensor.{MANUAL_PREFIX}_high_tariff"


def _link_issue(hass: HomeAssistant, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, manual_issue_id(entry))


async def test_the_entered_rate_plus_levies_and_vat_is_the_price(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, manual_entry())
    state = state_of(hass, PRICE)
    # weekday 10:00: 25 Rp entered + 3 Rp national = 28 Rp, x 1.081 = 30.268 Rp
    assert float(state.state) == 0.30268
    assert name_of(hass, PRICE) == "Current Electricity Price"
    assert state.attributes["calculation"] == (
        "25.00 Rp manually entered tariff + 3.00 Rp national levies = 28.00 Rp, × 1.081 (8.1% VAT) = 30.27 Rp/kWh = 0.3027 CHF/kWh"
    )
    assert state.attributes["tariff_level"] == "High tariff"
    assert state.attributes["tariff_rp_kwh"] == 25.0
    assert state.attributes["national_levies_rp_kwh"] == 3.0
    assert state.attributes["vat_percent"] == 8.1
    # 18 + 3 = 21 Rp, x 1.081 = 22.701 Rp
    assert [slot["value"] for slot in state.attributes["raw_today"]] == [0.22701] * 6 + [0.30268] * 16 + [0.22701] * 2
    assert state.attributes["min_today"] == 0.22701
    assert state.attributes["max_today"] == 0.30268
    assert state.attributes["next_change"] == "2026-11-18T22:00:00+01:00"
    assert state.attributes["next_price"] == 0.22701
    assert state.attributes["next_level"] == "Low tariff"

    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_high") == 0.30268
    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_low") == 0.22701
    assert name_of(hass, f"sensor.{MANUAL_PREFIX}_price_low") == "Electricity Price Low tariff"
    fee = state_of(hass, f"sensor.{MANUAL_PREFIX}_base_fee")
    assert float(fee.state) == 9.5
    assert fee.attributes["unit_of_measurement"] == "CHF/month"
    assert value_of(hass, f"number.{MANUAL_PREFIX}_national_levies") == 3.0
    assert device_of(hass, entry).name == "Musterwerk AG Electricity Prices (manual)"
    assert operator_host.fetched == []  # the rates are local, no tariff file is involved


async def test_an_amount_exempt_from_vat_is_added_after_vat(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, manual_entry(options={CONF_VAT_EXEMPT_LEVIES_RP: 0.5}))
    state = state_of(hass, PRICE)
    # 28 Rp x 1.081 = 30.268 Rp, + 0.5 Rp = 30.768 Rp
    assert float(state.state) == 0.30768
    assert state.attributes["calculation"] == (
        "25.00 Rp manually entered tariff + 3.00 Rp national levies = 28.00 Rp, × 1.081 (8.1% VAT)"
        " = 30.268 Rp + 0.500 Rp levies exempt from VAT = 30.77 Rp/kWh = 0.3077 CHF/kWh"
    )
    # 21 Rp x 1.081 = 22.701 Rp, + 0.5 Rp = 23.201 Rp
    assert value_of(hass, f"sensor.{MANUAL_PREFIX}_price_low") == 0.23201


async def test_level_and_price_switch_at_the_entered_times(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, manual_entry())
    level = state_of(hass, LEVEL)
    assert level.state == "High tariff"
    assert level.attributes["schedule"] == [
        {"from": "06:00", "to": "22:00", "weekdays": ["Mo", "Tu", "We", "Th", "Fr"], "rp_kwh": 25.0, "level": "high"}
    ]
    assert level.attributes["next_change"] == "2026-11-18T22:00:00+01:00"
    assert level.attributes["next_level"] == "Low tariff"
    assert state_of(hass, HIGH_TARIFF).state == STATE_ON

    await travel(hass, wednesday_morning, "2026-11-18 22:00:01+01:00")
    assert state_of(hass, LEVEL).state == "Low tariff"
    assert state_of(hass, HIGH_TARIFF).state == STATE_OFF
    assert value_of(hass, PRICE) == 0.22701
    assert state_of(hass, HIGH_TARIFF).attributes["next_change"] == "2026-11-19T06:00:00+01:00"

    await travel(hass, wednesday_morning, "2026-11-19 06:00:01+01:00")
    assert state_of(hass, LEVEL).state == "High tariff"
    assert value_of(hass, PRICE) == 0.30268


async def test_without_weekdays_the_high_rate_applies_every_day(hass: HomeAssistant, freezer, operator_host) -> None:
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to("2026-11-21 10:00:00+01:00")  # a Saturday
    await setup_entry(hass, manual_entry({CONF_MANUAL_HIGH_WEEKDAYS: []}))
    assert state_of(hass, LEVEL).state == "High tariff"
    assert value_of(hass, PRICE) == 0.30268


async def test_a_single_rate_has_a_price_and_nothing_to_switch(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, manual_entry({CONF_MANUAL_LOW_RP: None, CONF_MANUAL_GRID_FEE_CHF: None}))
    state = state_of(hass, PRICE)
    assert float(state.state) == 0.30268
    assert state.attributes["tariff_level"] == "Flat tariff"
    assert {slot["value"] for slot in state.attributes["raw_today"]} == {0.30268}
    assert state.attributes["next_change"] is None
    assert state.attributes["next_price"] is None
    assert state.attributes["next_level"] is None
    registry = er.async_get(hass)
    for absent in (LEVEL, HIGH_TARIFF, f"sensor.{MANUAL_PREFIX}_price_high", f"sensor.{MANUAL_PREFIX}_base_fee", f"calendar.{MANUAL_PREFIX}_tariff"):
        assert registry.async_get(absent) is None, absent

    await travel(hass, wednesday_morning, "2026-11-19 00:00:01+01:00")
    assert value_of(hass, PRICE) == 0.30268


async def test_times_that_are_no_times_keep_the_entry_from_loading(hass: HomeAssistant, wednesday_morning, operator_host, caplog) -> None:
    entry = await setup_entry(hass, manual_entry({CONF_MANUAL_HIGH_FROM: "six"}))
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert "Manual tariff times are malformed" in caplog.text
    assert hass.states.get(PRICE) is None


async def test_a_working_tariff_file_is_reported_as_a_repair_issue(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.registered[None] = TARIFF_URL
    entry = await setup_entry(hass, manual_entry())
    await hass.async_block_till_done(wait_background_tasks=True)
    issue = _link_issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "manual_link_available"
    assert issue.translation_placeholders == {"operator": "Musterwerk AG"}
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.is_fixable is False
    assert value_of(hass, PRICE) == 0.30268  # the entry keeps its entered rates

    # the link stops working again: the next daily check withdraws the issue
    del operator_host.files[TARIFF_URL]
    await refresh(hass, entry)
    assert _link_issue(hass, entry) is None


@pytest.mark.parametrize(
    ("registered", "file"),
    [
        # the operator has reported no link
        (None, None),
        # the link leads nowhere
        ("https://example.invalid/gone.json", None),
        # the file cannot be read
        (TARIFF_URL, OSError("HTTP 403")),
        # the file has grid tariffs but no energy tariffs
        (TARIFF_URL, sample_file(GRID)),
    ],
)
async def test_no_repair_issue_without_a_usable_tariff_file(hass: HomeAssistant, wednesday_morning, operator_host, registered, file) -> None:
    operator_host.registered[None] = registered
    if file is not None:
        operator_host.files[TARIFF_URL] = file
    entry = await setup_entry(hass, manual_entry())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert operator_host.lookups == [None]
    assert _link_issue(hass, entry) is None
    assert entry.state is ConfigEntryState.LOADED


async def test_a_file_that_starts_working_later_is_noticed_by_the_daily_check(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    operator_host.files[TARIFF_URL] = sample_file(*TARIFFS_2027)  # only next year's tariffs: still counts as usable
    entry = await setup_entry(hass, manual_entry())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert _link_issue(hass, entry) is None

    operator_host.registered[None] = TARIFF_URL
    await refresh(hass, entry)
    assert _link_issue(hass, entry) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _link_issue(hass, entry) is None
