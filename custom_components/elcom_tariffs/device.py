"""Shared device info for all platforms."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_ENERGY_PRODUCT, CONF_MUNICIPALITY, CONF_OPERATOR_NAME, DOMAIN
from .localization import t


def device_info(hass: HomeAssistant, entry: ConfigEntry) -> DeviceInfo:
    operator = entry.data.get(CONF_OPERATOR_NAME)
    municipality = entry.data.get(CONF_MUNICIPALITY)
    energy_product = entry.data.get(CONF_ENERGY_PRODUCT)
    if municipality and energy_product:
        name = t("device_name", hass, operator=operator, municipality=municipality, energy_product=energy_product)
    else:
        name = t("device_name_manual", hass, operator=operator)
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=name,
        manufacturer=operator,
        model=t("model", hass),
        entry_type="service",
    )
