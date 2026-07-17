"""Sensors for the Swiss Electricity Price (ElCom) integration — all tariff
products + municipality levies + combined price, for the selected grid operator."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
    CONF_ENERGY_PRODUCT,
    CONF_GRID_PRODUCT,
    CONF_MANUAL_HIGH_RP,
    CONF_MUNICIPALITY,
    CONF_NATIONAL_LEVIES_RP,
    CONF_VAT_PERCENT,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
    DOMAIN,
)
from .coordinator import ElcomTariffCoordinator
from .device import device_info
from .localization import t, tariff_level_text

_YEAR_RE = re.compile(r"(20\d{2})")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: ElcomTariffCoordinator = hass.data[DOMAIN][entry.entry_id]
    slug = slugify(entry.data.get("operator_name", entry.entry_id))

    if CONF_MANUAL_HIGH_RP in entry.data:
        async_add_entities(_manual_entities(hass, coordinator, entry, slug))
        return

    data = coordinator.data or {}
    entities: list[SensorEntity] = [
        ElcomDiagnosticHostSensor(hass, coordinator, entry, slug),
        ElcomDiagnosticPathSensor(hass, coordinator, entry, slug),
        ElcomDiagnosticFilenameSensor(hass, coordinator, entry, slug),
        ElcomDiagnosticYearSensor(hass, coordinator, entry, slug),
        ElcomDiagnosticLastCheckedSensor(hass, coordinator, entry, slug),
    ]
    for category in ("energy", "grid"):
        for name, d in data.get(category, {}).items():
            entities.append(ElcomTariffSensor(hass, coordinator, entry, category, name, slug))
            if d.get("high_rp_kwh") is not None:
                entities.append(ElcomTariffLevelSensor(hass, coordinator, entry, category, name, "high_rp_kwh", slug))
                entities.append(ElcomTariffLevelSensor(hass, coordinator, entry, category, name, "low_rp_kwh", slug))
    for name in data.get("metering", {}):
        entities.append(ElcomMeteringSensor(hass, coordinator, entry, name, slug))
    for name in data.get("municipalities", {}):
        entities.append(ElcomMunicipalityLevySensor(hass, coordinator, entry, name, slug))

    if entry.data.get(CONF_MUNICIPALITY) and entry.data.get(CONF_ENERGY_PRODUCT) and entry.data.get(CONF_GRID_PRODUCT):
        entities.append(ElcomTariffStatusSensor(hass, coordinator, entry, slug))
        entities.append(ElcomPriceCurrentSensor(hass, coordinator, entry, slug))
        entities.append(ElcomPriceLevelSensor(hass, coordinator, entry, "high", slug))
        entities.append(ElcomPriceLevelSensor(hass, coordinator, entry, "low", slug))

    async_add_entities(entities)


class ElcomTariffSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Currently active Rp rate for an energy or grid tariff product."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry,
        category: str, name: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._hass_ref = hass
        self._category = category
        self._name_key = name
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_{category}_{slugify(name)}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_{slugify(name)}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._category, {}).get(self._name_key, {})

    @property
    def native_value(self):
        rp = self._data().get("current_rp_kwh")
        return round(rp / 100, 5) if rp is not None else None

    @property
    def extra_state_attributes(self):
        d = self._data()
        if not d:
            return {}
        return {
            "current_level": tariff_level_text(d.get("current_level"), self._hass_ref),
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
        category: str, name: str, level_key: str, slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._category = category
        self._name_key = name
        self._level_key = level_key
        suffix = "high" if level_key == "high_rp_kwh" else "low"
        level_label = t("tariff_level_high", hass) if suffix == "high" else t("tariff_level_low", hass)
        self._attr_name = f"{name} ({level_label})"
        self._attr_unique_id = f"{entry.entry_id}_{category}_{slugify(name)}_{suffix}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_{slugify(name)}_{suffix}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._category, {}).get(self._name_key, {})

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
    """The tariff data's own validity year (from its startDate) — NOT a date
    that might be embedded in the file name. Operators commonly publish next
    year's tariffs months in advance, so a file dated e.g. August 2025 can
    (and often does) contain tariffs valid from 2026, not 2025."""
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
        slug: str, name_key: str, id_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t(name_key, hass)
        self._attr_unique_id = f"{entry.entry_id}_diagnostics_{id_suffix}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_diagnostics_{id_suffix}"


class ElcomDiagnosticHostSensor(_ElcomDiagnosticSensorBase):
    """The host of the tariff-file URL actually being fetched (after
    resolving the operator's registered link, which may itself point at a
    landing page rather than the file directly). Split out from the path and
    filename, which have their own sensors, so no single value runs too long
    to fit the diagnostics card. The full URL is available as an attribute
    for copy-pasting."""

    _attr_icon = "mdi:web"

    def __init__(self, hass, coordinator, entry, slug):
        super().__init__(hass, coordinator, entry, slug, "diagnostics_host_name", "host")

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

    def __init__(self, hass, coordinator, entry, slug):
        super().__init__(hass, coordinator, entry, slug, "diagnostics_path_name", "path")

    @property
    def native_value(self):
        return _diagnostic_path(self.coordinator.data or {})


class ElcomDiagnosticFilenameSensor(_ElcomDiagnosticSensorBase):
    """The current tariff file's name, taken from the resolved URL."""

    _attr_icon = "mdi:file-outline"

    def __init__(self, hass, coordinator, entry, slug):
        super().__init__(hass, coordinator, entry, slug, "diagnostics_filename_name", "filename")

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

    def __init__(self, hass, coordinator, entry, slug):
        super().__init__(hass, coordinator, entry, slug, "diagnostics_year_name", "year")

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

    def __init__(self, hass, coordinator, entry, slug):
        super().__init__(hass, coordinator, entry, slug, "diagnostics_last_checked_name", "last_checked")

    @property
    def native_value(self):
        last_checked = (self.coordinator.data or {}).get("last_checked")
        return dt_util.parse_datetime(last_checked) if last_checked else None


class ElcomTariffStatusSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Shows whether the configured customer tariff is currently on high or low tariff."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:clock-time-eight-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("tariff_status_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_tariff_status"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_tariff_level"

    def _energy(self) -> dict:
        data = self.coordinator.data or {}
        return data.get("energy", {}).get(self._entry.data[CONF_ENERGY_PRODUCT], {})

    @property
    def native_value(self):
        return tariff_level_text(self._energy().get("current_level"), self._hass_ref)

    @property
    def extra_state_attributes(self):
        d = self._energy()
        if not d:
            return {}
        return {
            "energy_product": self._entry.data[CONF_ENERGY_PRODUCT],
            "schedule": d.get("schedule"),
        }


class ElcomMeteringSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Fixed monthly metering fee."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/M"
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, name: str, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._name_key = name
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_metering_{slugify(name)}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_{slugify(name)}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get("metering", {}).get(self._name_key, {})

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
    """Municipality levy (CHF/kWh) for a single municipality served by the operator."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "Rp/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:town-hall"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, name: str, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._name_key = name
        self._attr_name = f"{t('municipality_levy_prefix', hass)} {name}"
        self._attr_unique_id = f"{entry.entry_id}_municipality_{slugify(name)}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_municipality_levy_{slugify(name)}"

    def _data(self) -> dict:
        return (self.coordinator.data or {}).get("municipalities", {}).get(self._name_key, {})

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
        }


def _calculation_components(coordinator: ElcomTariffCoordinator, entry: ConfigEntry) -> dict | None:
    data = coordinator.data or {}
    municipality = entry.data[CONF_MUNICIPALITY]
    energy = data.get("energy", {}).get(entry.data[CONF_ENERGY_PRODUCT])
    grid = data.get("grid", {}).get(entry.data[CONF_GRID_PRODUCT])
    municipality_data = data.get("municipalities", {}).get(municipality)
    if not energy or not grid or not municipality_data:
        return None
    return {"energy": energy, "grid": grid, "municipality": municipality_data}


class ElcomPriceCurrentSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined current electricity price for the configured municipality +
    customer tariff, including national levies and VAT."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("current_price_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_price_current"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_price_current"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    def _components(self) -> dict | None:
        return _calculation_components(self.coordinator, self._entry)

    @property
    def native_value(self):
        c = self._components()
        if not c:
            return None
        total_rp = (
            (c["energy"]["current_rp_kwh"] or 0)
            + (c["grid"]["current_rp_kwh"] or 0)
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
        energy_rp = c["energy"]["current_rp_kwh"] or 0
        grid_rp = c["grid"]["current_rp_kwh"] or 0
        municipality_rp = c["municipality"]["rp_kwh"] or 0
        national_rp = self._national_levies_rp()
        vat = self._vat_percent()
        subtotal_rp = energy_rp + grid_rp + municipality_rp + national_rp
        final_rp = subtotal_rp * (1 + vat / 100)
        energy_product = self._entry.data[CONF_ENERGY_PRODUCT]
        grid_product = self._entry.data[CONF_GRID_PRODUCT]
        municipality = self._entry.data[CONF_MUNICIPALITY]
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
            "tariff_level": tariff_level_text(c["energy"]["current_level"], self._hass_ref),
            "energy_rp_kwh": energy_rp,
            "grid_rp_kwh": grid_rp,
            "municipality_rp_kwh": municipality_rp,
            "national_levies_rp_kwh": national_rp,
            "vat_percent": vat,
            "municipality": municipality,
            "energy_product": energy_product,
            "grid_product": grid_product,
        }


class ElcomPriceLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined high-tariff or low-tariff reference price (fixed, independent
    of the current time)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, level: str, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._level = level
        label = t("tariff_level_high", hass) if level == "high" else t("tariff_level_low", hass)
        self._attr_name = t("price_level_name", hass, level=label)
        self._attr_unique_id = f"{entry.entry_id}_price_{level}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_price_{level}"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    def _components(self) -> dict | None:
        return _calculation_components(self.coordinator, self._entry)

    def _key(self) -> str:
        return "high_rp_kwh" if self._level == "high" else "low_rp_kwh"

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
        municipality = self._entry.data[CONF_MUNICIPALITY]
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


def _manual_entities(
    hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
) -> list[SensorEntity]:
    """Entities for a manually-entered tariff (no operator tariff file could be resolved)."""
    data = coordinator.data or {}
    entities: list[SensorEntity] = [ElcomManualPriceCurrentSensor(hass, coordinator, entry, slug)]
    if data.get("low_rp_kwh") is not None:
        entities.append(ElcomManualTariffLevelSensor(hass, coordinator, entry, slug))
        entities.append(ElcomManualPriceLevelSensor(hass, coordinator, entry, "high", slug))
        entities.append(ElcomManualPriceLevelSensor(hass, coordinator, entry, "low", slug))
    if data.get("grid_fee_chf_month") is not None:
        entities.append(ElcomManualGridFeeSensor(hass, coordinator, entry, slug))
    return entities


class ElcomManualTariffLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Shows whether the manually entered tariff is currently on high or low tariff."""

    _attr_has_entity_name = False
    _attr_icon = "mdi:clock-time-eight-outline"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._hass_ref = hass
        self._attr_name = t("tariff_status_sensor_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_tariff_status"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_tariff_level"

    @property
    def native_value(self):
        return tariff_level_text((self.coordinator.data or {}).get("current_level"), self._hass_ref)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        return {"schedule": d.get("schedule")} if d else {}


class ElcomManualPriceCurrentSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined current price for a manually entered tariff, including
    national levies and VAT."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._hass_ref = hass
        self._attr_name = t("current_price_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_price_current"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_price_current"

    def _national_levies_rp(self) -> float:
        return self._entry.options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP)

    def _vat_percent(self) -> float:
        return self._entry.options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT)

    @property
    def native_value(self):
        rp = (self.coordinator.data or {}).get("current_rp_kwh")
        if rp is None:
            return None
        total_rp = (rp + self._national_levies_rp()) * (1 + self._vat_percent() / 100)
        return round(total_rp / 100, 5)

    @property
    def extra_state_attributes(self):
        d = self.coordinator.data or {}
        rp = d.get("current_rp_kwh")
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
        return {
            "calculation": calculation,
            "tariff_level": tariff_level_text(d.get("current_level"), self._hass_ref),
            "tariff_rp_kwh": rp,
            "national_levies_rp_kwh": national_rp,
            "vat_percent": vat,
        }


class ElcomManualPriceLevelSensor(CoordinatorEntity[ElcomTariffCoordinator], SensorEntity):
    """Combined fixed high-tariff or low-tariff reference price for a
    manually entered tariff (same formula, fixed rate instead of current)."""

    _attr_has_entity_name = False
    _attr_native_unit_of_measurement = "CHF/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, level: str, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._level = level
        label = t("tariff_level_high", hass) if level == "high" else t("tariff_level_low", hass)
        self._attr_name = t("price_level_name", hass, level=label)
        self._attr_unique_id = f"{entry.entry_id}_price_{level}"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_price_{level}"

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
    _attr_native_unit_of_measurement = "CHF/M"
    _attr_icon = "mdi:meter-electric"

    def __init__(
        self, hass: HomeAssistant, coordinator: ElcomTariffCoordinator, entry: ConfigEntry, slug: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_name = t("grid_fee_name", hass)
        self._attr_unique_id = f"{entry.entry_id}_grid_fee"
        self._attr_device_info = device_info(hass, entry)
        self.entity_id = f"sensor.elcom_{slug}_grundgebuehr"

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("grid_fee_chf_month")
