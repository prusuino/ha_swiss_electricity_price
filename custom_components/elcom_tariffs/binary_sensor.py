"""High-tariff binary sensor: on while the configured energy tariff is on
its high rate. The tariff-level sensor shows the same thing as text; a
binary sensor is the handier form for automation conditions and
dashboard conditional cards. Only created for tariffs that actually have
more than one level."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ENERGY_PRODUCT, CONF_MANUAL_HIGH_RP, DOMAIN
from .coordinator import ElcomTariffCoordinator, current_rate
from .device import device_info, entity_id_prefix
from .localization import t
from .price_schedule import level_at, level_change_attributes, localized


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ElcomTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    prefix = entity_id_prefix(entry)
    data = coordinator.data or {}
    if CONF_MANUAL_HIGH_RP in entry.data:
        if data.get("low_rp_kwh") is not None:
            async_add_entities([ElcomHighTariffBinarySensor(hass, coordinator, entry, prefix, None)])
        return
    product = entry.data.get(CONF_ENERGY_PRODUCT)
    if product and data.get("energy", {}).get(product, {}).get("high_rp_kwh") is not None:
        async_add_entities([ElcomHighTariffBinarySensor(hass, coordinator, entry, prefix, product)])


class ElcomHighTariffBinarySensor(CoordinatorEntity[ElcomTariffCoordinator], BinarySensorEntity):
    """On while the energy tariff (the configured product, or the manually
    entered rates) is on its high rate — resolved against the clock on
    every read, refreshed at every window boundary like the sensors."""

    _attr_has_entity_name = False

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: ElcomTariffCoordinator,
        entry: ConfigEntry,
        prefix: str,
        product: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._hass_ref = hass
        self._product = product
        self._attr_name = t("high_tariff_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_high_tariff"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"binary_sensor.{prefix}_high_tariff"

    def _data(self) -> dict:
        data = self.coordinator.data or {}
        if self._product is None:
            return data
        return data.get("energy", {}).get(self._product, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def is_on(self) -> bool | None:
        _, level = current_rate(self._data(), dt_util.now())
        return None if level is None else level == "high"

    @property
    def icon(self) -> str:
        return "mdi:trending-up" if self.is_on else "mdi:trending-down"

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return localized(level_change_attributes(level_at(d), [d], dt_util.now()), self._hass_ref)
