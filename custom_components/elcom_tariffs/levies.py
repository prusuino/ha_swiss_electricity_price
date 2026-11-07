"""The levies that sit on top of an operator's energy and grid rates, and
the one place the combined price is put together.

Four kinds of levy reach the bill:

- the national levies (grid surcharge, system services, electricity
  reserve, solidarity surcharge), one adjustable figure in Rp/kWh
  subject to VAT (0 when the grid price already contains them);
- the municipal levy of the chosen municipality, from the operator's
  file, subject to VAT;
- cantonal levies the operator publishes in the file's `cantonalTaxes`
  section. Which of them apply is chosen per entry, because operators
  fill that section very differently (see coordinator.py); each chosen
  entry is either subject to VAT or exempt from it — a cantonal tax such
  as Vaud's 0.6 Rp/kWh renewable-energy tax is a tax, not a price, and
  is billed without VAT, while the cantonal fee next to it is billed
  with VAT;
- an adjustable amount exempt from VAT, for a levy the file does not
  carry (Rp/kWh, default 0).

    price = (energy + grid + municipality + national + cantonal taxed) × (1 + VAT)
            + cantonal exempt + exempt amount

Every combined-price figure of the integration — the current price and
the high/low reference sensors, the next-period preview, the price curve
and the actions — goes through `Levies.total_rp`, so they cannot drift
apart.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CANTONAL_LEVIES,
    CONF_CANTONAL_LEVIES_VAT_EXEMPT,
    CONF_NATIONAL_LEVIES_RP,
    CONF_VAT_EXEMPT_LEVIES_RP,
    CONF_VAT_PERCENT,
    DEFAULT_NATIONAL_LEVIES_RP,
    DEFAULT_VAT_EXEMPT_LEVIES_RP,
    DEFAULT_VAT_PERCENT,
)
from .coordinator import current_rate


def level_rp(product: dict, key: str | None) -> float:
    """A parsed product's high or low rate (`high_rp_kwh` / `low_rp_kwh`);
    a flat product has only its one rate, which serves for both."""
    value = product.get(key) if key else None
    if value is not None:
        return value
    windows = product.get("windows") or []
    return windows[0]["rp_kwh"] if windows else 0.0


def rate_at(product: dict, when: datetime | None, level_key: str | None) -> float:
    """A parsed product's rate at a moment, or at a tariff level when no
    moment is given (the fixed high/low reference prices)."""
    if when is not None:
        rp, _level = current_rate(product, when)
        return rp if rp is not None else 0.0
    return level_rp(product, level_key)


@dataclass(frozen=True)
class Levies:
    """One entry's levies, resolved against the fetched data."""

    national_rp: float
    vat_percent: float
    exempt_rp: float
    # (name, parsed product, exempt from VAT) of every chosen cantonal levy
    # the data carries. A chosen levy the file no longer has is left out —
    # the price is then too low rather than unavailable, and the repair
    # issue for a vanished selection names it (see coordinator.py).
    cantonal: tuple[tuple[str, dict, bool], ...] = ()

    @property
    def factor(self) -> float:
        return 1 + self.vat_percent / 100

    def cantonal_rp(self, when: datetime | None = None, level_key: str | None = None) -> tuple[float, float]:
        """(sum subject to VAT, sum exempt from VAT) of the chosen
        cantonal levies, in Rp/kWh, at a moment or tariff level."""
        taxed = exempt = 0.0
        for _name, product, is_exempt in self.cantonal:
            rp = rate_at(product, when, level_key)
            if is_exempt:
                exempt += rp
            else:
                taxed += rp
        return round(taxed, 5), round(exempt, 5)

    def total_rp(
        self,
        energy_rp: float,
        grid_rp: float,
        municipality_rp: float = 0.0,
        when: datetime | None = None,
        level_key: str | None = None,
    ) -> float:
        """The combined price in Rp/kWh (unrounded)."""
        taxed, exempt = self.cantonal_rp(when, level_key)
        subtotal = energy_rp + grid_rp + municipality_rp + self.national_rp + taxed
        return subtotal * self.factor + exempt + self.exempt_rp

    def total_chf(self, energy_rp: float, grid_rp: float, municipality_rp: float = 0.0, **kwargs) -> float:
        """The combined price in CHF/kWh, rounded as the sensors show it."""
        return round(self.total_rp(energy_rp, grid_rp, municipality_rp, **kwargs) / 100, 5)

    def breakdown(
        self,
        energy_rp: float,
        grid_rp: float,
        municipality_rp: float = 0.0,
        when: datetime | None = None,
        level_key: str | None = None,
    ) -> dict:
        """Every component of the price, for the `calculation` text and
        the per-component attributes of the combined-price sensors."""
        taxed, exempt = self.cantonal_rp(when, level_key)
        subtotal = energy_rp + grid_rp + municipality_rp + self.national_rp + taxed
        taxed_total = subtotal * self.factor
        final = taxed_total + exempt + self.exempt_rp
        return {
            "energy_rp": energy_rp,
            "grid_rp": grid_rp,
            "municipality_rp": municipality_rp,
            "national_rp": self.national_rp,
            "cantonal_rp": taxed,
            "subtotal_rp": subtotal,
            "factor": self.factor,
            "vat": self.vat_percent,
            "taxed_rp": taxed_total,
            "cantonal_exempt_rp": exempt,
            "exempt_rp": self.exempt_rp,
            "final_rp": final,
            "final_chf": final / 100,
        }

    def attributes(self) -> dict:
        """The levy settings as sensor attributes."""
        return {
            "national_levies_rp_kwh": self.national_rp,
            "vat_percent": self.vat_percent,
            "cantonal_levies": [name for name, _product, _exempt in self.cantonal],
            "cantonal_levies_vat_exempt": [name for name, _product, exempt in self.cantonal if exempt],
            "vat_exempt_levies_rp_kwh": self.exempt_rp,
        }


def entry_levies(entry: ConfigEntry, data: dict | None, cantonal_source: dict | None = None) -> Levies:
    """The entry's levies: the adjustable figures from its options and the
    chosen cantonal levies from the fetched data (`cantonal_source`, when
    given, supplies the cantonal section instead — the next period's)."""
    options = entry.options
    source = cantonal_source if cantonal_source is not None else (data or {})
    available = source.get("cantonal") or {}
    chosen = entry.data.get(CONF_CANTONAL_LEVIES) or []
    exempt = set(entry.data.get(CONF_CANTONAL_LEVIES_VAT_EXEMPT) or [])
    cantonal = tuple(
        (name, available[name], name in exempt) for name in chosen if name in available
    )
    return Levies(
        national_rp=options.get(CONF_NATIONAL_LEVIES_RP, DEFAULT_NATIONAL_LEVIES_RP),
        vat_percent=options.get(CONF_VAT_PERCENT, DEFAULT_VAT_PERCENT),
        exempt_rp=options.get(CONF_VAT_EXEMPT_LEVIES_RP, DEFAULT_VAT_EXEMPT_LEVIES_RP),
        cantonal=cantonal,
    )
