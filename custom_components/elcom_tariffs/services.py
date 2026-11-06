"""Actions with response data: the price at any moment and the price curve.

Swiss tariffs are time-of-use schedules, so the price at any future moment
is known from the parsed windows. These actions hand that knowledge to
scripts, automations and Assist: `get_price` answers "what does a kWh cost
at 22:30 tonight?", `get_price_schedule` returns the hourly curve of a
period (from the current hour to the end of tomorrow by default) plus its cheapest hours, for shifting a load into them.

Both take the config entry (the operator/tariff selection) as a field, so
they work for automatic and manual entries alike.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .localization import tariff_level_text
from .price_schedule import entry_price_at, hourly_slots, next_change

SERVICE_GET_PRICE = "get_price"
SERVICE_GET_PRICE_SCHEDULE = "get_price_schedule"

ATTR_CONFIG_ENTRY = "config_entry"
ATTR_DATETIME = "datetime"
ATTR_START = "start"
ATTR_END = "end"
ATTR_CHEAPEST_HOURS = "cheapest_hours"

GET_PRICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_DATETIME): cv.datetime,
    }
)

GET_PRICE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY): cv.string,
        vol.Optional(ATTR_START): cv.datetime,
        vol.Optional(ATTR_END): cv.datetime,
        vol.Optional(ATTR_CHEAPEST_HOURS): vol.All(vol.Coerce(int), vol.Range(min=1, max=48)),
    }
)


def _local(value: datetime | None, default: datetime) -> datetime:
    """A field value as an aware local datetime; the selector delivers
    naive wall-clock time, which is read in the instance's time zone."""
    if value is None:
        return default
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(value)


def _price_function(hass: HomeAssistant, entry_id: str):
    """The configured entry's price-over-time function and its products,
    or a readable error when the entry is unknown, not loaded or has no
    usable tariff selection."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_not_found")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_loaded",
            translation_placeholders={"title": entry.title},
        )
    coordinator = hass.data[DOMAIN][entry.entry_id]
    resolved = entry_price_at(coordinator, entry)
    if resolved is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="no_price",
            translation_placeholders={"title": entry.title},
        )
    price_at, products = resolved
    return entry, price_at, products


async def _get_price(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry, price_at, products = _price_function(hass, call.data[ATTR_CONFIG_ENTRY])
    at = _local(call.data.get(ATTR_DATETIME), dt_util.now())
    price, level = price_at(at)
    change = next_change(price_at, products, at)
    next_price, next_level = price_at(change) if change else (None, None)
    return {
        "entry": entry.title,
        "at": at.isoformat(),
        "price_chf_kwh": price,
        "tariff_level_key": level,
        "tariff_level": tariff_level_text(level, hass),
        "valid_until": change.isoformat() if change else None,
        "next_price_chf_kwh": next_price,
        "next_tariff_level_key": next_level,
        "next_tariff_level": tariff_level_text(next_level, hass),
    }


async def _get_price_schedule(call: ServiceCall) -> ServiceResponse:
    hass = call.hass
    entry, price_at, _products = _price_function(hass, call.data[ATTR_CONFIG_ENTRY])
    now = dt_util.now()
    # Default: from the current hour (a load is shifted into the future, past
    # hours would only clutter the cheapest list) to the end of tomorrow.
    start = _local(call.data.get(ATTR_START), now.replace(minute=0, second=0, microsecond=0))
    end = _local(call.data.get(ATTR_END), dt_util.start_of_local_day(now + timedelta(days=2)))
    if end <= start:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="end_before_start")
    if end - start > timedelta(days=14):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="period_too_long")
    slots = hourly_slots(price_at, start, end)
    for slot in slots:
        slot["tariff_level_key"] = price_at(datetime.fromisoformat(slot["start"]))[1]
    values = [s["value"] for s in slots if s["value"] is not None]
    response: dict = {
        "entry": entry.title,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "slots": slots,
        "min_chf_kwh": min(values) if values else None,
        "max_chf_kwh": max(values) if values else None,
        "avg_chf_kwh": round(sum(values) / len(values), 5) if values else None,
    }
    if count := call.data.get(ATTR_CHEAPEST_HOURS):
        # Cheapest first; equal prices keep chronological order, so the
        # earliest of a flat stretch comes first.
        ranked = sorted((s for s in slots if s["value"] is not None), key=lambda s: s["value"])
        response["cheapest"] = ranked[:count]
    return response


def async_register_services(hass: HomeAssistant) -> None:
    """Register the actions once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_PRICE):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_GET_PRICE, _get_price, schema=GET_PRICE_SCHEMA, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PRICE_SCHEDULE,
        _get_price_schedule,
        schema=GET_PRICE_SCHEDULE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
