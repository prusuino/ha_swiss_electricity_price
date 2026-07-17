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

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
    CONF_NATIONAL_LEVIES_RP,
    CONF_OPERATOR_ID,
    CONF_OPERATOR_NAME,
    CONF_TARIFF_URL,
    CONF_URL_IS_MANUAL,
    CONF_VAT_PERCENT,
    DEFAULT_MANUAL_HIGH_FROM,
    DEFAULT_MANUAL_HIGH_TO,
    DEFAULT_MANUAL_HIGH_WEEKDAYS,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
)
from .coordinator import WEEKDAY_ABBR, async_fetch_raw_tariffs
from .directory import async_fetch_operator_directory, async_resolve_tariff_url
from .localization import t

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
    for tariff in raw.get("tariffs", []):
        if tariff.get("tariffType") != tariff_type:
            continue
        name = tariff.get("tariffName", "")
        if name and name not in names:
            names.append(name)
    return names


def _list_municipalities(raw: dict) -> list[str]:
    names: set[str] = set()
    for tariff in raw.get("tariffs", []):
        if tariff.get("tariffType") != "regional_fees":
            continue
        for m in tariff.get("prices", {}).get("municipalityTaxes", []):
            name = m.get("municipalityName")
            if name:
                names.add(name)
    return sorted(names)


class ElcomTariffConfigFlow(ConfigFlow, domain=DOMAIN):
    """Setup wizard: search + pick a grid operator, then a municipality + customer tariff."""

    VERSION = 2

    def __init__(self) -> None:
        self._operators: list[dict] = []
        self._matches: list[dict] = []
        self._selected: dict | None = None
        self._raw: dict | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if not self._operators:
            try:
                self._operators = await async_fetch_operator_directory(self.hass)
            except Exception:
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
                        resolved_url = await async_resolve_tariff_url(self.hass, url_to_use)
                        self._raw = await async_fetch_raw_tariffs(self.hass, resolved_url)
                    except Exception:
                        # Neither the operator's registered link nor a supplied
                        # manual URL worked — offer manual rate entry instead
                        # of a dead-end error.
                        self._selected = {**operator, "url": url_to_use, "resolved_url": None}
                        return await self.async_step_manual()
                    else:
                        if (
                            not _list_municipalities(self._raw)
                            or not _list_products(self._raw, "electricity")
                            or not _list_products(self._raw, "grid")
                        ):
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
            await self.async_set_unique_id(
                f"{self._selected['id']}_{user_input[CONF_MUNICIPALITY]}_"
                f"{user_input[CONF_ENERGY_PRODUCT]}_{user_input[CONF_GRID_PRODUCT]}"
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=t(
                    "device_name",
                    self.hass,
                    operator=self._selected["name"],
                    municipality=user_input[CONF_MUNICIPALITY],
                    energy_product=user_input[CONF_ENERGY_PRODUCT],
                ),
                data={
                    CONF_OPERATOR_ID: self._selected["id"],
                    CONF_OPERATOR_NAME: self._selected["name"],
                    CONF_TARIFF_URL: self._selected["url"],
                    CONF_URL_IS_MANUAL: self._selected.get("is_manual_url", False),
                    CONF_MUNICIPALITY: user_input[CONF_MUNICIPALITY],
                    CONF_ENERGY_PRODUCT: user_input[CONF_ENERGY_PRODUCT],
                    CONF_GRID_PRODUCT: user_input[CONF_GRID_PRODUCT],
                },
                options={
                    CONF_NATIONAL_LEVIES_RP: DEFAULT_NATIONAL_LEVIES_RP,
                    CONF_VAT_PERCENT: DEFAULT_VAT_PERCENT,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_MUNICIPALITY, default=municipalities[0]): vol.In(municipalities),
                vol.Required(CONF_ENERGY_PRODUCT, default=energy_products[0]): vol.In(energy_products),
                vol.Required(CONF_GRID_PRODUCT, default=grid_products[0]): vol.In(grid_products),
            }
        )
        return self.async_show_form(
            step_id="location",
            data_schema=schema,
            description_placeholders={"operator": self._selected["name"]},
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
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
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


class ElcomTariffOptionsFlow(OptionsFlow):
    """Change the national levies / VAT rate after setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NATIONAL_LEVIES_RP,
                    default=current.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP),
                ): vol.Coerce(float),
                vol.Required(
                    CONF_VAT_PERCENT, default=current.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
