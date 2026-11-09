"""Diagnostics download (Settings → Devices & services → entry menu →
Download diagnostics): what the entry is configured with and what was
parsed from the operator's file, so an issue report can carry the facts
instead of a description of them. The file contents themselves are not
stored by the coordinator; the resolved URL is, and the file is public.

The entry title and unique id are redacted. The municipality stays in the
entry data: the levy and the product selection cannot be checked without it."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MUNICIPALITY, DOMAIN, USER_AGENT


def _product_summary(products: dict[str, dict]) -> dict[str, Any]:
    return {
        name: {
            "high_rp_kwh": d.get("high_rp_kwh"),
            "low_rp_kwh": d.get("low_rp_kwh"),
            "base_price": d.get("base_price_chf_month"),
            "base_price_unit": d.get("base_price_unit"),
            "customer_type": d.get("customer_type"),
            "voltage_level": d.get("voltage_level"),
            "valid_from": d.get("valid_from"),
            "valid_to": d.get("valid_to"),
            "schedule": d.get("schedule"),
        }
        for name, d in products.items()
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = getattr(entry, "runtime_data", None)
    data = (coordinator.data if coordinator is not None else None) or {}
    municipalities = data.get("municipalities", {})
    next_period = data.get("next_period") or {}
    return {
        "integration": USER_AGENT,
        "entry": {
            "title": "**REDACTED**",
            "version": entry.version,
            "unique_id": "**REDACTED**" if entry.unique_id else None,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": {
            "loaded": coordinator is not None,
            "last_update_success": coordinator.last_update_success if coordinator is not None else None,
            "dso_name": data.get("dso_name"),
            "registered_url": data.get("registered_url"),
            "tariff_url": data.get("tariff_url"),
            "last_checked": data.get("last_checked"),
            "tariff_valid_from": data.get("tariff_valid_from"),
            "tariff_valid_to": data.get("tariff_valid_to"),
            "tariff_period_end": data.get("tariff_period_end"),
            "tariff_selection": data.get("tariff_selection"),
            "energy_products": _product_summary(data.get("energy", {})),
            "grid_products": _product_summary(data.get("grid", {})),
            "metering": data.get("metering", {}),
            "municipality_count": len(municipalities),
            "municipality_names_sample": sorted(municipalities)[:20],
            # The chosen municipality's levy with every record the file has for it.
            "municipal_levy": municipalities.get(entry.data.get(CONF_MUNICIPALITY) or ""),
            "cantonal_levies": _product_summary(data.get("cantonal", {})),
            "next_period": {
                "valid_from": next_period.get("valid_from"),
                "energy_products": sorted(next_period.get("energy", {})),
                "grid_products": sorted(next_period.get("grid", {})),
                "municipality_count": len(next_period.get("municipalities", {})),
            }
            if next_period
            else None,
            # Manual entry: its own windows.
            "manual_windows": data.get("windows") if "windows" in data else None,
        },
    }
