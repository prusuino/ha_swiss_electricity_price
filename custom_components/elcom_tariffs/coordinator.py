"""DataUpdateCoordinator for a Swiss grid operator's machine-readable tariff file.

Every Swiss grid operator's tariff file follows the same standardized format
(the VSE API Definition), so this parsing logic works for any operator, not
just one specific utility — only the resolved download URL differs per
operator, see directory.py.

Time-of-day handling: the coordinator only fetches and parses the tariff
file (every 24 h — tariffs change once a year). Which time window is
currently active is NOT frozen into the fetched data; the entities resolve
it against the current time on every read via current_rate(), and the
coordinator arms a point-in-time timer at the next window boundary (e.g.
22:00 and 06:00) that pushes the already-fetched data to every entity
again, so a high/low switch shows up at the minute it happens rather than
at the next 24 h fetch.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MANUAL_GRID_FEE_CHF,
    CONF_MANUAL_HIGH_FROM,
    CONF_MANUAL_HIGH_RP,
    CONF_MANUAL_HIGH_TO,
    CONF_MANUAL_HIGH_WEEKDAYS,
    CONF_MANUAL_LOW_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    DEFAULT_MANUAL_HIGH_FROM,
    DEFAULT_MANUAL_HIGH_TO,
    DEFAULT_MANUAL_HIGH_WEEKDAYS,
    DOMAIN,
    UPDATE_INTERVAL_HOURS,
)
from .directory import async_fetch_operator_url, async_resolve_tariff_url

_LOGGER = logging.getLogger(__name__)

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAY_ABBR = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

_MINUTES_PER_DAY = 24 * 60


def _parse_date(value: str | None):
    """Date part of an ISO startDate/endDate ("2027-01-01" or
    "2027-01-01T00:00:00"); None when missing or unparsable."""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date() if value else None
    except (TypeError, ValueError):
        return None


def _select_current_tariffs(tariffs: list[dict], today) -> list[dict]:
    """Keep, per product, only the tariff entry that is valid today.

    Operators such as BKW publish next year's tariffs next to the current
    ones in the same file, with identical tariffName. Without this filter
    the entry that happens to come first/last in the file wins. Per product
    (type + name + voltage level + municipality/canton) the entry with
    startDate <= today <= endDate is used; if none is valid today, the
    most recent one that already started, else the earliest upcoming one.
    Entries without usable dates are always kept."""

    def key(t: dict) -> tuple:
        prices = t.get("prices") or {}
        places = tuple(
            sorted(
                str(m.get("municipalityNumber") or m.get("municipalityName"))
                for m in prices.get("municipalityTaxes", []) or []
            )
            + sorted(str(c.get("cantonName")) for c in prices.get("cantonalTaxes", []) or [])
        )
        return (t.get("tariffType"), t.get("tariffName"), t.get("customerVoltageLevel"), places)

    groups: dict[tuple, list[dict]] = {}
    for t in tariffs:
        groups.setdefault(key(t), []).append(t)

    chosen: set[int] = set()
    for group in groups.values():
        dated = [(t, _parse_date(t.get("startDate")), _parse_date(t.get("endDate"))) for t in group]
        if all(start is None for _, start, _ in dated):
            chosen.update(id(t) for t in group)
            continue
        valid = [
            t for t, start, end in dated
            if start is not None and start <= today and (end is None or today <= end)
        ]
        if not valid:
            started = sorted(
                ((start, t) for t, start, _ in dated if start is not None and start <= today),
                key=lambda x: x[0],
            )
            if started:
                valid = [started[-1][1]]
            else:
                upcoming = sorted(
                    ((start, t) for t, start, _ in dated if start is not None),
                    key=lambda x: x[0],
                )
                valid = [upcoming[0][1]]
        # entries without dates in a mixed group are kept as before
        chosen.update(id(t) for t in valid)
        chosen.update(id(t) for t, start, _ in dated if start is None)
    return [t for t in tariffs if id(t) in chosen]


def _is_secondary_meter_fee(t: dict) -> bool:
    """True for concession fees that only apply to secondary meters
    ("Zweitzähler" / "compteurs secondaires"). BKW lists them next to the
    regular concession fee for the same municipality; they must not
    override it."""
    text = f"{t.get('tariffName', '')} {t.get('comment', '')}".lower()
    return "zweitzähler" in text or "compteurs secondaires" in text


def manual_issue_id(entry: ConfigEntry) -> str:
    """Repair-issue id raised for a manual entry once the operator's own
    tariff file starts working (see ElcomManualTariffCoordinator)."""
    return f"manual_link_available_{entry.entry_id}"


def _parse_hhmm(value: str) -> int:
    """HH:MM -> minutes since midnight. "24:00" is accepted as end of day
    (1440), which the window math below treats like "00:00"."""
    h, m = (int(x) for x in value.split(":"))
    return h * 60 + m


def _time_in_window(now_min: int, from_min: int, to_min: int) -> bool:
    if from_min == 0 and to_min == 0:
        return True
    if from_min <= to_min:
        return from_min <= now_min < to_min
    return now_min >= from_min or now_min < to_min


def _current_window(windows: list[dict], now: datetime) -> dict | None:
    weekday = WEEKDAY_ABBR[now.weekday()]
    month = MONTH_ABBR[now.month - 1]
    now_min = now.hour * 60 + now.minute
    for w in windows:
        weekdays = w.get("weekdays")
        if weekdays and weekday not in weekdays:
            continue
        months = w.get("months")
        if months and month not in months:
            continue
        if _time_in_window(now_min, w["from_min"], w["to_min"]):
            return w
    return None


def current_rate(product: dict, now: datetime) -> tuple[float | None, str | None]:
    """The (Rp/kWh, level key) of a parsed product that applies at `now`,
    or (None, None) when none of its windows covers that moment. Evaluated
    by the entities on every read — never cached in coordinator data."""
    active = _current_window(product.get("windows", []), now)
    if active is None:
        return None, None
    return active["rp_kwh"], active["level"]


def next_switch_time(products: Iterable[dict], now: datetime) -> datetime:
    """The next wall-clock moment at which the active window of any of the
    given products can change: the earliest from/to boundary after `now`.
    Midnight is always included, since weekday- or month-restricted windows
    switch there without any from/to boundary of their own."""
    boundaries = {0}
    for product in products:
        for w in product.get("windows", []):
            boundaries.add(w["from_min"] % _MINUTES_PER_DAY)
            boundaries.add(w["to_min"] % _MINUTES_PER_DAY)
    candidates = []
    for minute in boundaries:
        candidate = now.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    return min(candidates)


def _price_level(all_prices: list[float], price: float) -> str:
    """Canonical (language-independent) tariff-level key. Translate for
    display via localization.tariff_level_text."""
    if len(set(all_prices)) <= 1:
        return "flat"
    if price >= max(all_prices):
        return "high"
    if price <= min(all_prices):
        return "low"
    return "medium"


async def async_fetch_raw_tariffs(hass: HomeAssistant, json_url: str) -> dict:
    """Fetch the tariff JSON from an already-resolved direct URL."""
    session = async_get_clientsession(hass)
    async with session.get(json_url, timeout=25) as resp:
        resp.raise_for_status()
        return await resp.json(content_type=None)


class _ElcomBaseCoordinator(DataUpdateCoordinator[dict]):
    """Shared by both coordinators: per-entry naming, and the timer that
    re-pushes the fetched data to the entities at every window boundary."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, update_interval: timedelta) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=update_interval,
            config_entry=entry,
        )
        self._unsub_switch_timer: CALLBACK_TYPE | None = None

    async def _async_fetch_data(self) -> dict:
        raise NotImplementedError

    def _switch_products(self, data: dict) -> list[dict]:
        """The parsed products whose windows decide when the timer fires."""
        raise NotImplementedError

    async def _async_update_data(self) -> dict:
        data = await self._async_fetch_data()
        self._async_arm_switch_timer(data)
        return data

    @callback
    def _async_arm_switch_timer(self, data: dict) -> None:
        self.async_cancel_switch_timer()
        when = next_switch_time(self._switch_products(data), dt_util.now())
        self._unsub_switch_timer = async_track_point_in_time(self.hass, self._async_switch_fired, when)

    @callback
    def _async_switch_fired(self, _now: datetime) -> None:
        """A window boundary was reached: rewrite every entity's state from
        the already-fetched data (no network), then arm the next one."""
        self._unsub_switch_timer = None
        self.async_update_listeners()
        if self.data is not None:
            self._async_arm_switch_timer(self.data)

    @callback
    def async_cancel_switch_timer(self) -> None:
        """Registered with entry.async_on_unload so no timer outlives the entry."""
        if self._unsub_switch_timer is not None:
            self._unsub_switch_timer()
            self._unsub_switch_timer = None


class ElcomTariffCoordinator(_ElcomBaseCoordinator):
    """Resolves the operator's tariff file URL (periodically, in case it
    changes) and fetches/parses the tariff data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, timedelta(hours=UPDATE_INTERVAL_HOURS))

    def _switch_products(self, data: dict) -> list[dict]:
        return [*data.get("energy", {}).values(), *data.get("grid", {}).values()]

    async def _async_refresh_registered_url(self) -> None:
        """Re-check ElCom's registry for this operator's current-year
        registration on every cycle — operators re-report annually, so the
        registered URL itself can change (e.g. switching hosting), not just
        the file it points to. Skipped entirely for a user-supplied manual
        URL override, which is never auto-corrected."""
        entry = self.config_entry
        if entry.data.get(CONF_URL_IS_MANUAL):
            return
        try:
            current_registered = await async_fetch_operator_url(self.hass, entry.data[CONF_OPERATOR_ID])
        except Exception as err:  # noqa: BLE001 - any failure here means "keep the existing registration"
            _LOGGER.debug("ElCom registry check skipped, directory unreachable: %s", err)
            return
        if not current_registered or current_registered == entry.data[CONF_TARIFF_URL]:
            return
        try:
            resolved = await async_resolve_tariff_url(self.hass, current_registered)
            await async_fetch_raw_tariffs(self.hass, resolved)
        except Exception as err:  # noqa: BLE001 - newly registered link doesn't work (yet), keep the old one
            _LOGGER.debug("Newly registered URL %s not usable yet, keeping the old one: %s", current_registered, err)
            return
        _LOGGER.info(
            "Operator's ElCom registration changed, switching to the new registered URL: %s",
            current_registered,
        )
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_TARIFF_URL: current_registered}
        )

    async def _async_fetch_data(self) -> dict:
        await self._async_refresh_registered_url()

        # Always re-discover the actual tariff-file link fresh on every
        # cycle rather than caching a once-resolved URL — the same
        # always-rediscover approach used for single-utility integrations.
        # This matters even when the registered URL itself hasn't changed:
        # an "auto-detect" operator's page can keep the same address while
        # the .json file it links to gets swapped for a new year, and a
        # cached link would just keep re-fetching stale content forever
        # without ever failing.
        registered_url = self.config_entry.data[CONF_TARIFF_URL]
        try:
            json_url = await async_resolve_tariff_url(self.hass, registered_url)
            raw = await async_fetch_raw_tariffs(self.hass, json_url)
        except Exception as err:
            # aiohttp's ClientResponseError carries the HTTP status in its
            # text, so a 403 (bot protection) reads differently from a
            # timeout or a DNS failure in the log.
            raise UpdateFailed(f"Operator tariff data unreachable: {err}") from err

        # The whole parse runs inside the try: an operator publishing a
        # slightly non-conformant tariff file (a record missing a price,
        # a window without from/to, "tariffs" not being a list) would
        # otherwise raise straight out of the update and show up as a
        # generic "Unexpected error fetching ..." traceback instead of the
        # clean unavailable state the README promises. UpdateFailed itself
        # is none of these types, so the deliberate "no energy tariffs"
        # raise below passes through unwrapped.
        try:
            energy_tariffs: dict[str, dict] = {}
            grid_tariffs: dict[str, dict] = {}
            metering_tariffs: dict[str, dict] = {}
            municipalities: dict[str, dict] = {}
            secondary_municipalities: dict[str, dict] = {}

            current_tariffs = _select_current_tariffs(
                raw.get("tariffs", []), dt_util.now().date()
            )

            for t in current_tariffs:
                ttype = t.get("tariffType")
                name = t.get("tariffName", "")
                if ttype == "electricity":
                    energy_tariffs[name] = self._parse_time_tariff(t)
                elif ttype == "grid":
                    grid_tariffs[name] = self._parse_time_tariff(t)
                elif ttype == "metering":
                    base = t.get("prices", {}).get("base", {})
                    metering_tariffs[name] = {
                        "price_chf": base.get("price"),
                        "unit": base.get("priceUnit"),
                        "customer_type": t.get("customerType"),
                        "valid_from": t.get("startDate"),
                        "valid_to": t.get("endDate"),
                    }
                elif ttype == "regional_fees":
                    for m in t.get("prices", {}).get("municipalityTaxes", []):
                        mname = m.get("municipalityName")
                        if not mname:
                            continue
                        energy = m.get("municipalityEnergy", [])
                        base = m.get("municipalityBase", {})
                        target = (
                            secondary_municipalities
                            if _is_secondary_meter_fee(t)
                            else municipalities
                        )
                        target[mname] = {
                            "rp_kwh": round(energy[0]["price"] * 100, 3) if energy else 0.0,
                            "base_price_chf": base.get("price", 0),
                            "valid_from": t.get("startDate"),
                            "valid_to": t.get("endDate"),
                        }

            # Secondary-meter fees are only used for municipalities that have
            # no regular concession fee at all.
            for mname, levy in secondary_municipalities.items():
                municipalities.setdefault(mname, levy)

            if not energy_tariffs:
                raise UpdateFailed("No energy tariffs found in the operator's tariff data")

            # The tariff file's own validity period (startDate/endDate of its
            # electricity tariffs), NOT to be confused with any date embedded in
            # the file's name — operators commonly publish next year's tariffs
            # months in advance, so a file named e.g. "20250807_..." can (and
            # for eug does) contain tariffs valid from 2026-01-01, not 2025.
            first_electricity = next(
                (t for t in current_tariffs if t.get("tariffType") == "electricity"), None
            )

            return {
                "dso_name": raw.get("dsoName"),
                "registered_url": registered_url,
                "tariff_url": json_url,
                "last_checked": dt_util.now().isoformat(),
                "tariff_valid_from": first_electricity.get("startDate") if first_electricity else None,
                "tariff_valid_to": first_electricity.get("endDate") if first_electricity else None,
                "energy": energy_tariffs,
                "grid": grid_tariffs,
                "metering": metering_tariffs,
                "municipalities": municipalities,
            }
        # ValueError covers time windows whose from/to are not HH:MM strings
        # (int() in _parse_hhmm raises it).
        except (KeyError, AttributeError, TypeError, IndexError, ValueError) as err:
            raise UpdateFailed(
                f"Operator tariff data is malformed ({type(err).__name__}: {err})"
            ) from err

    @staticmethod
    def _parse_time_tariff(t: dict) -> dict:
        """Parse one energy/grid product. `windows` is the internal,
        pre-parsed list current_rate() evaluates at read time; `schedule`
        is the same information in the shape exposed as an attribute."""
        raw_windows = t.get("prices", {}).get("energy", [])
        prices = [w["price"] for w in raw_windows]
        windows = [
            {
                "from": w["from"],
                "to": w["to"],
                "from_min": _parse_hhmm(w["from"]),
                "to_min": _parse_hhmm(w["to"]),
                "weekdays": w.get("weekdays") or None,
                "months": w.get("months") or None,
                "rp_kwh": round(w["price"] * 100, 3),
                "level": _price_level(prices, w["price"]),
            }
            for w in raw_windows
        ]
        schedule = [
            {
                "from": w["from"],
                "to": w["to"],
                "weekdays": w["weekdays"] or ["all"],
                "rp_kwh": w["rp_kwh"],
                "level": w["level"],
            }
            for w in windows
        ]
        base = t.get("prices", {}).get("base", {})
        distinct = sorted(set(prices))
        high_rp = round(max(prices) * 100, 3) if len(distinct) >= 2 else None
        low_rp = round(min(prices) * 100, 3) if len(distinct) >= 2 else None
        return {
            "windows": windows,
            "high_rp_kwh": high_rp,
            "low_rp_kwh": low_rp,
            "base_price_chf_month": base.get("price", 0),
            "customer_type": t.get("customerType"),
            "valid_from": t.get("startDate"),
            "valid_to": t.get("endDate"),
            "schedule": schedule,
        }


class ElcomManualTariffCoordinator(_ElcomBaseCoordinator):
    """Builds the time windows from manually entered high/low rates —
    pure local math, no network fetch for the data itself. Used when
    neither the operator's registered link nor a user-supplied URL could be
    resolved during setup. The current level is resolved by the entities
    at read time and pushed at every window boundary, exactly like the
    automatic path, so no polling is needed."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, timedelta(hours=UPDATE_INTERVAL_HOURS))

    def _switch_products(self, data: dict) -> list[dict]:
        return [data]

    async def _async_check_operator_link(self) -> None:
        """Once per refresh cycle (24 h), check whether the operator now has
        a working tariff file (its own link is still tried directly here — a
        manual entry has no stored URL of its own to refresh). If one is
        found, raise a repair issue suggesting the user switch to full
        automatic tariffs — not done silently, since municipality/customer
        tariff still need to be picked, which only the user can do. If the
        link stops working again, the issue is withdrawn."""
        entry = self.config_entry
        try:
            registered_url = await async_fetch_operator_url(self.hass, entry.data[CONF_OPERATOR_ID])
            usable = False
            if registered_url:
                resolved_url = await async_resolve_tariff_url(self.hass, registered_url)
                raw = await async_fetch_raw_tariffs(self.hass, resolved_url)
                usable = any(t.get("tariffType") == "electricity" for t in raw.get("tariffs", []))
        except Exception as err:  # noqa: BLE001 - "still no working link" is the expected outcome here
            _LOGGER.debug("Operator link still not usable for manual entry %s: %s", entry.title, err)
            usable = False

        if not usable:
            ir.async_delete_issue(self.hass, DOMAIN, manual_issue_id(entry))
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            manual_issue_id(entry),
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="manual_link_available",
            translation_placeholders={"operator": entry.data[CONF_OPERATOR_NAME]},
        )

    async def _async_fetch_data(self) -> dict:
        if self.data is None:
            # First refresh (entry setup): run the link check in the
            # background so up to ~80 s of network timeouts never delay the
            # entities coming up — the data itself is local anyway.
            self.config_entry.async_create_background_task(
                self.hass, self._async_check_operator_link(), name=f"{DOMAIN} manual link check"
            )
        else:
            await self._async_check_operator_link()

        data = self.config_entry.data
        high_rp = data[CONF_MANUAL_HIGH_RP]
        low_rp = data.get(CONF_MANUAL_LOW_RP)
        grid_fee = data.get(CONF_MANUAL_GRID_FEE_CHF)

        if low_rp is None:
            return {
                "windows": [
                    {
                        "from": "00:00",
                        "to": "00:00",
                        "from_min": 0,
                        "to_min": 0,
                        "weekdays": None,
                        "months": None,
                        "rp_kwh": high_rp,
                        "level": "flat",
                    }
                ],
                "high_rp_kwh": high_rp,
                "low_rp_kwh": None,
                "grid_fee_chf_month": grid_fee,
                "schedule": [],
            }

        high_from = data.get(CONF_MANUAL_HIGH_FROM, DEFAULT_MANUAL_HIGH_FROM)
        high_to = data.get(CONF_MANUAL_HIGH_TO, DEFAULT_MANUAL_HIGH_TO)
        high_weekdays = data.get(CONF_MANUAL_HIGH_WEEKDAYS, DEFAULT_MANUAL_HIGH_WEEKDAYS)
        try:
            high_window = {
                "from": high_from,
                "to": high_to,
                "from_min": _parse_hhmm(high_from),
                "to_min": _parse_hhmm(high_to),
                "weekdays": list(high_weekdays) or None,
                "months": None,
                "rp_kwh": high_rp,
                "level": "high",
            }
        except (ValueError, AttributeError, TypeError) as err:
            raise UpdateFailed(f"Manual tariff times are malformed ({err})") from err
        # The high window is matched first; the catch-all low window covers
        # every other moment (all weekdays, whole day).
        low_window = {
            "from": "00:00",
            "to": "00:00",
            "from_min": 0,
            "to_min": 0,
            "weekdays": None,
            "months": None,
            "rp_kwh": low_rp,
            "level": "low",
        }
        return {
            "windows": [high_window, low_window],
            "high_rp_kwh": high_rp,
            "low_rp_kwh": low_rp,
            "grid_fee_chf_month": grid_fee,
            "schedule": [
                {"from": high_from, "to": high_to, "weekdays": high_weekdays, "rp_kwh": high_rp, "level": "high"}
            ],
        }
