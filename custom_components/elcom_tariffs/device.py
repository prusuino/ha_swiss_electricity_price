"""Shared device info and entity-id prefix for all platforms."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.util import slugify

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
        entry_type=DeviceEntryType.SERVICE,
    )


def entity_id_prefix(entry: ConfigEntry) -> str:
    """Object-id prefix shared by every entity of an entry: the operator's
    slug plus a short per-entry discriminator (the last four characters of
    the entry id, lower-case), so two entries for the same operator —
    another municipality, another product, or a manual entry next to an
    automatic one — never collide on their suggested entity ids. A
    suggested id only applies when an entity is first created; entities
    that already exist keep whatever id they have."""
    slug = slugify(entry.data.get(CONF_OPERATOR_NAME) or entry.entry_id)
    return f"elcom_{slug}_{entry.entry_id[-4:].lower()}"
