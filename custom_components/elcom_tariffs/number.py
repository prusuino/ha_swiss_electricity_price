"""Directly adjustable values: national electricity levies and VAT rate."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_HIGH_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_NAME,
    CONF_VAT_PERCENT,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
)
from .device import device_info
from .localization import t


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    has_location = (
        entry.data.get(CONF_MUNICIPALITY)
        and entry.data.get(CONF_ENERGY_PRODUCT)
        and entry.data.get(CONF_GRID_PRODUCT)
    )
    is_manual = CONF_MANUAL_HIGH_RP in entry.data
    if has_location or is_manual:
        slug = slugify(entry.data.get(CONF_OPERATOR_NAME, entry.entry_id))
        async_add_entities([NationalLeviesNumber(hass, entry, slug), VatNumber(hass, entry, slug)])


class _OptionNumber(NumberEntity):
    """Base class: reads/writes a config entry option value."""

    _attr_has_entity_name = False
    _attr_mode = NumberMode.BOX

    _option_key: str
    _default: float

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_device_info = device_info(hass, entry)

    @property
    def native_value(self) -> float:
        return self._entry.options.get(self._option_key, self._default)

    async def async_set_native_value(self, value: float) -> None:
        new_options = {**self._entry.options, self._option_key: value}
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)


class NationalLeviesNumber(_OptionNumber):
    """National electricity levies: grid surcharge Art. 35, system services,
    strategic reserve, solidarity network costs."""

    _attr_native_unit_of_measurement = "Rp/kWh"
    _attr_native_min_value = 0
    _attr_native_max_value = 20
    _attr_native_step = 0.01
    _attr_icon = "mdi:bank"
    _option_key = CONF_NATIONAL_LEVIES_RP
    _default = DEFAULT_NATIONAL_LEVIES_RP

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, slug: str) -> None:
        super().__init__(hass, entry)
        self._attr_name = t("national_levies_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_national_levies"
        self.entity_id = f"number.elcom_{slug}_national_levies"


class VatNumber(_OptionNumber):
    """Current VAT rate."""

    _attr_native_unit_of_measurement = "%"
    _attr_native_min_value = 0
    _attr_native_max_value = 20
    _attr_native_step = 0.1
    _attr_icon = "mdi:percent"
    _option_key = CONF_VAT_PERCENT
    _default = DEFAULT_VAT_PERCENT

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, slug: str) -> None:
        super().__init__(hass, entry)
        self._attr_name = t("vat_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_vat"
        self.entity_id = f"number.elcom_{slug}_vat_percent"
