"""The adjustable numbers (national levies, VAT rate, amount exempt from
VAT): they hold the entry's options, and a change reaches the combined
price at once without the tariff file being fetched again."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.elcom_tariffs.const import CONF_NATIONAL_LEVIES_RP, CONF_VAT_EXEMPT_LEVIES_RP, CONF_VAT_PERCENT

from .common import PREFIX, TARIFF_URL, automatic_entry, name_of, setup_entry, state_of, value_of

PRICE = f"sensor.{PREFIX}_price_current"
NATIONAL = f"number.{PREFIX}_national_levies"
VAT = f"number.{PREFIX}_vat_percent"
EXEMPT = f"number.{PREFIX}_vat_exempt_levies"


async def _set(hass: HomeAssistant, entity_id: str, value: float) -> None:
    await hass.services.async_call("number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True)
    await hass.async_block_till_done()


async def test_the_numbers_show_the_options_of_the_entry(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    await setup_entry(hass, automatic_entry(options={CONF_VAT_EXEMPT_LEVIES_RP: 0.6}))
    national = state_of(hass, NATIONAL)
    assert float(national.state) == 1.5
    assert name_of(hass, NATIONAL) == "National Electricity Levies"
    assert national.attributes["unit_of_measurement"] == "Rp/kWh"
    assert national.attributes["max"] == 20
    vat = state_of(hass, VAT)
    assert float(vat.state) == 8.1
    assert vat.attributes["unit_of_measurement"] == "%"
    assert float(state_of(hass, EXEMPT).state) == 0.6


async def test_a_new_value_reaches_the_price_at_once(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    assert value_of(hass, PRICE) == 0.25944

    await _set(hass, NATIONAL, 3.03)
    assert entry.options[CONF_NATIONAL_LEVIES_RP] == 3.03
    assert value_of(hass, NATIONAL) == 3.03
    # 12 + 10 + 0.5 + 3.03 = 25.53 Rp, x 1.081 = 27.59793 Rp
    assert value_of(hass, PRICE) == 0.27598

    await _set(hass, VAT, 0)
    assert entry.options[CONF_VAT_PERCENT] == 0
    # no VAT: 25.53 Rp
    assert value_of(hass, PRICE) == 0.2553

    await _set(hass, EXEMPT, 0.6)
    assert entry.options[CONF_VAT_EXEMPT_LEVIES_RP] == 0.6
    # 25.53 Rp + 0.6 Rp = 26.13 Rp
    assert value_of(hass, PRICE) == 0.2613
    assert operator_host.fetched == [TARIFF_URL]  # the entry was not reloaded for it


async def test_options_changed_elsewhere_show_on_the_numbers(hass: HomeAssistant, wednesday_morning, operator_host) -> None:
    entry = await setup_entry(hass, automatic_entry())
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_VAT_PERCENT: 7.7})
    await hass.async_block_till_done()
    assert value_of(hass, VAT) == 7.7
    # 24 Rp x 1.077 = 25.848 Rp
    assert value_of(hass, PRICE) == 0.25848
