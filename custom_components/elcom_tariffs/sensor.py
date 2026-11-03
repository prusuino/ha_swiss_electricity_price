"""Sensors for the Swiss Electricity Price (ElCom) integration — all tariff
products (rate, high/low reference prices, monthly base fee) + metering fees
+ municipality levies + combined price, for the selected grid operator."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_INCLUDES_LEVIES,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_HIGH_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_VAT_PERCENT,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
)
from .coordinator import ElcomTariffCoordinator, current_rate
from .device import device_info, entity_id_prefix
from .localization import t, tariff_level_text
from .price_schedule import (
    combined_price_at,
    level_at,
    level_change_attributes,
    localized,
    manual_price_at,
    schedule_attributes,
)

_YEAR_RE = re.compile(r"(20\d{2})")

# Units the VSE tariff format uses for flat fees (prices.base.priceUnit),
# spelled out for display. Anything unknown is shown as the file has it.
_FEE_UNITS = {
    "CHF/M": "CHF/month",
    "CHF/MONAT": "CHF/month",
    "CHF/MONTH": "CHF/month",
    "CHF/J": "CHF/year",
    "CHF/A": "CHF/year",
    "CHF/Y": "CHF/year",
    "CHF/JAHR": "CHF/year",
    "CHF/YEAR": "CHF/year",
    "CHF/D": "CHF/day",
    "CHF/TAG": "CHF/day",
    "CHF/DAY": "CHF/day",
}


def _fee_unit(raw: str | None) -> str:
    """Display unit of a flat fee; CHF/month when the file names none."""
    if not raw:
        return "CHF/month"
    return _FEE_UNITS.get(raw.strip().upper(), raw.strip())


class _SlugBook:
    """Hands out one id slug per (category, name) and keeps it for the life
    of the entry. slugify() drops characters like "<", "=" and ">", so two
    products that differ only in them — BKW's "Benutzungsdauer <= 3500h/a"
    and "... > 3500h/a" — would collapse into one unique_id and the second
    entity would be refused by Home Assistant. The first name to claim a
    slug keeps it plain (so ids created by earlier versions stay as they
    are); a later name landing on a taken slug gets a short hash of its
    full name appended. Names arriving in one batch are claimed in sorted
    order, so the outcome does not depend on the order within the file."""

    def __init__(self) -> None:
        self._slugs: dict[tuple[str, str], str] = {}
        self._taken: set[tuple[str, str]] = set()

    def claim(self, category: str, names: list[str]) -> None:
        for name in sorted(n for n in names if (category, n) not in self._slugs):
            slug = slugify(name)
            if (category, slug) in self._taken:
                slug = f"{slug}_{hashlib.sha1(name.encode('utf-8')).hexdigest()[:6]}"
            self._taken.add((category, slug))
            self._slugs[(category, name)] = slug

    def get(self, category: str, name: str) -> str:
        return self._slugs[(category, name)]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ElcomTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    prefix = entity_id_prefix(entry)

    if CONF_MANUAL_HIGH_RP in entry.data:
        async_add_entities(_manual_entities(hass, coordinator, entry, prefix))
        return

    entities: list[SensorEntity] = [
        ElcomDiagnosticHostSensor(hass, coordinator, entry, prefix),
        ElcomDiagnosticPathSensor(hass, coordinator, entry, prefix),
        ElcomDiagnosticFilenameSensor(hass, coordinator, entry, prefix),
        ElcomDiagnosticYearSensor(hass, coordinator, entry, prefix),
        ElcomDiagnosticLastCheckedSensor(hass, coordinator, entry, prefix),
    ]
    # The municipality may be empty (operator file without a municipality section).
    if entry.data.get(CONF_ENERGY_PRODUCT) and entry.data.get(CONF_GRID_PRODUCT):
        entities.append(ElcomTariffStatusSensor(hass, coordinator, entry, prefix))
        entities.append(ElcomPriceCurrentSensor(hass, coordinator, entry, prefix))
        entities.append(ElcomPriceLevelSensor(hass, coordinator, entry, "high", prefix))
        entities.append(ElcomPriceLevelSensor(hass, coordinator, entry, "low", prefix))
        entities.append(ElcomNextPeriodPriceSensor(hass, coordinator, entry, prefix))
    async_add_entities(entities)

    # Product, base-fee, metering-fee and municipality entities follow the operator's
    # file: whatever the latest fetch contains and hasn't been created yet
    # is added (a product that first appears in next year's file shows up
    # without a reload). Entities whose key later disappears from the file
    # are kept and report unavailable (see the `available` properties)
    # rather than being removed from the registry — one published file
    # missing a product is no proof it is gone for good, and removing the
    # entity would throw away the user's renames, areas and customizations.
    known: set[tuple] = set()
    slugs = _SlugBook()

    @callback
    def _async_add_dynamic_entities() -> None:
        data = coordinator.data or {}
        new: list[SensorEntity] = []
        for category in ("energy", "grid"):
            slugs.claim(category, list(data.get(category, {})))
            for name, d in data.get(category, {}).items():
                slug = slugs.get(category, name)
                if (category, name) not in known:
                    known.add((category, name))
                    new.append(ElcomTariffSensor(hass, coordinator, entry, category, name, prefix, slug))
                if d.get("high_rp_kwh") is not None and (category, name, "levels") not in known:
                    known.add((category, name, "levels"))
                    new.append(ElcomTariffLevelSensor(hass, coordinator, entry, category, name, "high_rp_kwh", prefix, slug))
                    new.append(ElcomTariffLevelSensor(hass, coordinator, entry, category, name, "low_rp_kwh", prefix, slug))
                if (d.get("base_price_chf_month") or 0) > 0 and (category, name, "base_fee") not in known:
                    known.add((category, name, "base_fee"))
                    new.append(ElcomBaseFeeSensor(hass, coordinator, entry, category, name, prefix, slug))
        slugs.claim("metering", list(data.get("metering", {})))
        for name in data.get("metering", {}):
            if ("metering", name) not in known:
                known.add(("metering", name))
                new.append(ElcomMeteringSensor(hass, coordinator, entry, name, prefix, slugs.get("metering", name)))
        slugs.claim("municipality", list(data.get("municipalities", {})))
        for name in data.get("municipalities", {}):
            if ("municipality", name) not in known:
                known.add(("municipality", name))
                new.append(
                    ElcomMunicipalityLevySensor(hass, coordinator, entry, name, prefix, slugs.get("municipality", name))
                )
        if new:
            async_add_entities(new)

    _async_add_dynamic_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_dynamic_entities))


class ElcomTariffSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Currently active rate (CHF/kWh) for an energy or grid tariff product.
    The active window is resolved against the clock on every read (and the
    coordinator rewrites the state at every window boundary), not frozen at
    fetch time."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        category: str, name: str, prefix: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._hass_ref = hass
        self._category = category
        self._name_key = name
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{category}_{slug}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_{category}_{slug}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._category, {}).get(self._name_key, {})

    @property
    def available(self) -> bool:
        # Unavailable, not "unknown", once the product has vanished from the operator's file.
        return super().available and bool(self._data())

    @property
    def native_value(self):
        rp, _ = current_rate(self._data(), dt_util.now())
        return round(rp / 100, 5) if rp is not None else None

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        _, level = current_rate(d, dt_util.now())
        return {
            "current_level": tariff_level_text(level, self._hass_ref),
            "base_price_chf_month": d.get("base_price_chf_month"),
            "customer_type": d.get("customer_type"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "schedule": d.get("schedule"),
        }


class ElcomTariffLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Fixed high-tariff or low-tariff reference price of a tariff product
    (independent of the current time)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        category: str, name: str, level_key: str, prefix: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._category = category
        self._name_key = name
        self._level_key = level_key
        suffix = "high" if level_key == "high_rp_kwh" else "low"
        level_label = t("tariff_level_high", hass) if suffix == "high" else t("tariff_level_low", hass)
        self._attr_name = f"{name} ({level_label})"
        self._attr_unique_id = f"{entry.entry_id}_{category}_{slug}_{suffix}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_{category}_{slug}_{suffix}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._category, {}).get(self._name_key, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_value(self):
        rp = self._data().get(self._level_key)
        return round(rp / 100, 5) if rp is not None else None

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return {
            "customer_type": d.get("customer_type"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
        }


class ElcomBaseFeeSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Fixed base fee of an energy or grid tariff product (CHF/month as a
    rule; the unit follows the operator's file). Created only for products
    whose base price is above zero. Only the products chosen at setup start
    enabled — an operator can publish dozens of products, and the base fee
    of a product you are not on is rarely of interest; the others can be
    enabled in their entity settings. The fee is not part of the per-kWh
    price sensors, which is why it is a separate entity."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:cash"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        category: str, name: str, prefix: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._category = category
        self._name_key = name
        self._attr_name = f"{name} ({t('grid_fee_name', hass)})"
        selected = entry.data.get(CONF_ENERGY_PRODUCT if category == "energy" else CONF_GRID_PRODUCT)
        self._attr_entity_registry_enabled_default = not selected or name == selected
        self._attr_unique_id = f"{entry.entry_id}_{category}_{slug}_base_fee"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_{category}_{slug}_base_fee"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._category, {}).get(self._name_key, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_unit_of_measurement(self) -> str:
        return _fee_unit(self._data().get("base_price_unit"))

    @property
    def native_value(self):
        return self._data().get("base_price_chf_month")

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return {
            "price_unit": d.get("base_price_unit"),
            "customer_type": d.get("customer_type"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
        }


def _diagnostic_filename(data: dict) -> str | None:
    tariff_url = data.get("tariff_url")
    return tariff_url.rsplit("/", 1)[-1] if tariff_url else None


def _diagnostic_host(data: dict) -> str | None:
    tariff_url = data.get("tariff_url")
    return urlsplit(tariff_url).netloc if tariff_url else None


def _diagnostic_path(data: dict) -> str | None:
    """Directory portion of the resolved URL, without the filename (which
    has its own sensor) — split out from the host so neither line runs too
    long to fit the diagnostics card."""
    tariff_url = data.get("tariff_url")
    if not tariff_url:
        return None
    path = urlsplit(tariff_url).path
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return directory or "/"


def _diagnostic_tariff_year(data: dict) -> str | None:
    """The tariff data's own validity year (from its startDate), falling
    back to a year found in the file name only when the data carries no
    startDate. Operators commonly publish next year's tariffs months in
    advance, so a file dated e.g. August 2025 can (and often does) contain
    tariffs valid from 2026, not 2025 — which is why startDate wins."""
    valid_from = data.get("tariff_valid_from")
    match = _YEAR_RE.search(valid_from) if valid_from else None
    if match:
        return match.group(1)
    filename = _diagnostic_filename(data)
    match = _YEAR_RE.search(filename) if filename else None
    return match.group(1) if match else None


class _ElcomDiagnosticSensorBase(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Shared setup for the individual diagnostic sensors below — each shows
    exactly one piece of source info about the operator's tariff file as its
    own visible entity (rather than bundled as hidden attributes on a single
    sensor), so they show up as separate rows under the device's
    Diagnostics section."""

    _attr_has_entity_name = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        prefix: str, name_key: str, id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t(name_key, hass)
        self._attr_unique_id = f"{entry.entry_id}_diagnostics_{id_suffix}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_diagnostics_{id_suffix}"


class ElcomDiagnosticHostSensor(_ElcomDiagnosticSensorBase):
    """The host of the tariff-file URL actually being fetched (after
    resolving the operator's registered link, which may itself point at a
    landing page rather than the file directly). Split out from the path and
    filename, which have their own sensors, so no single value runs too long
    to fit the diagnostics card. The full URL is available as an attribute
    for copy-pasting."""

    _attr_icon = "mdi:web"

    def __init__(self, hass, coordinator, entry, prefix):
        super().__init__(hass, coordinator, entry, prefix, "diagnostics_host_name", "host")

    @property
    def native_value(self):
        return _diagnostic_host(self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            "tariff_file_url": data.get("tariff_url"),
            "registered_url": data.get("registered_url"),
        }


class ElcomDiagnosticPathSensor(_ElcomDiagnosticSensorBase):
    """Directory portion of the resolved tariff-file URL (without the
    filename, which has its own sensor)."""

    _attr_icon = "mdi:folder-outline"

    def __init__(self, hass, coordinator, entry, prefix):
        super().__init__(hass, coordinator, entry, prefix, "diagnostics_path_name", "path")

    @property
    def native_value(self):
        return _diagnostic_path(self.coordinator.data or {})


class ElcomDiagnosticFilenameSensor(_ElcomDiagnosticSensorBase):
    """The current tariff file's name, taken from the resolved URL."""

    _attr_icon = "mdi:file-outline"

    def __init__(self, hass, coordinator, entry, prefix):
        super().__init__(hass, coordinator, entry, prefix, "diagnostics_filename_name", "filename")

    @property
    def native_value(self):
        return _diagnostic_filename(self.coordinator.data or {})


class ElcomDiagnosticYearSensor(_ElcomDiagnosticSensorBase):
    """The tariff data's own validity year — from the electricity tariffs'
    startDate, not any date that might appear in the file name (operators
    commonly publish next year's tariffs months in advance, so those can
    disagree). Falls back to a year found in the filename only if the data
    itself has no startDate."""

    _attr_icon = "mdi:calendar-outline"

    def __init__(self, hass, coordinator, entry, prefix):
        super().__init__(hass, coordinator, entry, prefix, "diagnostics_year_name", "year")

    @property
    def native_value(self):
        return _diagnostic_tariff_year(self.coordinator.data or {})

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        valid_to = data.get("tariff_valid_to")
        return {"valid_to": valid_to} if valid_to else {}


class ElcomDiagnosticLastCheckedSensor(_ElcomDiagnosticSensorBase):
    """When the tariff file was last successfully fetched and parsed."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, hass, coordinator, entry, prefix):
        super().__init__(hass, coordinator, entry, prefix, "diagnostics_last_checked_name", "last_checked")

    @property
    def native_value(self):
        last_checked = (self.coordinator.data or {}).get("last_checked")
        return dt_util.parse_datetime(last_checked) if last_checked else None


class ElcomTariffStatusSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Shows whether the configured customer tariff is currently on high or
    low tariff — resolved against the clock on every read."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:clock-time-eight-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("tariff_status_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_tariff_status"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_tariff_level"

    def _energy(self) -> dict:
        data = self.coordinator.data or {}
        return data.get("energy", {}).get(self._entry.data[CONF_ENERGY_PRODUCT], {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._energy())

    @property
    def native_value(self):
        _, level = current_rate(self._energy(), dt_util.now())
        return tariff_level_text(level, self._hass_ref)

    @property
    def extra_state_attributes(self):
        d = self._energy()
        if not d:
            return {}
        return {
            "energy_product": self._entry.data[CONF_ENERGY_PRODUCT],
            "schedule": d.get("schedule"),
            **localized(level_change_attributes(level_at(d), [d], dt_util.now()), self._hass_ref),
        }


class ElcomMeteringSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Fixed metering fee (CHF/month as a rule; the unit follows the operator's file)."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        name: str, prefix: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._name_key = name
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_metering_{slug}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_metering_{slug}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get("metering", {}).get(self._name_key, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_unit_of_measurement(self) -> str:
        return _fee_unit(self._data().get("unit"))

    @property
    def native_value(self):
        return self._data().get("price_chf")

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return {
            "customer_type": d.get("customer_type"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
        }


class ElcomMunicipalityLevySensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Municipality levy (Rp/kWh) for a single municipality served by the operator."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "Rp/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:town-hall"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        name: str, prefix: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._name_key = name
        self._attr_name = f"{t('municipality_levy_prefix', hass)} {name}"
        # An operator can serve hundreds of municipalities; only the one the
        # entry was set up for starts enabled (all of them without a choice).
        selected = entry.data.get(CONF_MUNICIPALITY)
        self._attr_entity_registry_enabled_default = not selected or name == selected
        self._attr_unique_id = f"{entry.entry_id}_municipality_{slug}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_municipality_levy_{slug}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get("municipalities", {}).get(self._name_key, {})

    @property
    def available(self) -> bool:
        return super().available and bool(self._data())

    @property
    def native_value(self):
        return self._data().get("rp_kwh")

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return {
            "base_price_chf_month": d.get("base_price_chf"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "comment": d.get("comment"),
        }


def _calculation_components(coordinator: ElcomTariffCoordinator, entry: ConfigEntry) -> dict | None:
    data = coordinator.data or {}
    municipality = entry.data.get(CONF_MUNICIPALITY, "")
    energy = data.get("energy", {}).get(entry.data[CONF_ENERGY_PRODUCT])
    grid = data.get("grid", {}).get(entry.data[CONF_GRID_PRODUCT])
    municipalities = data.get("municipalities", {})
    if municipalities:
        # The chosen municipality vanished from the file: unavailable, not a price without its levy.
        municipality_data = municipalities.get(municipality)
    else:
        # The file carries no municipality section at all (some operators
        # publish none, others drop it from one year to the next): no
        # municipal levy is added.
        municipality_data = {"rp_kwh": 0.0}
    if not energy or not grid or not municipality_data:
        return None
    return {"energy": energy, "grid": grid, "municipality": municipality_data}


class ElcomPriceCurrentSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined current electricity price for the configured municipality +
    customer tariff, including national levies and VAT. The energy and grid
    rates are the ones active right now (resolved on every read)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("current_price_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_price_current"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_price_current"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    def _components(self) -> dict | None:
        return _calculation_components(self.coordinator, self._entry)

    @property
    def available(self) -> bool:
        # The configured product or municipality vanished from the file: unavailable, not a stale price.
        return super().available and self._components() is not None

    @property
    def native_value(self):
        c = self._components()
        if not c:
            return None
        now = dt_util.now()
        total_rp = (
            (current_rate(c["energy"], now)[0] or 0)
            + (current_rate(c["grid"], now)[0] or 0)
            + (c["municipality"]["rp_kwh"] or 0)
            + self._national_levies_rp()
        )
        total_rp *= 1 + self._vat_percent() / 100
        return round(total_rp / 100, 5)

    @property
    def extra_state_attributes(self):
        c = self._components()
        if not c:
            return {}
        now = dt_util.now()
        energy_rp_now, energy_level = current_rate(c["energy"], now)
        energy_rp = energy_rp_now or 0
        grid_rp = current_rate(c["grid"], now)[0] or 0
        municipality_rp = c["municipality"]["rp_kwh"] or 0
        national_rp = self._national_levies_rp()
        vat = self._vat_percent()
        subtotal_rp = energy_rp + grid_rp + municipality_rp + national_rp
        final_rp = subtotal_rp * (1 + vat / 100)
        energy_product = self._entry.data[CONF_ENERGY_PRODUCT]
        grid_product = self._entry.data[CONF_GRID_PRODUCT]
        municipality = self._entry.data.get(CONF_MUNICIPALITY, "")
        calculation = t(
            "calculation_formula",
            self._hass_ref,
            energy_rp=energy_rp,
            grid_rp=grid_rp,
            municipality_rp=municipality_rp,
            national_rp=national_rp,
            subtotal_rp=subtotal_rp,
            factor=1 + vat / 100,
            vat=vat,
            final_rp=final_rp,
            final_chf=final_rp / 100,
            energy_product=energy_product,
            grid_product=grid_product,
            municipality=municipality,
        )
        return {
            "calculation": calculation,
            "tariff_level": tariff_level_text(energy_level, self._hass_ref),
            # None for entries created before 1.5.0 (the choice did not exist yet).
            "grid_includes_national_levies": self._entry.data.get(CONF_GRID_INCLUDES_LEVIES),
            "energy_rp_kwh": energy_rp,
            "grid_rp_kwh": grid_rp,
            "municipality_rp_kwh": municipality_rp,
            "national_levies_rp_kwh": national_rp,
            "vat_percent": vat,
            "municipality": municipality,
            "energy_product": energy_product,
            "grid_product": grid_product,
            **localized(
                schedule_attributes(
                    combined_price_at(c["energy"], c["grid"], municipality_rp, national_rp, vat),
                    [c["energy"], c["grid"]],
                    now,
                ),
                self._hass_ref,
            ),
        }


class ElcomPriceLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined high-tariff or low-tariff reference price (fixed, independent
    of the current time)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, level: str, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._level = level
        label = t("tariff_level_high", hass) if level == "high" else t("tariff_level_low", hass)
        self._attr_name = t("price_level_name", hass, level=label)
        self._attr_unique_id = f"{entry.entry_id}_price_{level}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_price_{level}"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    def _components(self) -> dict | None:
        return _calculation_components(self.coordinator, self._entry)

    def _key(self) -> str:
        return "high_rp_kwh" if self._level == "high" else "low_rp_kwh"

    @property
    def available(self) -> bool:
        return super().available and self._components() is not None

    @property
    def native_value(self):
        c = self._components()
        if not c:
            return None
        energy_rp = c["energy"].get(self._key())
        grid_rp = c["grid"].get(self._key())
        if energy_rp is None or grid_rp is None:
            return None
        total_rp = energy_rp + grid_rp + (c["municipality"]["rp_kwh"] or 0) + self._national_levies_rp()
        total_rp *= 1 + self._vat_percent() / 100
        return round(total_rp / 100, 5)

    @property
    def extra_state_attributes(self):
        c = self._components()
        if not c:
            return {}
        energy_rp = c["energy"].get(self._key())
        grid_rp = c["grid"].get(self._key())
        if energy_rp is None or grid_rp is None:
            return {}
        municipality_rp = c["municipality"]["rp_kwh"] or 0
        national_rp = self._national_levies_rp()
        vat = self._vat_percent()
        subtotal_rp = energy_rp + grid_rp + municipality_rp + national_rp
        final_rp = subtotal_rp * (1 + vat / 100)
        energy_product = self._entry.data[CONF_ENERGY_PRODUCT]
        grid_product = self._entry.data[CONF_GRID_PRODUCT]
        municipality = self._entry.data.get(CONF_MUNICIPALITY, "")
        calculation = t(
            "calculation_formula",
            self._hass_ref,
            energy_rp=energy_rp,
            grid_rp=grid_rp,
            municipality_rp=municipality_rp,
            national_rp=national_rp,
            subtotal_rp=subtotal_rp,
            factor=1 + vat / 100,
            vat=vat,
            final_rp=final_rp,
            final_chf=final_rp / 100,
            energy_product=energy_product,
            grid_product=grid_product,
            municipality=municipality,
        )
        return {
            "calculation": calculation,
            "energy_rp_kwh": energy_rp,
            "grid_rp_kwh": grid_rp,
            "municipality_rp_kwh": municipality_rp,
            "national_levies_rp_kwh": national_rp,
            "vat_percent": vat,
            "municipality": municipality,
            "energy_product": energy_product,
            "grid_product": grid_product,
        }


def _level_rp(product: dict, key: str) -> float:
    """A product's high or low rate; a flat product has only its one rate."""
    value = product.get(key)
    return value if value is not None else product["windows"][0]["rp_kwh"]


class ElcomNextPeriodPriceSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Preview of the combined price under the operator's NEXT tariff
    period (next year's tariffs, which operators publish months ahead):
    the same formula as the current price sensor, evaluated with the
    upcoming set at this very moment, so the two are directly comparable.
    Attributes carry the fixed high/low prices of both periods and the
    change in percent. Unavailable while the file holds no upcoming set
    or the configured products are not in it."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:calendar-arrow-right"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("next_period_price_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_price_next_period"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_price_next_period"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    def _components(self) -> dict | None:
        """Next period's energy/grid product and municipal levy (next
        period's own levy when it publishes one, else today's)."""
        data = self.coordinator.data or {}
        upcoming = data.get("next_period")
        if not upcoming:
            return None
        energy = upcoming.get("energy", {}).get(self._entry.data.get(CONF_ENERGY_PRODUCT))
        grid = upcoming.get("grid", {}).get(self._entry.data.get(CONF_GRID_PRODUCT))
        if not energy or not grid:
            return None
        municipality = self._entry.data.get(CONF_MUNICIPALITY, "")
        municipalities = upcoming.get("municipalities") or data.get("municipalities", {})
        levy = municipalities.get(municipality) if municipalities else {"rp_kwh": 0.0}
        if levy is None:
            return None
        return {"energy": energy, "grid": grid, "municipality_rp": levy.get("rp_kwh") or 0.0, "valid_from": upcoming["valid_from"]}

    def _combined(self, energy_rp: float, grid_rp: float, municipality_rp: float) -> float:
        total_rp = energy_rp + grid_rp + municipality_rp + self._national_levies_rp()
        return round(total_rp * (1 + self._vat_percent() / 100) / 100, 5)

    @property
    def available(self) -> bool:
        return super().available and self._components() is not None

    @property
    def native_value(self):
        c = self._components()
        if not c:
            return None
        now = dt_util.now()
        return self._combined(
            current_rate(c["energy"], now)[0] or 0, current_rate(c["grid"], now)[0] or 0, c["municipality_rp"]
        )

    @property
    def extra_state_attributes(self):
        c = self._components()
        if not c:
            return {}
        current = _calculation_components(self.coordinator, self._entry)
        attrs = {
            "valid_from": c["valid_from"],
            "energy_product": self._entry.data.get(CONF_ENERGY_PRODUCT),
            "grid_product": self._entry.data.get(CONF_GRID_PRODUCT),
            "municipality": self._entry.data.get(CONF_MUNICIPALITY, ""),
            "energy_rp_kwh_high": _level_rp(c["energy"], "high_rp_kwh"),
            "energy_rp_kwh_low": _level_rp(c["energy"], "low_rp_kwh"),
            "grid_rp_kwh_high": _level_rp(c["grid"], "high_rp_kwh"),
            "grid_rp_kwh_low": _level_rp(c["grid"], "low_rp_kwh"),
            "municipality_rp_kwh": c["municipality_rp"],
        }
        for level in ("high", "low"):
            key = f"{level}_rp_kwh"
            new = self._combined(_level_rp(c["energy"], key), _level_rp(c["grid"], key), c["municipality_rp"])
            attrs[f"price_{level}"] = new
            if current:
                old = self._combined(
                    _level_rp(current["energy"], key),
                    _level_rp(current["grid"], key),
                    current["municipality"]["rp_kwh"] or 0,
                )
                attrs[f"current_price_{level}"] = old
                attrs[f"change_{level}_percent"] = round((new - old) / old * 100, 1) if old else None
        return attrs


def _manual_entities(
    hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
) -> list[SensorEntity]:
    """Entities for a manually-entered tariff (no operator tariff file could be resolved)."""
    data = coordinator.data or {}
    entities: list[SensorEntity] = [ElcomManualPriceCurrentSensor(hass, coordinator, entry, prefix)]
    if data.get("low_rp_kwh") is not None:
        entities.append(ElcomManualTariffLevelSensor(hass, coordinator, entry, prefix))
        entities.append(ElcomManualPriceLevelSensor(hass, coordinator, entry, "high", prefix))
        entities.append(ElcomManualPriceLevelSensor(hass, coordinator, entry, "low", prefix))
    if data.get("grid_fee_chf_month") is not None:
        entities.append(ElcomManualGridFeeSensor(hass, coordinator, entry, prefix))
    return entities


class ElcomManualTariffLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Shows whether the manually entered tariff is currently on high or low
    tariff — resolved against the clock on every read."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:clock-time-eight-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._hass_ref = hass
        self._attr_name = t("tariff_status_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_tariff_status"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_tariff_level"

    @property
    def native_value(self):
        _, level = current_rate(self.coordinator.data or {}, dt_util.now())
        return tariff_level_text(level, self._hass_ref)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        if not d:
            return {}
        return {
            "schedule": d.get("schedule"),
            **localized(level_change_attributes(level_at(d), [d], dt_util.now()), self._hass_ref),
        }


class ElcomManualPriceCurrentSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined current price for a manually entered tariff, including
    national levies and VAT."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("current_price_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_price_current"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_price_current"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    @property
    def native_value(self):
        rp, _ = current_rate(self.coordinator.data or {}, dt_util.now())
        if rp is None:
            return None
        total_rp = (rp + self._national_levies_rp()) * (1 + self._vat_percent() / 100)
        return round(total_rp / 100, 5)

    @property
    def extra_state_attributes(self):
        rp, level = current_rate(self.coordinator.data or {}, dt_util.now())
        if rp is None:
            return {}
        national_rp = self._national_levies_rp()
        vat = self._vat_percent()
        subtotal_rp = rp + national_rp
        final_rp = subtotal_rp * (1 + vat / 100)
        calculation = t(
            "calculation_formula_manual",
            self._hass_ref,
            tariff_rp=rp,
            national_rp=national_rp,
            subtotal_rp=subtotal_rp,
            factor=1 + vat / 100,
            vat=vat,
            final_rp=final_rp,
            final_chf=final_rp / 100,
        )
        data = self.coordinator.data or {}
        return {
            "calculation": calculation,
            "tariff_level": tariff_level_text(level, self._hass_ref),
            "tariff_rp_kwh": rp,
            "national_levies_rp_kwh": national_rp,
            "vat_percent": vat,
            **localized(
                schedule_attributes(manual_price_at(data, national_rp, vat), [data], dt_util.now()),
                self._hass_ref,
            ),
        }


class ElcomManualPriceLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined fixed high-tariff or low-tariff reference price for a
    manually entered tariff (same formula, fixed rate instead of current)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, level: str, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._level = level
        label = t("tariff_level_high", hass) if level == "high" else t("tariff_level_low", hass)
        self._attr_name = t("price_level_name", hass, level=label)
        self._attr_unique_id = f"{entry.entry_id}_price_{level}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_price_{level}"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    @property
    def native_value(self):
        key = "high_rp_kwh" if self._level == "high" else "low_rp_kwh"
        rp = (self.coordinator.data or {}).get(key)
        if rp is None:
            return None
        total_rp = (rp + self._national_levies_rp()) * (1 + self._vat_percent() / 100)
        return round(total_rp / 100, 5)


class ElcomManualGridFeeSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Manually entered monthly grid/metering fee."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/month"
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, prefix: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t("grid_fee_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_grid_fee"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.{prefix}_base_fee"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("grid_fee_chf_month")
