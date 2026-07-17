"""Swiss Electricity Price (ElCom) integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ENERGY_PRODUCT, CONF_GRID_PRODUCT, CONF_MANUAL_HIGH_RP, DOMAIN
from .coordinator import ElcomManualTariffCoordinator, ElcomTariffCoordinator

PLATFORMS = ["sensor", "number"]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """v1 -> v2: the energy and grid tariff product used for the combined
    price sensors are now picked directly by the user (see config_flow.py)
    instead of both being derived from a single "customer_type" selection
    via a naming-convention guess (f"{customer_type} Energie"/" Netz") that
    doesn't reliably hold across operators. Reproduce that same guess here
    for already-configured entries, so setups where it happened to match
    (like most real ones) keep working unchanged; entries where it didn't
    match anything keep behaving exactly as before (no combined-price
    sensors) — this migration only renames the stored keys, it does not
    change matching behavior for existing entries either way.
    """
    if entry.version > 1:
        return True
    data = {**entry.data}
    customer_type = data.pop("customer_type", None)
    if customer_type is not None:
        data[CONF_ENERGY_PRODUCT] = f"{customer_type} Energie"
        data[CONF_GRID_PRODUCT] = f"{customer_type} Netz"
    hass.config_entries.async_update_entry(entry, data=data, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = (
        ElcomManualTariffCoordinator(hass, entry)
        if CONF_MANUAL_HIGH_RP in entry.data
        else ElcomTariffCoordinator(hass, entry)
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # The national-levies/VAT number entities are plain config-entry-option
    # values with no dependency on the coordinator's data at all, so set
    # them up before the (network-dependent, possibly slow or briefly
    # unreachable right after a HA restart) first refresh below — otherwise
    # a transient startup hiccup on the operator's tariff-file host leaves
    # them stuck showing "unavailable" until the whole entry setup retries,
    # even though they never needed the network in the first place.
    await hass.config_entries.async_forward_entry_setups(entry, ["number"])

    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Push the coordinator's already-fetched data to every entity again
    when options (national levies / VAT rate) change, so the combined-price
    sensors — which read entry.options live on every access but only get
    polled again on the next 24h coordinator refresh — pick up the new rate
    immediately. A full async_reload() would also work, but re-fetches the
    operator's tariff file over the network and briefly drops every entity
    to "unavailable" just to update two locally-computed numbers."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.async_update_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
