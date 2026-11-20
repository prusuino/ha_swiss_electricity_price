"""The high-tariff binary sensor: on during the high-tariff window of the
chosen energy product, switched at the window boundary, and only there
for a tariff with two levels."""
from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.elcom_tariffs.const import CONF_ENERGY_PRODUCT

from .common import PREFIX, automatic_entry, name_of, setup_entry, state_of, travel

HIGH_TARIFF = f"binary_sensor.{PREFIX}_high_tariff"


async def test_on_during_the_high_tariff_window(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    state = state_of(hass, HIGH_TARIFF)
    assert state.state == STATE_ON
    assert name_of(hass, HIGH_TARIFF) == "High Tariff Active"
    assert state.attributes["icon"] == "mdi:trending-up"
    assert state.attributes["next_change"] == "2026-11-18T22:00:00+01:00"
    assert state.attributes["next_level"] == "Low tariff"


async def test_off_from_22_00_and_over_the_weekend(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry())
    await travel(hass, wednesday_morning, "2026-11-18 22:00:01+01:00")
    state = state_of(hass, HIGH_TARIFF)
    assert state.state == STATE_OFF
    assert state.attributes["icon"] == "mdi:trending-down"
    assert state.attributes["next_change"] == "2026-11-19T06:00:00+01:00"
    assert state.attributes["next_level"] == "High tariff"

    # Saturday 10:00: the high window is for weekdays, the next one starts on Monday
    await travel(hass, wednesday_morning, "2026-11-21 10:00:00+01:00")
    state = state_of(hass, HIGH_TARIFF)
    assert state.state == STATE_OFF
    assert state.attributes["next_change"] == "2026-11-23T06:00:00+01:00"


async def test_not_created_for_a_flat_energy_product(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry({CONF_ENERGY_PRODUCT: "Green Energie"}))
    assert er.async_get(hass).async_get(HIGH_TARIFF) is None
    assert state_of(hass, f"sensor.{PREFIX}_tariff_level").state == "Flat tariff"
    assert state_of(hass, f"sensor.{PREFIX}_tariff_level").attributes["next_change"] is None
    assert state_of(hass, f"sensor.{PREFIX}_tariff_level").attributes["next_level"] is None
