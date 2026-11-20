"""Shared sample data and fakes for the tests that set an entry up.

`OperatorHost` stands in for everything on the network: the ElCom operator
directory and the operator's own web server with its tariff files. The
sample file builds on the records of test_tariffs.py and holds the 2026
tariffs next to the 2027 ones, as operators publish them from late summer
on."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.elcom_tariffs.const import (
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
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
    DOMAIN,
)

from .test_tariffs import ENERGY, FEES, GRID

FLOW = "custom_components.elcom_tariffs.config_flow."
COORDINATOR = "custom_components.elcom_tariffs.coordinator."

OPERATOR_ID = "https://energy.ld.admin.ch/elcom/electricityprice/operator/123"
OPERATOR_NAME = "Musterwerk AG"
TARIFF_URL = "https://example.invalid/files/tarife_2026.json"
NEXT_YEAR_URL = "https://example.invalid/files/tarife_2027.json"
OPERATORS = [
    {"id": OPERATOR_ID, "uid": "CHE-123.456.789", "name": OPERATOR_NAME, "url": TARIFF_URL, "likely_direct": True},
    {"id": OPERATOR_ID + "9", "uid": "CHE-987.654.321", "name": "Anderes Werk", "url": "https://example.invalid/anderes", "likely_direct": False},
]

# Entity ids start with the operator's slug and the last four characters of the entry id.
ENTRY_ID = "01JELCOMTESTENTRY000000ABCD"
PREFIX = "elcom_musterwerk_ag_abcd"
MANUAL_ENTRY_ID = "01JELCOMTESTENTRY000000WXYZ"
MANUAL_PREFIX = "elcom_musterwerk_ag_wxyz"

# Wednesday 18 November 2026, 10:00 in Zurich: a weekday inside the high-tariff window.
WEDNESDAY_MORNING = "2026-11-18 10:00:00+01:00"

_HIGH_LOW = ENERGY["prices"]["energy"]


def _two_rates(high: float, low: float) -> list[dict]:
    """The windows of the sample energy product (weekdays 06:00-22:00 high, else low) with other rates."""
    return [{**_HIGH_LOW[0], "price": high}, {**_HIGH_LOW[1], "price": low}]


def _flat(price: float) -> list[dict]:
    return [{"from": "00:00", "to": "00:00", "price": price}]


def next_year(record: dict, prices: dict | None = None) -> dict:
    """The same record as the operator publishes it for 2027."""
    return {**record, "startDate": "2027-01-01", "endDate": "2027-12-31", "prices": prices or record["prices"]}


GREEN = {**ENERGY, "tariffName": "Green Energie", "prices": {"energy": _flat(0.14), "base": {}}}
GRID_TWO_RATES = {**GRID, "tariffName": "Doppel Netz", "prices": {"energy": _two_rates(0.10, 0.06), "base": {"price": 0}}}
METERING = {
    "tariffType": "metering", "tariffName": "Messung Standard", "customerType": "H4", "startDate": "2026-01-01", "endDate": "2026-12-31",
    "prices": {"base": {"price": 3.5, "priceUnit": "CHF/M"}},
}
METERING_YEARLY = {**METERING, "tariffName": "Messung Lastgang", "prices": {"base": {"price": 120.0, "priceUnit": "CHF/J"}}}
METERING_QUARTERLY = {**METERING, "tariffName": "Messung Quartal", "prices": {"base": {"price": 12.0, "priceUnit": "CHF/Quartal"}}}

# The municipality section of the year, ending with it like the products.
FEES_2026 = {**FEES, "endDate": "2026-12-31"}

ENERGY_2027 = next_year(ENERGY, {"energy": _two_rates(0.13, 0.10), "base": {"price": 5.5, "priceUnit": "CHF/M"}})
GRID_TWO_RATES_2027 = next_year(GRID_TWO_RATES, {"energy": _two_rates(0.11, 0.07), "base": {"price": 0}})
FEES_2027 = next_year(FEES, {"municipalityTaxes": [
    {"municipalityName": "Musterwil", "municipalityEnergy": [{"price": 0.006}], "municipalityBase": {"price": 0}},
]})

TARIFFS_2026 = [ENERGY, GREEN, GRID, GRID_TWO_RATES, METERING, METERING_YEARLY, METERING_QUARTERLY, FEES_2026]
TARIFFS_2027 = [ENERGY_2027, GRID_TWO_RATES_2027, FEES_2027]


def sample_file(*tariffs: dict) -> dict:
    """A tariff file; without arguments the complete one with both years."""
    return {"dsoName": OPERATOR_NAME, "tariffs": deepcopy(list(tariffs) or TARIFFS_2026 + TARIFFS_2027)}


# What operators put into the `cantonalTaxes` section: a fee and a tax with
# one rate, a levy with a high and a low rate, a lump sum per month, a
# figure entered in hundredths of a Rappen, and entries nothing can be read from.
CANTONAL = {
    "tariffType": "regional_fees", "tariffName": "Kantonale Abgaben", "customerType": "Alle", "startDate": "2026-01-01", "endDate": "2026-12-31",
    "prices": {
        "municipalityTaxes": [{"municipalityEnergy": [{"price": 0.02}]}],
        "cantonalTaxes": [
            {"cantonName": "VD", "cantonComment": "Emolument cantonal", "cantonEnergy": _flat(0.002), "cantonBase": {"price": 0}},
            {"cantonName": "VD", "cantonComment": "Taxe cantonale", "cantonEnergy": _flat(0.006)},
            {"cantonName": "GE", "cantonEnergy": _two_rates(0.012, 0.008)},
            {"cantonName": "SG", "cantonComment": "Pauschale", "cantonEnergy": [], "cantonBase": {"price": 1.5, "priceUnit": "CHF/M"}},
            {"cantonName": "NE", "cantonComment": "Redevance", "cantonEnergy": _flat(70)},
            "not an entry",
            {"cantonName": "ZH", "cantonComment": "Leer", "cantonEnergy": _flat(0), "cantonBase": {}},
        ],
    },
}
EMOLUMENT = "VD – Emolument cantonal"
TAXE = "VD – Taxe cantonale"
GENEVA = "GE – Kantonale Abgaben"
LUMP_SUM = "SG – Pauschale"
REDEVANCE = "NE – Redevance"


class OperatorHost:
    """The ElCom directory and the operator's web server, without a network.

    `files` maps a URL to the tariff file behind it (or to the exception
    that fetching it raises); `registered` maps a year (None: the current
    one) to the link the operator reported to ElCom for it."""

    def __init__(self, raw: dict | None = None) -> None:
        self.files: dict[str, dict | Exception] = {TARIFF_URL: raw if raw is not None else sample_file()}
        self.registered: dict[int | None, str | None] = {}
        self.operators = OPERATORS
        self.directory_error: Exception | None = None
        self.lookups: list[int | None] = []
        self.fetched: list[str] = []
        self.verified: list[list[str]] = []

    async def _directory(self, hass: HomeAssistant, year: int | None = None, verify: bool = True) -> list[dict]:
        if self.directory_error:
            raise self.directory_error
        return deepcopy(self.operators)

    async def _verify(self, hass: HomeAssistant, operators: list[dict]) -> None:
        self.verified.append([operator["name"] for operator in operators])

    async def _operator_url(self, hass: HomeAssistant, operator_id: str, year: int | None = None) -> str | None:
        self.lookups.append(year)
        if self.directory_error:
            raise self.directory_error
        return self.registered.get(year)

    async def _resolve(self, hass: HomeAssistant, url: str) -> str:
        if url not in self.files:
            raise ValueError(f"No machine-readable tariff file could be found at {url}")
        return url

    async def _fetch(self, hass: HomeAssistant, url: str) -> dict:
        self.fetched.append(url)
        file = self.files[url]
        if isinstance(file, Exception):
            raise file
        return deepcopy(file)

    @contextmanager
    def patched(self):
        with (
            patch(FLOW + "async_fetch_operator_directory", self._directory),
            patch(FLOW + "async_verify_direct_links", self._verify),
            patch(FLOW + "async_fetch_operator_url", self._operator_url),
            patch(FLOW + "async_resolve_tariff_url", self._resolve),
            patch(FLOW + "async_fetch_raw_tariffs", self._fetch),
            patch(COORDINATOR + "async_fetch_operator_url", self._operator_url),
            patch(COORDINATOR + "async_resolve_tariff_url", self._resolve),
            patch(COORDINATOR + "async_fetch_raw_tariffs", self._fetch),
        ):
            yield self


def automatic_entry(data: dict | None = None, options: dict | None = None, **kwargs) -> MockConfigEntry:
    """An entry on the operator's tariff file: Basic Energie + Doppel Netz in Musterwil."""
    entry_data = {
        CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: OPERATOR_NAME, CONF_TARIFF_URL: TARIFF_URL,
        CONF_MUNICIPALITY: "Musterwil", CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Doppel Netz",
        **(data or {}),
    }
    kwargs.setdefault("entry_id", ENTRY_ID)
    kwargs.setdefault("version", 2)
    kwargs.setdefault("title", "Musterwerk AG Electricity Prices (Musterwil, Basic Energie)")
    kwargs.setdefault(
        "unique_id",
        f"{OPERATOR_ID}_{entry_data.get(CONF_MUNICIPALITY, '')}_{entry_data.get(CONF_ENERGY_PRODUCT)}_{entry_data.get(CONF_GRID_PRODUCT)}",
    )
    return MockConfigEntry(
        domain=DOMAIN,
        data={key: value for key, value in entry_data.items() if value is not None},
        options={CONF_NATIONAL_LEVIES_RP: 1.5, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0, **(options or {})},
        **kwargs,
    )


def manual_entry(data: dict | None = None, options: dict | None = None, **kwargs) -> MockConfigEntry:
    """An entry with rates typed in by hand: 25 Rp on weekdays 06:00-22:00, 18 Rp otherwise."""
    entry_data = {
        CONF_OPERATOR_ID: OPERATOR_ID, CONF_OPERATOR_NAME: OPERATOR_NAME,
        CONF_MANUAL_HIGH_RP: 25.0, CONF_MANUAL_LOW_RP: 18.0,
        CONF_MANUAL_HIGH_FROM: "06:00", CONF_MANUAL_HIGH_TO: "22:00",
        CONF_MANUAL_HIGH_WEEKDAYS: ["Mo", "Tu", "We", "Th", "Fr"], CONF_MANUAL_GRID_FEE_CHF: 9.5,
        **(data or {}),
    }
    kwargs.setdefault("entry_id", MANUAL_ENTRY_ID)
    kwargs.setdefault("version", 2)
    kwargs.setdefault("title", "Musterwerk AG Electricity Prices (manual)")
    kwargs.setdefault("unique_id", f"{OPERATOR_ID}_manual")
    return MockConfigEntry(
        domain=DOMAIN,
        data=entry_data,
        options={CONF_NATIONAL_LEVIES_RP: 3.0, CONF_VAT_PERCENT: 8.1, CONF_VAT_EXEMPT_LEVIES_RP: 0.0, **(options or {})},
        **kwargs,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def state_of(hass: HomeAssistant, entity_id: str):
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state


def value_of(hass: HomeAssistant, entity_id: str) -> float:
    return float(state_of(hass, entity_id).state)


async def travel(hass: HomeAssistant, freezer, when: str) -> None:
    """Let the clock reach `when` and run the timers that are due by then."""
    freezer.move_to(when)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """The coordinator's daily fetch, now."""
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def name_of(hass: HomeAssistant, entity_id: str) -> str:
    """The name the integration gives the entity (the state's friendly name may carry the device name in front)."""
    registered = er.async_get(hass).async_get(entity_id)
    assert registered is not None, f"{entity_id} is not registered"
    return registered.original_name


def device_of(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    return devices[0]
