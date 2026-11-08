"""Config and options flow for the Swiss Electricity Price (ElCom) integration.

Multi-instance: one config entry per grid operator + municipality + customer
tariff combination. Setup starts with a name search rather than one long
scrollable list of ~600 operators. The search field and the operator picker
live together in a single step/form (rather than as two separate steps) —
an earlier version used separate steps and hit a frontend issue where
switching from a select-only screen back to a text-only screen left the text
field unresponsive; keeping both fields in the same form throughout avoids
that transition entirely. Retyping the search and resubmitting re-filters
the list; picking an operator from the current list proceeds. Since not
every operator's registered link actually resolves to a usable
machine-readable tariff file (see directory.py), each match is labeled with
a direct-link or auto-detect hint so the user knows what to expect before
choosing.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.util import dt as dt_util

# Whether the entry's update listener reloads the entry on a change (then a
# reconfigure must not reload again, see _finish).
_LISTENER_RELOADS = True

from .const import (
    CONF_CANTONAL_LEVIES,
    CONF_CANTONAL_LEVIES_VAT_EXEMPT,
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_GRID_FEE_CHF,
    CONF_MANUAL_HIGH_FROM,
    CONF_MANUAL_HIGH_RP,
    CONF_MANUAL_HIGH_TO,
    CONF_MANUAL_HIGH_WEEKDAYS,
    CONF_MANUAL_LOW_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
    DEFAULT_MANUAL_HIGH_FROM,
    DEFAULT_MANUAL_HIGH_TO,
    DEFAULT_MANUAL_HIGH_WEEKDAYS,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_EXEMPT_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
)
from .coordinator import WEEKDAY_ABBR, ElcomTariffCoordinator, async_fetch_raw_tariffs
from .directory import (
    async_fetch_operator_directory,
    async_fetch_operator_url,
    async_resolve_tariff_url,
    async_verify_direct_links,
)
from .localization import t
from .tariff_period import select_current_tariffs

_LOGGER = logging.getLogger(__name__)

CONF_OPERATOR_SEARCH = "operator_search"
CONF_MANUAL_URL = "manual_url"
_MAX_RESULTS = 60


def _time_str(value: str) -> str:
    """Validate a HH:MM string (same format the coordinator's time-window match expects)."""
    try:
        h, m = value.split(":")
        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
            raise ValueError
    except ValueError as err:
        raise vol.Invalid("invalid_time") from err
    return value


def _current_tariffs(raw: dict) -> list[dict]:
    """The file's records valid today — operators often ship this year's
    and next year's tariffs in one file under the same product names, and
    the picker must list exactly what the coordinator will later parse."""
    return select_current_tariffs(raw.get("tariffs", []), dt_util.now().date())[0]


def _list_products(raw: dict, tariff_type: str) -> list[str]:
    """Tariff product names (tariffName, verbatim) of the given type
    (electricity or grid) — used to let the user pick their own energy and
    grid product directly, rather than trying to auto-pair them by name
    convention or by their customerType text, neither of which operators
    fill in consistently enough to match reliably (verified against real
    operator data: same-segment customerType text can differ in wording
    between an operator's own energy and grid product, and two different
    energy products can share one customerType)."""
    names: list[str] = []
    for tariff in _current_tariffs(raw):
        if tariff.get("tariffType") != tariff_type:
            continue
        name = tariff.get("tariffName", "")
        if name and name not in names:
            names.append(name)
    return names


def _list_municipalities(raw: dict) -> list[str]:
    names: set[str] = set()
    for tariff in _current_tariffs(raw):
        if tariff.get("tariffType") != "regional_fees":
            continue
        for m in tariff.get("prices", {}).get("municipalityTaxes", []):
            name = m.get("municipalityName")
            if name:
                names.add(name)
    return sorted(names)


def _list_cantonal(raw: dict) -> dict[str, dict]:
    """The file's cantonal levies valid today, parsed as the coordinator
    will parse them (same names, same rates)."""
    return ElcomTariffCoordinator._parse_tariff_records(_current_tariffs(raw))["cantonal"]


def _cantonal_label(name: str, levy: dict) -> str:
    """A cantonal levy as listed for choosing: its name and its rate(s)."""
    high, low = levy.get("high_rp_kwh"), levy.get("low_rp_kwh")
    if high is not None and low is not None:
        return f"{name} ({high:g} / {low:g} Rp/kWh)"
    windows = levy.get("windows") or []
    rp = windows[0]["rp_kwh"] if windows else 0.0
    base = levy.get("base_price_chf_month") or 0
    if not rp and base:
        return f"{name} ({base:g} {levy.get('base_price_unit') or 'CHF/M'})"
    return f"{name} ({rp:g} Rp/kWh)"


def _cantonal_selector(cantonal: dict[str, dict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=name, label=_cantonal_label(name, levy)) for name, levy in cantonal.items()],
            multiple=True,
            mode=SelectSelectorMode.LIST,
            sort=False,
        )
    )


class ElcomTariffConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup wizard: search + pick a grid operator, then a municipality + customer tariff."""

    VERSION = 2

    def __init__(self) -> None:
        self._operators: list[dict] = []
        self._entry = None  # the ConfigEntry being reconfigured, if any
        self._matches: list[dict] = []
        self._selected: dict | None = None
        self._raw: dict | None = None
        self._verified: set[str] = set()
        self._location: dict | None = None

    # ------------------------------------------------------------ reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        """Change the entry with the same steps as its setup. The entry
        keeps its id, so its entities keep their ids and history; the
        entities of a station or location that is no longer selected are
        removed and the reload creates the new ones."""
        self._entry = self._get_reconfigure_entry()
        return await self.async_step_user()

    def _default(self, key: str, fallback: Any) -> Any:
        """A form default: the entry's current value while reconfiguring."""
        if self._entry is not None and key in self._entry.data:
            return self._entry.data[key]
        return fallback

    def _abort_if_taken(self) -> None:
        """Like _abort_if_unique_id_configured, except that the entry being
        reconfigured may keep its own unique id."""
        if self._entry is None or self.unique_id != self._entry.unique_id:
            self._abort_if_unique_id_configured()

    def _finish(self, *, title: str, data: dict[str, Any], options: dict[str, Any] | None = None):
        """Create the entry, or update the one being reconfigured in place."""
        if self._entry is None:
            if options is not None:
                return self.async_create_entry(title=title, data=data, options=options)
            return self.async_create_entry(title=title, data=data)
        entry = self._entry
        if self.unique_id != entry.unique_id:
            # Another station, location or operator: the entities of the old
            # one would stay behind as orphans. Drop their registry rows; the
            # reload creates the new entities, and one whose unique id comes
            # back gets its entity id back as well.
            registry = er.async_get(self.hass)
            for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
                registry.async_remove(reg_entry.entity_id)
        # Update, then let the entry's update listener reload it (Home
        # Assistant asks integrations with a listener to do it that way).
        # Where the listener only refreshes, the reload is scheduled here.
        self.hass.config_entries.async_update_entry(
            entry,
            unique_id=self.unique_id,
            title=title,
            data=data,
            options=self._reconfigured_options(entry, options),
        )
        if not _LISTENER_RELOADS:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reconfigure_successful")

    def _reconfigured_options(self, entry, options: dict[str, Any] | None) -> dict[str, Any]:
        """The options to keep on a reconfigure: the user's current ones."""
        return dict(entry.options)

    async def _async_verify_matches(self) -> None:
        """Live-check the direct links of the operators about to be shown
        (at most _MAX_RESULTS), not the whole ~600-operator directory:
        verifying everything up front meant one GET against every
        operator's host as soon as the dialog opened, just to label
        entries the user never scrolls to. Each operator is checked at
        most once per flow."""
        pending = [o for o in self._matches if o["id"] not in self._verified]
        if not pending:
            return
        await async_verify_direct_links(self.hass, pending)
        self._verified.update(o["id"] for o in pending)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if not self._operators:
            try:
                self._operators = await async_fetch_operator_directory(self.hass, verify=False)
            except Exception as err:  # noqa: BLE001 - any failure ends the flow with the same abort
                _LOGGER.debug("ElCom operator directory could not be retrieved: %s", err)
                return self.async_abort(reason="cannot_connect")
            if not self._operators:
                return self.async_abort(reason="no_data")

        query = ""
        if user_input is not None:
            query = user_input.get(CONF_OPERATOR_SEARCH, "").strip()
            query_lower = query.lower()
            matches = (
                [o for o in self._operators if query_lower in o["name"].lower()]
                if query_lower
                else list(self._operators)
            )
            if not matches:
                errors["base"] = "no_matches"
                self._matches = []
            else:
                self._matches = matches[:_MAX_RESULTS]
                chosen_id = user_input.get(CONF_OPERATOR_ID)
                operator = (
                    next((o for o in self._matches if o["id"] == chosen_id), None)
                    if chosen_id
                    else None
                )
                manual_url = user_input.get(CONF_MANUAL_URL, "").strip()
                if manual_url and operator is None:
                    errors["base"] = "manual_url_needs_operator"
                elif operator is not None:
                    url_to_use = manual_url or operator["url"]
                    try:
                        try:
                            resolved_url = await async_resolve_tariff_url(self.hass, url_to_use)
                            self._raw = await async_fetch_raw_tariffs(self.hass, resolved_url)
                        except Exception:
                            if manual_url:
                                raise
                            # The registration for this year is unusable;
                            # from autumn on the operator may already have
                            # registered next year's (often newer) link.
                            next_year_url = await async_fetch_operator_url(
                                self.hass, operator["id"], dt_util.now().year + 1
                            )
                            if not next_year_url or next_year_url == url_to_use:
                                raise
                            resolved_url = await async_resolve_tariff_url(self.hass, next_year_url)
                            self._raw = await async_fetch_raw_tariffs(self.hass, resolved_url)
                            url_to_use = next_year_url
                    except Exception as err:  # noqa: BLE001 - any failure leads to the manual fallback
                        # Neither the operator's registered link nor a supplied
                        # manual URL worked — offer manual rate entry instead
                        # of a dead-end error.
                        _LOGGER.debug("Tariff file for %s not usable via %s: %s", operator["name"], url_to_use, err)
                        self._selected = {**operator, "url": url_to_use, "resolved_url": None}
                        return await self.async_step_manual()
                    else:
                        # A file without a municipality section is fine
                        # (no municipal levy is added then); without energy
                        # or grid products there is nothing to combine.
                        if not _list_products(self._raw, "electricity") or not _list_products(self._raw, "grid"):
                            self._selected = {**operator, "url": url_to_use, "resolved_url": resolved_url}
                            return await self.async_step_manual()
                        else:
                            self._selected = {
                                **operator,
                                "url": url_to_use,
                                "resolved_url": resolved_url,
                                "is_manual_url": bool(manual_url),
                            }
                            return await self.async_step_location()
        else:
            self._matches = self._operators[:_MAX_RESULTS]

        fields: dict[Any, Any] = {vol.Optional(CONF_OPERATOR_SEARCH, default=query): str}
        if self._matches:
            await self._async_verify_matches()
            options = [
                SelectOptionDict(
                    value=o["id"],
                    label=o["name"]
                    + (
                        t("operator_direct_hint", self.hass)
                        if o["likely_direct"]
                        else t("operator_uncertain_hint", self.hass)
                    ),
                )
                for o in self._matches
            ]
            fields[vol.Optional(CONF_OPERATOR_ID)] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN, sort=False)
            )
            fields[vol.Optional(CONF_MANUAL_URL, default="")] = str

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"count": str(len(self._matches))},
        )

    async def async_step_location(self, user_input: dict[str, Any] | None = None):
        assert self._selected is not None and self._raw is not None
        municipalities = _list_municipalities(self._raw)
        energy_products = _list_products(self._raw, "electricity")
        grid_products = _list_products(self._raw, "grid")

        if user_input is not None:
            grid_includes_levies = bool(user_input.get(CONF_GRID_INCLUDES_LEVIES, True))
            # Empty when the operator's file has no municipality section:
            # no municipal levy is added to the combined price then.
            municipality = user_input.get(CONF_MUNICIPALITY, "") if municipalities else ""
            await self.async_set_unique_id(
                _unique_id(
                    self._selected["id"], municipality, user_input[CONF_ENERGY_PRODUCT], user_input[CONF_GRID_PRODUCT]
                )
            )
            self._abort_if_taken()
            self._location = {
                "title": _entry_title(self.hass, self._selected["name"], municipality, user_input[CONF_ENERGY_PRODUCT]),
                "data": {
                    CONF_OPERATOR_ID: self._selected["id"],
                    CONF_OPERATOR_NAME: self._selected["name"],
                    CONF_TARIFF_URL: self._selected["url"],
                    CONF_URL_IS_MANUAL: self._selected.get("is_manual_url", False),
                    CONF_MUNICIPALITY: municipality,
                    CONF_ENERGY_PRODUCT: user_input[CONF_ENERGY_PRODUCT],
                    CONF_GRID_PRODUCT: user_input[CONF_GRID_PRODUCT],
                    CONF_GRID_INCLUDES_LEVIES: grid_includes_levies,
                    CONF_CANTONAL_LEVIES: [],
                    CONF_CANTONAL_LEVIES_VAT_EXEMPT: [],
                },
                "options": {
                    # The VSE tariff format defines the grid price as including
                    # the federal levies, so nothing is added on top in that
                    # case; operators publishing a net grid price get the
                    # published levy total added. Adjustable later either way.
                    CONF_NATIONAL_LEVIES_RP: 0.0 if grid_includes_levies else DEFAULT_NATIONAL_LEVIES_RP,
                    CONF_VAT_PERCENT: DEFAULT_VAT_PERCENT,
                    CONF_VAT_EXEMPT_LEVIES_RP: DEFAULT_VAT_EXEMPT_LEVIES_RP,
                },
            }
            if _list_cantonal(self._raw):
                return await self.async_step_cantonal()
            return self._create_location_entry()

        fields: dict[Any, Any] = {}
        if municipalities:
            fields[vol.Required(CONF_MUNICIPALITY, default=municipalities[0])] = vol.In(municipalities)
        fields[vol.Required(CONF_ENERGY_PRODUCT, default=energy_products[0])] = vol.In(energy_products)
        fields[vol.Required(CONF_GRID_PRODUCT, default=grid_products[0])] = vol.In(grid_products)
        fields[vol.Required(CONF_GRID_INCLUDES_LEVIES, default=True)] = bool
        return self.async_show_form(
            step_id="location",
            data_schema=vol.Schema(fields),
            description_placeholders={"operator": self._selected["name"]},
        )

    def _create_location_entry(self):
        assert self._location is not None
        return self._finish(
            title=self._location["title"], data=self._location["data"], options=self._location["options"]
        )

    async def async_step_cantonal(self, user_input: dict[str, Any] | None = None):
        """Only for a file with a `cantonalTaxes` section: which of its
        entries belong on this customer's bill, and which of those are
        billed without VAT. Nothing is preselected — operators put very
        different things under this heading (cantonal taxes, but also the
        federal levies or municipal fees), so a default would be wrong for
        somebody, and double-counting the federal levies would be the
        most likely error."""
        assert self._selected is not None and self._raw is not None and self._location is not None
        cantonal = _list_cantonal(self._raw)
        if user_input is not None:
            chosen = [n for n in user_input.get(CONF_CANTONAL_LEVIES, []) if n in cantonal]
            exempt = [n for n in user_input.get(CONF_CANTONAL_LEVIES_VAT_EXEMPT, []) if n in chosen]
            self._location["data"][CONF_CANTONAL_LEVIES] = chosen
            self._location["data"][CONF_CANTONAL_LEVIES_VAT_EXEMPT] = exempt
            return self._create_location_entry()
        schema = vol.Schema(
            {
                vol.Optional(CONF_CANTONAL_LEVIES, default=[]): _cantonal_selector(cantonal),
                vol.Optional(CONF_CANTONAL_LEVIES_VAT_EXEMPT, default=[]): _cantonal_selector(cantonal),
            }
        )
        return self.async_show_form(
            step_id="cantonal",
            data_schema=schema,
            description_placeholders={"operator": self._selected["name"], "count": str(len(cantonal))},
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None):
        """Fallback when neither the operator's registered link nor a
        supplied manual URL could be resolved/parsed: let the user enter
        their known rates by hand instead of a dead-end error."""
        assert self._selected is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                high_from = _time_str(user_input[CONF_MANUAL_HIGH_FROM])
                high_to = _time_str(user_input[CONF_MANUAL_HIGH_TO])
            except vol.Invalid:
                errors["base"] = "invalid_time"
            else:
                await self.async_set_unique_id(f"{self._selected['id']}_manual")
                self._abort_if_taken()
                return self._finish(
                    title=t("device_name_manual", self.hass, operator=self._selected["name"]),
                    data={
                        CONF_OPERATOR_ID: self._selected["id"],
                        CONF_OPERATOR_NAME: self._selected["name"],
                        CONF_MANUAL_HIGH_RP: user_input[CONF_MANUAL_HIGH_RP],
                        CONF_MANUAL_LOW_RP: user_input.get(CONF_MANUAL_LOW_RP),
                        CONF_MANUAL_HIGH_FROM: high_from,
                        CONF_MANUAL_HIGH_TO: high_to,
                        CONF_MANUAL_HIGH_WEEKDAYS: user_input.get(
                            CONF_MANUAL_HIGH_WEEKDAYS, DEFAULT_MANUAL_HIGH_WEEKDAYS
                        ),
                        CONF_MANUAL_GRID_FEE_CHF: user_input.get(CONF_MANUAL_GRID_FEE_CHF),
                    },
                    options={
                        CONF_NATIONAL_LEVIES_RP: DEFAULT_NATIONAL_LEVIES_RP,
                        CONF_VAT_PERCENT: DEFAULT_VAT_PERCENT,
                        CONF_VAT_EXEMPT_LEVIES_RP: DEFAULT_VAT_EXEMPT_LEVIES_RP,
                    },
                )

        weekday_options = [
            SelectOptionDict(value=d, label=t(f"weekday_{d.lower()}", self.hass)) for d in WEEKDAY_ABBR
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_MANUAL_HIGH_RP): vol.Coerce(float),
                vol.Optional(CONF_MANUAL_LOW_RP): vol.Coerce(float),
                vol.Optional(CONF_MANUAL_HIGH_FROM, default=DEFAULT_MANUAL_HIGH_FROM): str,
                vol.Optional(CONF_MANUAL_HIGH_TO, default=DEFAULT_MANUAL_HIGH_TO): str,
                vol.Optional(
                    CONF_MANUAL_HIGH_WEEKDAYS, default=DEFAULT_MANUAL_HIGH_WEEKDAYS
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=weekday_options, multiple=True, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional(CONF_MANUAL_GRID_FEE_CHF): vol.Coerce(float),
            }
        )
        return self.async_show_form(
            step_id="manual",
            data_schema=schema,
            errors=errors,
            description_placeholders={"operator": self._selected["name"]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "ElcomTariffOptionsFlow":
        return ElcomTariffOptionsFlow()


def _entry_title(hass, operator: str, municipality: str, energy_product: str) -> str:
    if municipality:
        return t("device_name", hass, operator=operator, municipality=municipality, energy_product=energy_product)
    return t("device_name_no_municipality", hass, operator=operator, energy_product=energy_product)


def _unique_id(operator_id: str, municipality: str, energy_product: str, grid_product: str) -> str:
    return f"{operator_id}_{municipality}_{energy_product}_{grid_product}"


def _select(options: list[str], current: str | None) -> tuple[SelectSelector, str]:
    """A dropdown over the operator's current names, defaulting to the
    stored choice when the file still has it and to the first name
    otherwise (the stored one was renamed or dropped)."""
    selector = SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=o, label=o) for o in options],
            mode=SelectSelectorMode.DROPDOWN,
            sort=False,
        )
    )
    return selector, current if current in options else options[0]


class ElcomTariffOptionsFlow(OptionsFlow):
    """Change the national levies / VAT rate after setup and, for an entry
    based on the operator's tariff file, re-select municipality and
    energy/grid product from the file as it is today. Operators rename
    products or restructure the file between years (TBF prefixed every
    product name with "TBF " and dropped the municipality section in
    November 2026), and the combined-price sensors stay unavailable until
    the current names are chosen — which used to take removing the entry
    and setting it up again, losing renames and history."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        entry = self.config_entry
        errors: dict[str, str] = {}
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        data: dict = {}
        if CONF_MANUAL_HIGH_RP not in entry.data and coordinator is not None and coordinator.data:
            data = coordinator.data
        energy_products = list(data.get("energy", {}))
        grid_products = list(data.get("grid", {}))
        municipalities = sorted(data.get("municipalities", {}))
        cantonal = data.get("cantonal", {})
        selectable = bool(energy_products and grid_products)

        if user_input is not None:
            options = {
                CONF_NATIONAL_LEVIES_RP: user_input[CONF_NATIONAL_LEVIES_RP],
                CONF_VAT_PERCENT: user_input[CONF_VAT_PERCENT],
                CONF_VAT_EXEMPT_LEVIES_RP: user_input[CONF_VAT_EXEMPT_LEVIES_RP],
            }
            if selectable:
                new_data = {
                    **entry.data,
                    CONF_ENERGY_PRODUCT: user_input[CONF_ENERGY_PRODUCT],
                    CONF_GRID_PRODUCT: user_input[CONF_GRID_PRODUCT],
                }
                if municipalities:
                    new_data[CONF_MUNICIPALITY] = user_input[CONF_MUNICIPALITY]
                if cantonal:
                    chosen = [n for n in user_input.get(CONF_CANTONAL_LEVIES, []) if n in cantonal]
                    new_data[CONF_CANTONAL_LEVIES] = chosen
                    new_data[CONF_CANTONAL_LEVIES_VAT_EXEMPT] = [
                        n for n in user_input.get(CONF_CANTONAL_LEVIES_VAT_EXEMPT, []) if n in chosen
                    ]
                if new_data != dict(entry.data):
                    unique_id = _unique_id(
                        new_data[CONF_OPERATOR_ID],
                        new_data.get(CONF_MUNICIPALITY, ""),
                        new_data[CONF_ENERGY_PRODUCT],
                        new_data[CONF_GRID_PRODUCT],
                    )
                    if any(
                        e.unique_id == unique_id and e.entry_id != entry.entry_id
                        for e in self.hass.config_entries.async_entries(DOMAIN)
                    ):
                        errors["base"] = "already_configured"
                    else:
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data=new_data,
                            unique_id=unique_id,
                            title=_entry_title(
                                self.hass,
                                new_data[CONF_OPERATOR_NAME],
                                new_data.get(CONF_MUNICIPALITY, ""),
                                new_data[CONF_ENERGY_PRODUCT],
                            ),
                        )
                        # The chosen products feed the combined-price sensors,
                        # the base-fee defaults, the device name and the
                        # high-tariff binary sensor: a reload applies them all.
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
            if not errors:
                return self.async_create_entry(title="", data=options)

        current = entry.options
        fields: dict[Any, Any] = {}
        if selectable:
            if municipalities:
                selector, default = _select(municipalities, entry.data.get(CONF_MUNICIPALITY))
                fields[vol.Required(CONF_MUNICIPALITY, default=default)] = selector
            selector, default = _select(energy_products, entry.data.get(CONF_ENERGY_PRODUCT))
            fields[vol.Required(CONF_ENERGY_PRODUCT, default=default)] = selector
            selector, default = _select(grid_products, entry.data.get(CONF_GRID_PRODUCT))
            fields[vol.Required(CONF_GRID_PRODUCT, default=default)] = selector
            if cantonal:
                chosen = [n for n in entry.data.get(CONF_CANTONAL_LEVIES) or [] if n in cantonal]
                exempt = [n for n in entry.data.get(CONF_CANTONAL_LEVIES_VAT_EXEMPT) or [] if n in chosen]
                fields[vol.Optional(CONF_CANTONAL_LEVIES, default=chosen)] = _cantonal_selector(cantonal)
                fields[vol.Optional(CONF_CANTONAL_LEVIES_VAT_EXEMPT, default=exempt)] = _cantonal_selector(cantonal)
        fields[
            vol.Required(
                CONF_NATIONAL_LEVIES_RP,
                default=current.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP),
            )
        ] = vol.Coerce(float)
        fields[vol.Required(CONF_VAT_PERCENT, default=current.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT))] = vol.Coerce(
            float
        )
        fields[
            vol.Required(
                CONF_VAT_EXEMPT_LEVIES_RP,
                default=current.get(CONF_VAT_EXEMPT_LEVIES_RP, DEFAULT_VAT_EXEMPT_LEVIES_RP),
            )
        ] = vol.Coerce(float)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={"operator": entry.data.get(CONF_OPERATOR_NAME, "")},
        )
