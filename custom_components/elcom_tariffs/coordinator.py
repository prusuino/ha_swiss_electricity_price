"""DataUpdateCoordinator for a Swiss grid operator's machine-readable tariff file.

Every Swiss grid operator's tariff file follows the same standardized format
(the VSE API Definition), so this parsing logic works for any operator, not
just one specific utility — only the resolved download URL differs per
operator, see directory.py.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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


def _time_in_window(now_time, from_str: str, to_str: str) -> bool:
    if from_str == "00:00" and to_str == "00:00":
        return True
    fh, fm = (int(x) for x in from_str.split(":"))
    th, tm = (int(x) for x in to_str.split(":"))
    from_min, to_min = fh * 60 + fm, th * 60 + tm
    now_min = now_time.hour * 60 + now_time.minute
    if from_min <= to_min:
        return from_min <= now_min < to_min
    return now_min >= from_min or now_min < to_min


def _current_window(windows: list[dict], now) -> dict | None:
    weekday = WEEKDAY_ABBR[now.weekday()]
    month = MONTH_ABBR[now.month - 1]
    for w in windows:
        weekdays = w.get("weekdays")
        if weekdays and weekday not in weekdays:
            continue
        months = w.get("months")
        if months and month not in months:
            continue
        if _time_in_window(now.time(), w["from"], w["to"]):
            return w
    return None


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


class ElcomTariffCoordinator(DataUpdateCoordinator[dict]):
    """Resolves the operator's tariff file URL (periodically, in case it
    changes) and fetches/parses the tariff data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self._entry = entry

    async def _async_refresh_registered_url(self) -> None:
        """Re-check ElCom's registry for this operator's current-year
        registration on every cycle — operators re-report annually, so the
        registered URL itself can change (e.g. switching hosting), not just
        the file it points to. Skipped entirely for a user-supplied manual
        URL override, which is never auto-corrected."""
        if self._entry.data.get(CONF_URL_IS_MANUAL):
            return
        try:
            current_registered = await async_fetch_operator_url(
                self.hass, self._entry.data[CONF_OPERATOR_ID]
            )
        except Exception:
            return  # directory temporarily unreachable — keep using the existing registration
        if not current_registered or current_registered == self._entry.data[CONF_TARIFF_URL]:
            return
        try:
            resolved = await async_resolve_tariff_url(self.hass, current_registered)
            await async_fetch_raw_tariffs(self.hass, resolved)
        except Exception:
            return  # newly registered link doesn't actually work (yet) — keep the old one
        _LOGGER.info(
            "Operator's ElCom registration changed, switching to the new registered URL: %s",
            current_registered,
        )
        self.hass.config_entries.async_update_entry(
            self._entry, data={**self._entry.data, CONF_TARIFF_URL: current_registered}
        )

    async def _async_update_data(self) -> dict:
        await self._async_refresh_registered_url()

        # Always re-discover the actual tariff-file link fresh on every
        # cycle rather than caching a once-resolved URL — the same
        # always-rediscover approach used for single-utility integrations.
        # This matters even when the registered URL itself hasn't changed:
        # an "auto-detect" operator's page can keep the same address while
        # the .json file it links to gets swapped for a new year, and a
        # cached link would just keep re-fetching stale content forever
        # without ever failing.
        registered_url = self._entry.data[CONF_TARIFF_URL]
        try:
            json_url = await async_resolve_tariff_url(self.hass, registered_url)
            raw = await async_fetch_raw_tariffs(self.hass, json_url)
        except Exception as err:
            raise UpdateFailed(f"Operator tariff data unreachable: {err}") from err

        now = dt_util.now()
        energy_tariffs: dict[str, dict] = {}
        grid_tariffs: dict[str, dict] = {}
        metering_tariffs: dict[str, dict] = {}
        municipalities: dict[str, dict] = {}

        for t in raw.get("tariffs", []):
            ttype = t.get("tariffType")
            name = t.get("tariffName", "")
            if ttype == "electricity":
                energy_tariffs[name] = self._parse_time_tariff(t, now)
            elif ttype == "grid":
                grid_tariffs[name] = self._parse_time_tariff(t, now)
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
                    municipalities[mname] = {
                        "rp_kwh": round(energy[0]["price"] * 100, 3) if energy else 0.0,
                        "base_price_chf": base.get("price", 0),
                        "valid_from": t.get("startDate"),
                        "valid_to": t.get("endDate"),
                    }

        if not energy_tariffs:
            raise UpdateFailed("No energy tariffs found in the operator's tariff data")

        # The tariff file's own validity period (startDate/endDate of its
        # electricity tariffs), NOT to be confused with any date embedded in
        # the file's name — operators commonly publish next year's tariffs
        # months in advance, so a file named e.g. "20250807_..." can (and
        # for eug does) contain tariffs valid from 2026-01-01, not 2025.
        first_electricity = next(
            (t for t in raw.get("tariffs", []) if t.get("tariffType") == "electricity"), None
        )

        return {
            "dso_name": raw.get("dsoName"),
            "registered_url": registered_url,
            "tariff_url": json_url,
            "last_checked": now.isoformat(),
            "tariff_valid_from": first_electricity.get("startDate") if first_electricity else None,
            "tariff_valid_to": first_electricity.get("endDate") if first_electricity else None,
            "energy": energy_tariffs,
            "grid": grid_tariffs,
            "metering": metering_tariffs,
            "municipalities": municipalities,
        }

    @staticmethod
    def _parse_time_tariff(t: dict, now) -> dict:
        windows = t.get("prices", {}).get("energy", [])
        prices = [w["price"] for w in windows]
        active = _current_window(windows, now)
        schedule = [
            {
                "from": w["from"],
                "to": w["to"],
                "weekdays": w.get("weekdays") or ["all"],
                "rp_kwh": round(w["price"] * 100, 3),
                "level": _price_level(prices, w["price"]),
            }
            for w in windows
        ]
        base = t.get("prices", {}).get("base", {})
        distinct = sorted(set(prices))
        high_rp = round(max(prices) * 100, 3) if len(distinct) >= 2 else None
        low_rp = round(min(prices) * 100, 3) if len(distinct) >= 2 else None
        return {
            "current_rp_kwh": round(active["price"] * 100, 3) if active else None,
            "current_level": _price_level(prices, active["price"]) if active else None,
            "high_rp_kwh": high_rp,
            "low_rp_kwh": low_rp,
            "base_price_chf_month": base.get("price", 0),
            "customer_type": t.get("customerType"),
            "valid_from": t.get("startDate"),
            "valid_to": t.get("endDate"),
            "schedule": schedule,
        }


class ElcomManualTariffCoordinator(DataUpdateCoordinator[dict]):
    """Computes the current tariff level from manually entered high/low
    rates and a time window — pure local time math, no network fetch. Used
    when neither the operator's registered link nor a user-supplied URL
    could be resolved during setup."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=1))
        self._entry = entry
        self._last_link_check: object | None = None

    async def _async_check_operator_link(self) -> None:
        """Once a day, check whether the operator now has a working tariff
        file (its own link is still tried directly here — a manual entry has
        no stored URL of its own to refresh). If one is found, raise a
        repair issue suggesting the user switch to full automatic tariffs —
        not done silently, since municipality/customer tariff still need to
        be picked, which only the user can do."""
        today = dt_util.now().date()
        if self._last_link_check == today:
            return
        self._last_link_check = today

        try:
            registered_url = await async_fetch_operator_url(self.hass, self._entry.data[CONF_OPERATOR_ID])
            if not registered_url:
                return
            resolved_url = await async_resolve_tariff_url(self.hass, registered_url)
            raw = await async_fetch_raw_tariffs(self.hass, resolved_url)
            if not any(t.get("tariffType") == "electricity" for t in raw.get("tariffs", [])):
                return
        except Exception:
            return

        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"manual_link_available_{self._entry.entry_id}",
            is_fixable=False,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="manual_link_available",
            translation_placeholders={"operator": self._entry.data[CONF_OPERATOR_NAME]},
        )

    async def _async_update_data(self) -> dict:
        await self._async_check_operator_link()
        data = self._entry.data
        high_rp = data[CONF_MANUAL_HIGH_RP]
        low_rp = data.get(CONF_MANUAL_LOW_RP)
        grid_fee = data.get(CONF_MANUAL_GRID_FEE_CHF)

        if low_rp is None:
            return {
                "current_rp_kwh": high_rp,
                "current_level": "flat",
                "high_rp_kwh": high_rp,
                "low_rp_kwh": None,
                "grid_fee_chf_month": grid_fee,
                "schedule": [],
            }

        high_from = data.get(CONF_MANUAL_HIGH_FROM, DEFAULT_MANUAL_HIGH_FROM)
        high_to = data.get(CONF_MANUAL_HIGH_TO, DEFAULT_MANUAL_HIGH_TO)
        high_weekdays = data.get(CONF_MANUAL_HIGH_WEEKDAYS, DEFAULT_MANUAL_HIGH_WEEKDAYS)
        now = dt_util.now()
        in_window = WEEKDAY_ABBR[now.weekday()] in high_weekdays and _time_in_window(
            now.time(), high_from, high_to
        )
        return {
            "current_rp_kwh": high_rp if in_window else low_rp,
            "current_level": "high" if in_window else "low",
            "high_rp_kwh": high_rp,
            "low_rp_kwh": low_rp,
            "grid_fee_chf_month": grid_fee,
            "schedule": [
                {"from": high_from, "to": high_to, "weekdays": high_weekdays, "rp_kwh": high_rp, "level": "high"}
            ],
        }
