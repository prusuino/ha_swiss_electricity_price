"""Price-over-time helpers: the hourly price curve for today and tomorrow,
today's minimum/maximum/average, and the next moment the price (or tariff
level) changes.

Swiss tariffs are time-of-use schedules, not market prices, so the whole
curve is known in advance from the parsed windows: it is simply the
combined price evaluated at future moments. The curve is exposed in the
shape many dashboard cards already understand from market-price
integrations (`raw_today` / `raw_tomorrow`, one entry per hour with
`start`, `end`, `value`), so existing chart examples work unchanged.

Everything here is evaluated when an entity writes its state — at every
window boundary and at midnight — never on a timer of its own, and the
functions take the price as a callable so the automatic and the manual
entry share them.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_HIGH_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_VAT_PERCENT,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
)
from .coordinator import current_rate, next_switch_time
from .localization import tariff_level_text

# (combined price in CHF/kWh or None, canonical tariff-level key or None) at a moment
PriceAt = Callable[[datetime], tuple[float | None, str | None]]

# A time-of-use schedule repeats at least yearly (seasonal windows), so a
# change that has not come within this horizon never comes: flat tariff.
_CHANGE_HORIZON = timedelta(days=400)


def combined_price_at(energy: dict, grid: dict, municipality_rp: float, levies_rp: float, vat_percent: float) -> PriceAt:
    """The automatic entry's combined price (energy + grid rate active at
    the moment, municipality levy, national levies, VAT) as a function of
    time — the same arithmetic as the combined-price sensor's state. The
    level is the energy product's, like the tariff-level sensor."""

    def price_at(when: datetime) -> tuple[float | None, str | None]:
        energy_rp, level = current_rate(energy, when)
        grid_rp = current_rate(grid, when)[0]
        total_rp = (energy_rp or 0) + (grid_rp or 0) + (municipality_rp or 0) + levies_rp
        return round(total_rp * (1 + vat_percent / 100) / 100, 5), level

    return price_at


def manual_price_at(data: dict, levies_rp: float, vat_percent: float) -> PriceAt:
    """The manual entry's combined price (entered rate + national levies,
    VAT) as a function of time."""

    def price_at(when: datetime) -> tuple[float | None, str | None]:
        rp, level = current_rate(data, when)
        if rp is None:
            return None, level
        return round((rp + levies_rp) * (1 + vat_percent / 100) / 100, 5), level

    return price_at


def level_at(product: dict) -> Callable[[datetime], str | None]:
    """The product's canonical tariff-level key as a function of time."""

    def _level_at(when: datetime) -> str | None:
        return current_rate(product, when)[1]

    return _level_at


def localized(attributes: dict, hass: HomeAssistant) -> dict:
    """Replace the canonical `next_level_key` by the translated `next_level`
    shown to the user, like the `tariff_level` attribute."""
    attributes = dict(attributes)
    attributes["next_level"] = tariff_level_text(attributes.pop("next_level_key", None), hass)
    return attributes


def entry_price_at(coordinator, entry) -> tuple[PriceAt, list[dict]] | None:
    """The configured entry's combined price function plus the products
    whose windows mark its boundaries — automatic or manual entry alike.
    None while the configured products (or the chosen municipality, in a
    file that has a municipality section) are not in the fetched data."""
    data = coordinator.data or {}
    levies = entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)
    vat = entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)
    if CONF_MANUAL_HIGH_RP in entry.data:
        return (manual_price_at(data, levies, vat), [data]) if data else None
    energy = data.get("energy", {}).get(entry.data.get(CONF_ENERGY_PRODUCT))
    grid = data.get("grid", {}).get(entry.data.get(CONF_GRID_PRODUCT))
    if not energy or not grid:
        return None
    municipalities = data.get("municipalities", {})
    municipality_rp = 0.0
    if municipalities:
        levy = municipalities.get(entry.data.get(CONF_MUNICIPALITY, ""))
        if levy is None:
            return None
        municipality_rp = levy.get("rp_kwh") or 0.0
    return combined_price_at(energy, grid, municipality_rp, levies, vat), [energy, grid]


def entry_level_product(coordinator, entry) -> dict | None:
    """The product whose windows define the tariff level: the configured
    energy product, or the manual entry's own windows."""
    data = coordinator.data or {}
    if CONF_MANUAL_HIGH_RP in entry.data:
        return data or None
    return data.get("energy", {}).get(entry.data.get(CONF_ENERGY_PRODUCT)) or None


def level_segments(level_at: Callable[[datetime], str | None], products: list[dict], start: datetime, end: datetime):
    """Yields (start, end, level key) stretches of constant tariff level,
    beginning with the stretch in force at `start` (with its true start,
    found by walking forward from a week earlier — time-of-use schedules
    repeat weekly at most, apart from seasons) and ending with the one in
    force at `end` (with its true end). A flat tariff yields nothing."""
    t = start - timedelta(days=7)
    level = level_at(t)
    while t < end:
        change = next_change(level_at, products, t)
        if change is None:
            return
        if change > start:
            yield t, change, level
        t = change
        level = level_at(t)


def segment_containing(level_at: Callable[[datetime], str | None], products: list[dict], moment: datetime) -> tuple[datetime, datetime, str | None] | None:
    """The stretch of constant tariff level around `moment`, or None for a
    flat tariff."""
    for seg_start, seg_end, level in level_segments(level_at, products, moment, moment):
        if seg_start <= moment < seg_end:
            return seg_start, seg_end, level
    return None


def hourly_slots(price_at: PriceAt, start: datetime, end: datetime) -> list[dict]:
    """One entry per clock hour from `start` (inclusive) to `end`
    (exclusive), each holding the price in force at the start of that
    hour. Stepping in UTC keeps the slots right across a DST change (23 or
    25 entries on those days) while the printed times stay local."""
    tz = start.tzinfo
    slots: list[dict] = []
    t = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    while t < end_utc:
        nxt = min(t + timedelta(hours=1), end_utc)
        value, _level = price_at(t.astimezone(tz))
        slots.append({"start": t.astimezone(tz).isoformat(), "end": nxt.astimezone(tz).isoformat(), "value": value})
        t = nxt
    return slots


def next_change(value_at: Callable[[datetime], object], products: list[dict], now: datetime) -> datetime | None:
    """The first window boundary after `now` at which `value_at` returns
    something other than it does now, or None when it never does within
    the horizon (flat tariff). Boundaries come from the products' windows
    (plus midnight), so weekday- and month-restricted windows are covered
    without stepping through every minute."""
    current = value_at(now)
    t = now
    limit = now + _CHANGE_HORIZON
    while t < limit:
        t = next_switch_time(products, t)
        if value_at(t) != current:
            return t
    return None


def schedule_attributes(price_at: PriceAt, products: list[dict], now: datetime) -> dict:
    """The attributes shared by the combined-price sensors: today's and
    tomorrow's hourly curve, today's extremes and average, the price one
    hour from now, and the next price change (moment, price, level key —
    the caller translates the level)."""
    today = dt_util.start_of_local_day(now)
    tomorrow = dt_util.start_of_local_day(now + timedelta(days=1))
    day_after = dt_util.start_of_local_day(now + timedelta(days=2))
    raw_today = hourly_slots(price_at, today, tomorrow)
    raw_tomorrow = hourly_slots(price_at, tomorrow, day_after)
    values = [s["value"] for s in raw_today if s["value"] is not None]
    change = next_change(price_at, products, now)
    next_value, next_level = price_at(change) if change else (None, None)
    return {
        "raw_today": raw_today,
        "raw_tomorrow": raw_tomorrow,
        "min_today": min(values) if values else None,
        "max_today": max(values) if values else None,
        "avg_today": round(sum(values) / len(values), 5) if values else None,
        "price_next_hour": price_at(now + timedelta(hours=1))[0],
        "next_change": change.isoformat() if change else None,
        "next_price": next_value,
        "next_level_key": next_level,
    }


def level_change_attributes(level_at: Callable[[datetime], str | None], products: list[dict], now: datetime) -> dict:
    """`next_change` / `next_level_key` for the tariff-level entities: the
    next moment the level itself changes (the caller translates the key)."""
    change = next_change(level_at, products, now)
    return {
        "next_change": change.isoformat() if change else None,
        "next_level_key": level_at(change) if change else None,
    }
