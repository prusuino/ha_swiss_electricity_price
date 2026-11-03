"""Tariff calendar: the high/low tariff stretches of the configured energy
tariff as calendar events. Swiss tariffs are time-of-use schedules, so the
calendar is known for any date in advance; it is derived from the same
windows as the sensors. Usable with calendar triggers ("30 minutes before
the next low-tariff stretch starts") and the calendar card. Only created
for tariffs with more than one level — a flat tariff has no stretches."""
from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ElcomTariffCoordinator
from .device import device_info, entity_id_prefix
from .localization import t, tariff_level_text
from .price_schedule import entry_level_product, entry_price_at, level_at, level_segments, segment_containing


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ElcomTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    product = entry_level_product(coordinator, entry)
    if product and product.get("high_rp_kwh") is not None and product.get("low_rp_kwh") is not None:
        async_add_entities([ElcomTariffCalendar(hass, coordinator, entry, entity_id_prefix(entry))])


class ElcomTariffCalendar(CoordinatorEntity[ElcomTariffCoordinator], CalendarEntity):
    """One event per stretch of constant tariff level; the summary is the
    level ("High tariff"/"Low tariff"), the description the combined
    price in force during that stretch."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("calendar_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_tariff_calendar"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"calendar.{prefix}_tariff"

    def _product(self) -> dict | None:
        return entry_level_product(self.coordinator, self._entry)

    @property
    def available(self) -> bool:
        return super().available and self._product() is not None

    def _event(self, start: datetime, end: datetime, level: str | None) -> CalendarEvent:
        priced = entry_price_at(self.coordinator, self._entry)
        description = ""
        if priced is not None:
            value, _level = priced[0](start)
            if value is not None:
                description = f"{value:.4f} CHF/kWh"
        return CalendarEvent(
            start=start,
            end=end,
            summary=tariff_level_text(level, self._hass_ref) or "",
            description=description,
            uid=f"{self._entry.entry_id}_{start.isoformat()}",
        )

    @property
    def event(self) -> CalendarEvent | None:
        product = self._product()
        if not product:
            return None
        segment = segment_containing(level_at(product), [product], dt_util.now())
        if segment is None:
            return None
        return self._event(*segment)

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        product = self._product()
        if not product:
            return []
        start_date = dt_util.as_local(start_date)
        end_date = dt_util.as_local(end_date)
        return [
            self._event(seg_start, seg_end, level)
            for seg_start, seg_end, level in level_segments(level_at(product), [product], start_date, end_date)
        ]
