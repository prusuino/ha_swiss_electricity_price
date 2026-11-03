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

Validity periods: operators commonly keep more than one year's tariffs in
one file (next year's appended to this year's). Only the records valid
today are parsed, see tariff_period.py; when that validity period ends
(New Year, typically) the file is re-read right away instead of at the
next 24 h fetch.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_GRID_FEE_CHF,
    CONF_MANUAL_HIGH_FROM,
    CONF_MANUAL_HIGH_RP,
    CONF_MANUAL_HIGH_TO,
    CONF_MANUAL_HIGH_WEEKDAYS,
    CONF_MANUAL_LOW_RP,
    CONF_MUNICIPALITY,
    CONF_NEXT_PERIOD_NOTIFIED,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    DEFAULT_MANUAL_HIGH_FROM,
    DEFAULT_MANUAL_HIGH_TO,
    DEFAULT_MANUAL_HIGH_WEEKDAYS,
    DOMAIN,
    OPERATOR_REQUEST_HEADERS,
    UPDATE_INTERVAL_HOURS,
)
from .directory import async_fetch_operator_url, async_resolve_tariff_url
from .localization import t
from .tariff_period import (
    is_secondary_meter_fee,
    select_current_tariffs,
    select_upcoming_tariffs,
    tariff_validity_end,
)

_LOGGER = logging.getLogger(__name__)

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEKDAY_ABBR = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

_MINUTES_PER_DAY = 24 * 60

# The VSE format prescribes English two-letter weekday abbreviations, but
# a share of operators writes German ones — "So" for Sunday above all,
# found in 6 of the first 22 files surveyed in 11/2026 — and the file
# then has no window at all on those days, so the price fell back to the
# levies alone every Sunday. Spellings per language, canonical index 0..6
# (Monday..Sunday); "Di" and "Do" are ambiguous (German Tuesday/Thursday,
# French Sunday / Italian Sunday) and are resolved by the language that
# recognises most of the product's weekday tokens.
_WEEKDAY_SPELLINGS: dict[str, dict[str, int]] = {
    "en": {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6,
           "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6},
    "de": {"mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6},
    "fr": {"lu": 0, "ma": 1, "me": 2, "je": 3, "ve": 4, "sa": 5, "di": 6},
    "it": {"lu": 0, "ma": 1, "me": 2, "gi": 3, "ve": 4, "sa": 5, "do": 6},
}
_MONTH_SPELLINGS: dict[str, int] = {
    **{m.lower(): i for i, m in enumerate(MONTH_ABBR)},
    "mär": 2, "mrz": 2, "mai": 4, "okt": 9, "dez": 11,  # de
    "fév": 1, "fev": 1, "avr": 3, "juin": 5, "juil": 6, "aoû": 7, "aou": 7, "déc": 11,  # fr
    "gen": 0, "mag": 4, "giu": 5, "lug": 6, "ago": 7, "set": 8, "ott": 9, "dic": 11,  # it
}


def _weekday_tokens(raw: list | None) -> list[str]:
    return [str(t).strip().rstrip(".").lower() for t in (raw or []) if str(t).strip()]


def weekday_language(windows: list[dict]) -> str:
    """The spelling that recognises most of the weekday tokens across all
    of a product's windows — judged over the whole product, since a single
    window such as ["Sa", "Di"] is ambiguous on its own. Ties go to
    English, then German (dict order)."""
    tokens = [t for w in windows for t in _weekday_tokens(w.get("weekdays"))]
    scores = {lang: sum(1 for t in tokens if t in table) for lang, table in _WEEKDAY_SPELLINGS.items()}
    return max(_WEEKDAY_SPELLINGS, key=lambda lang: scores[lang])


def normalize_weekdays(raw: list | None, language: str = "en") -> list[str] | None:
    """Weekday tokens as the file spells them -> canonical WEEKDAY_ABBR
    list, or None when the window applies to every day (empty, "all", or
    a spelling nobody recognises — a window must not silently vanish).
    `language` (see weekday_language) decides the ambiguous tokens."""
    tokens = _weekday_tokens(raw)
    if not tokens or any(t in ("all", "alle", "tous", "tutti", "*") for t in tokens):
        return None
    preferred = language if language in _WEEKDAY_SPELLINGS else "en"
    days: set[int] = set()
    for token in tokens:
        if token in _WEEKDAY_SPELLINGS[preferred]:
            days.add(_WEEKDAY_SPELLINGS[preferred][token])
            continue
        for table in _WEEKDAY_SPELLINGS.values():
            if token in table:
                days.add(table[token])
                break
        else:
            _LOGGER.debug("Unknown weekday spelling %r in tariff window; window applies to all days", token)
            return None
    return [WEEKDAY_ABBR[i] for i in sorted(days)]


def normalize_months(raw: list | None) -> list[str] | None:
    """Month tokens as the file spells them -> canonical MONTH_ABBR list,
    or None when the window applies all year (or a spelling is unknown)."""
    if not raw:
        return None
    months: set[int] = set()
    for token in raw:
        key = str(token).strip().rstrip(".").lower()
        index = _MONTH_SPELLINGS.get(key, _MONTH_SPELLINGS.get(key[:3]))
        if index is None:
            _LOGGER.debug("Unknown month spelling %r in tariff window; window applies all year", token)
            return None
        months.add(index)
    return [MONTH_ABBR[i] for i in sorted(months)] if len(months) < 12 else None


def manual_issue_id(entry: ConfigEntry) -> str:
    """Repair-issue id raised for a manual entry once the operator's own
    tariff file starts working (see ElcomManualTariffCoordinator)."""
    return f"manual_link_available_{entry.entry_id}"


def selection_issue_id(entry: ConfigEntry) -> str:
    """Repair-issue id raised when the chosen product or municipality is
    no longer in the operator's file (see _async_check_selection)."""
    return f"selection_missing_{entry.entry_id}"


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
    # No window covers this moment — the operator's file leaves a gap
    # (a seasonal set missing a season, a weekday set missing a day).
    # The product's lowest rate is the least wrong answer: off-peak is
    # what operators leave implicit, and a price of zero would be worse.
    return min(windows, key=lambda w: w["rp_kwh"]) if windows else None


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
    async with session.get(json_url, headers=OPERATOR_REQUEST_HEADERS, timeout=25) as resp:
        resp.raise_for_status()
        body = await resp.read()
    return decode_tariff_file(body)


def decode_tariff_file(body: bytes) -> dict:
    """Parse an operator's file. The format is JSON, hence UTF-8, but a
    few operators export it in a Windows code page (two Romandy utilities
    in the 11/2026 survey), which otherwise fails on the first accent."""
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        _LOGGER.warning("Tariff file is not UTF-8, reading it as Windows-1252")
        text = body.decode("cp1252", errors="replace")
    return json.loads(text)


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

    def _data_expired(self, data: dict) -> bool:
        """True when the fetched data no longer applies and has to be
        re-fetched instead of merely re-pushed to the entities."""
        return False

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
        if self.data is not None and self._data_expired(self.data):
            # The tariff period ended at this midnight: the records parsed
            # for it are stale now (next year's set is likely already in
            # the file). Re-fetch and re-select; the refresh re-arms the
            # timer when it completes.
            self.hass.async_create_task(self.async_request_refresh())
            return
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

    def _data_expired(self, data: dict) -> bool:
        period_end = data.get("tariff_period_end")
        return bool(period_end) and dt_util.now().date() > date.fromisoformat(period_end)

    async def _async_fetch_from_registration(self, registered_url: str) -> tuple[str, dict]:
        """Resolve a registered URL and fetch the file behind it. If that
        fails, fall back once to the URL the operator registered for NEXT
        year (if any, and if different): from autumn on, many operators
        have already reported next year's link — often the only one that
        still works once they have moved this year's file — and that file
        typically carries the current year's tariffs as well. The fallback
        is used for this fetch only; the stored registration is left alone
        and re-checked on the next cycle as usual."""
        try:
            json_url = await async_resolve_tariff_url(self.hass, registered_url)
            return json_url, await async_fetch_raw_tariffs(self.hass, json_url)
        except Exception as err:  # noqa: BLE001 - try next year's registration before giving up
            if self.config_entry.data.get(CONF_URL_IS_MANUAL):
                raise
            try:
                next_year_url = await async_fetch_operator_url(
                    self.hass, self.config_entry.data[CONF_OPERATOR_ID], dt_util.now().year + 1
                )
            except Exception as directory_err:  # noqa: BLE001 - the directory is down: report the original problem
                _LOGGER.debug("Next-year registration lookup failed: %s", directory_err)
                raise err from None
            if not next_year_url or next_year_url == registered_url:
                raise
            _LOGGER.debug(
                "Registered URL %s not usable (%s), trying next year's registration %s",
                registered_url,
                err,
                next_year_url,
            )
            json_url = await async_resolve_tariff_url(self.hass, next_year_url)
            return json_url, await async_fetch_raw_tariffs(self.hass, json_url)

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
            json_url, raw = await self._async_fetch_from_registration(registered_url)
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
            # Only the records valid today. Files holding this year's and
            # next year's tariffs under the same product names would
            # otherwise be merged by name, with the year that happens to
            # come last in the file winning per product.
            today = dt_util.now().date()
            tariffs, selection = select_current_tariffs(raw.get("tariffs", []), today)
            if selection in ("upcoming", "expired"):
                _LOGGER.warning(
                    "%s: no tariffs valid on %s in the operator's file, showing the %s ones instead",
                    self.config_entry.title,
                    today.isoformat(),
                    "next" if selection == "upcoming" else "most recent",
                )

            parsed = self._parse_tariff_records(tariffs)
            energy_tariffs = parsed["energy"]
            grid_tariffs = parsed["grid"]
            metering_tariffs = parsed["metering"]
            municipalities = parsed["municipalities"]

            if not energy_tariffs:
                raise UpdateFailed("No energy tariffs found in the operator's tariff data")

            # The nearest set beyond today's (next year's tariffs, published
            # months ahead): parsed the same way, for the next-period
            # preview sensor and the one-off notification. Only when
            # today's set is itself current — if the file holds nothing
            # valid today, "upcoming" IS what is being shown already.
            next_period = None
            if selection == "current":
                upcoming, upcoming_start = select_upcoming_tariffs(raw.get("tariffs", []), today)
                if upcoming:
                    upcoming_parsed = self._parse_tariff_records(upcoming)
                    if upcoming_parsed["energy"] or upcoming_parsed["grid"]:
                        next_period = {"valid_from": upcoming_start.isoformat(), **upcoming_parsed}

            # The tariff file's own validity period (startDate/endDate of its
            # electricity tariffs), NOT to be confused with any date embedded in
            # the file's name — operators commonly publish next year's tariffs
            # months in advance, so a file named e.g. "20250807_..." can (and
            # some do) contain tariffs valid from 2026-01-01, not 2025.
            first_electricity = next((t for t in tariffs if t.get("tariffType") == "electricity"), None)
            period_end = tariff_validity_end(tariffs)

            data = {
                "dso_name": raw.get("dsoName"),
                "registered_url": registered_url,
                "tariff_url": json_url,
                "last_checked": dt_util.now().isoformat(),
                "tariff_valid_from": first_electricity.get("startDate") if first_electricity else None,
                "tariff_valid_to": first_electricity.get("endDate") if first_electricity else None,
                "tariff_period_end": period_end.isoformat() if period_end else None,
                "tariff_selection": selection,
                "energy": energy_tariffs,
                "grid": grid_tariffs,
                "metering": metering_tariffs,
                "municipalities": municipalities,
                "next_period": next_period,
            }
            self._async_check_selection(data)
            self._async_notify_next_period(data)
            return data
        # ValueError covers time windows whose from/to are not HH:MM strings
        # (int() in _parse_hhmm raises it).
        except (KeyError, AttributeError, TypeError, IndexError, ValueError) as err:
            raise UpdateFailed(
                f"Operator tariff data is malformed ({type(err).__name__}: {err})"
            ) from err

    @callback
    def _async_check_selection(self, data: dict) -> None:
        """Raise (or withdraw) the repair issue telling the user that the
        energy/grid product or municipality chosen at setup is no longer in
        the operator's file — operators rename products or drop the
        municipality section between years, and the combined-price
        sensors are unavailable until the current names are chosen in the
        Configure dialog."""
        entry = self.config_entry
        missing: list[str] = []
        energy = entry.data.get(CONF_ENERGY_PRODUCT)
        grid = entry.data.get(CONF_GRID_PRODUCT)
        municipality = entry.data.get(CONF_MUNICIPALITY)
        if energy and energy not in data["energy"]:
            missing.append(energy)
        if grid and grid not in data["grid"]:
            missing.append(grid)
        if municipality and data["municipalities"] and municipality not in data["municipalities"]:
            missing.append(municipality)
        if not missing:
            ir.async_delete_issue(self.hass, DOMAIN, selection_issue_id(entry))
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            selection_issue_id(entry),
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="selection_missing",
            translation_placeholders={"operator": entry.data[CONF_OPERATOR_NAME], "missing": ", ".join(missing)},
        )

    @callback
    def _async_notify_next_period(self, data: dict) -> None:
        """Once per upcoming tariff period: a persistent notification with
        the change of the configured energy + grid rates. Remembered in
        the entry data by the period's start date so it is not repeated at
        every 24 h refresh, and only when both configured products exist
        in both sets (nothing to compare otherwise)."""
        entry = self.config_entry
        upcoming = data.get("next_period")
        if not upcoming or entry.data.get(CONF_NEXT_PERIOD_NOTIFIED) == upcoming["valid_from"]:
            return
        energy_name = entry.data.get(CONF_ENERGY_PRODUCT)
        grid_name = entry.data.get(CONF_GRID_PRODUCT)
        old_e, old_g = data["energy"].get(energy_name), data["grid"].get(grid_name)
        new_e, new_g = upcoming["energy"].get(energy_name), upcoming["grid"].get(grid_name)
        if not (old_e and old_g and new_e and new_g):
            return

        def level(product: dict, key: str) -> float:
            value = product.get(key)
            return value if value is not None else product["windows"][0]["rp_kwh"]

        old_high = level(old_e, "high_rp_kwh") + level(old_g, "high_rp_kwh")
        old_low = level(old_e, "low_rp_kwh") + level(old_g, "low_rp_kwh")
        new_high = level(new_e, "high_rp_kwh") + level(new_g, "high_rp_kwh")
        new_low = level(new_e, "low_rp_kwh") + level(new_g, "low_rp_kwh")
        valid_from = date.fromisoformat(upcoming["valid_from"])
        persistent_notification.async_create(
            self.hass,
            t(
                "next_period_notification",
                self.hass,
                operator=entry.data[CONF_OPERATOR_NAME],
                date=valid_from.strftime("%d.%m.%Y"),
                old_high=old_high,
                new_high=new_high,
                high_pct=(new_high - old_high) / old_high * 100 if old_high else 0.0,
                old_low=old_low,
                new_low=new_low,
                low_pct=(new_low - old_low) / old_low * 100 if old_low else 0.0,
                energy_product=energy_name,
                grid_product=grid_name,
            ),
            title=t("next_period_notification_title", self.hass, date=valid_from.strftime("%d.%m.%Y")),
            notification_id=f"{DOMAIN}_next_period_{entry.entry_id}",
        )
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_NEXT_PERIOD_NOTIFIED: upcoming["valid_from"]}
        )

    @staticmethod
    def _municipal_levy_rp(mname: str, value: float) -> float:
        """A municipal levy in Rp/kWh from the figure in the operator's
        file. The format wants CHF/kWh, and no Swiss municipal levy
        reaches 10 Rp/kWh. Seven operators in the 11/2026 survey publish
        figures such as 70, 85 or 230 instead of 0.0070, 0.0085 or 0.023:
        the levy in hundredths of a Rappen. A figure between 0.1 and 10
        can only be the levy in Rp."""
        if value > 10:
            _LOGGER.warning("%s: municipal levy %s is not a CHF/kWh figure, reading it as Rp/kWh divided by 100", mname, value)
            return round(value / 100, 3)
        if value > 0.1:
            _LOGGER.warning("%s: municipal levy %s is not a CHF/kWh figure, reading it as Rp/kWh", mname, value)
            return round(value, 3)
        return round(value * 100, 3)

    @classmethod
    def _parse_tariff_records(cls, tariffs: list[dict]) -> dict:
        """Energy/grid products, metering fees and municipality levies of
        one set of records (today's set, or next year's for the preview)."""
        energy_tariffs: dict[str, dict] = {}
        grid_tariffs: dict[str, dict] = {}
        metering_tariffs: dict[str, dict] = {}
        municipalities: dict[str, dict] = {}
        secondary_meter_municipalities: dict[str, dict] = {}
        for t in tariffs:
            ttype = t.get("tariffType")
            name = t.get("tariffName", "")
            if ttype in ("electricity", "grid"):
                raw_windows = t.get("prices", {}).get("energy") or []
                if not raw_windows or not any(w.get("price") for w in raw_windows):
                    # No time windows, or every window priced at zero: a
                    # dynamic (market-price) product such as BKW "Sonne
                    # Scheint" or CKW "dynamic", or an operator that left
                    # the prices out. Nothing here can price it, so it is
                    # not offered or shown.
                    _LOGGER.debug("Skipping %s product %r: no price windows", ttype, name)
                    continue
                (energy_tariffs if ttype == "electricity" else grid_tariffs)[name] = cls._parse_time_tariff(t)
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
                    # A levy that applies to secondary meters only is
                    # kept apart so it cannot replace the regular one.
                    target = secondary_meter_municipalities if is_secondary_meter_fee(t) else municipalities
                    levy = (energy[0].get("price") or 0.0) if energy else 0.0
                    target[mname] = {
                        "rp_kwh": cls._municipal_levy_rp(mname, levy),
                        "base_price_chf": base.get("price", 0),
                        "valid_from": t.get("startDate"),
                        "valid_to": t.get("endDate"),
                        # Operators put the fine print here — BKW notes a
                        # yearly cap ("Deckelung in CHF/a: 24") on levies
                        # it publishes as an implausible per-kWh figure.
                        "comment": m.get("municipalityComment") or None,
                    }
        # A municipality that has nothing but a secondary-meter levy
        # still gets that one rather than none.
        for mname, levy in secondary_meter_municipalities.items():
            municipalities.setdefault(mname, levy)
        return {
            "energy": energy_tariffs,
            "grid": grid_tariffs,
            "metering": metering_tariffs,
            "municipalities": municipalities,
        }

    @staticmethod
    def _parse_time_tariff(t: dict) -> dict:
        """Parse one energy/grid product. `windows` is the internal,
        pre-parsed list current_rate() evaluates at read time; `schedule`
        is the same information in the shape exposed as an attribute."""
        raw_windows = t.get("prices", {}).get("energy", [])
        prices = [w["price"] for w in raw_windows]
        language = weekday_language(raw_windows)
        # The format wants CHF/kWh; a few operators enter Rp/kWh (a
        # municipal utility publishing 15.9 "CHF/kWh" was found in the
        # 11/2026 survey). No Swiss tariff exceeds 3 CHF/kWh, so values
        # above that are read as Rp.
        if prices and max(prices) > 3:
            _LOGGER.warning("%s: prices above 3 CHF/kWh in the operator's file, reading them as Rp/kWh", t.get("tariffName"))
            prices = [p / 100 for p in prices]
            raw_windows = [{**w, "price": w["price"] / 100} for w in raw_windows]
        windows = [
            {
                "from": w["from"],
                "to": w["to"],
                "from_min": _parse_hhmm(w["from"]),
                # Some operators (TBF, for one) close the last window of the
                # day with "23:59" instead of "24:00"/"00:00", which would
                # leave the minute 23:59–00:00 without any tariff: treated
                # as end of day.
                "to_min": _MINUTES_PER_DAY if _parse_hhmm(w["to"]) == _MINUTES_PER_DAY - 1 else _parse_hhmm(w["to"]),
                "weekdays": normalize_weekdays(w.get("weekdays"), language),
                "months": normalize_months(w.get("months")),
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
            "base_price_unit": base.get("priceUnit"),
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
                tariffs, _selection = select_current_tariffs(raw.get("tariffs", []), dt_util.now().date())
                usable = any(t.get("tariffType") == "electricity" for t in tariffs)
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
