"""A municipal levy that the operator publishes as a product: found only in
a file without a municipality section, added to the price, not offered as
a product, and never counted twice."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from custom_components.elcom_tariffs.config_flow import _choosable, _list_products
from custom_components.elcom_tariffs.const import CONF_ENERGY_PRODUCT, CONF_GRID_PRODUCT, CONF_MUNICIPALITY, CONF_NATIONAL_LEVIES_RP, CONF_VAT_PERCENT
from custom_components.elcom_tariffs.coordinator import ElcomTariffCoordinator
from custom_components.elcom_tariffs.municipal_levy import is_levy_product, levy_of_entry
from custom_components.elcom_tariffs.price_schedule import entry_price_at

from .test_tariffs import ENERGY, FEES, GRID

LEVY = {
    "tariffType": "electricity", "tariffName": "EW Abgabe an die Gemeinde", "standardBasegroup": False, "customerType": "Alle",
    "startDate": "2026-01-01", "endDate": "2026-12-31",
    "prices": {"base": {"price": 0, "priceUnit": "CHF/M"}, "energy": [{"from": "00:00", "to": "00:00", "price": 0.006, "priceUnit": "CHF/kWh"}]},
}


def _parse(tariffs, **kwargs):
    return ElcomTariffCoordinator._parse_tariff_records(tariffs, **kwargs)


def test_only_a_record_named_as_the_levy_with_one_rate_counts():
    assert is_levy_product(LEVY) is True
    for name in ("Gemeindeabgabe", "Abgaben Gemeinde", "Redevance communale", "Taxes communales", "Tassa comunale"):
        assert is_levy_product({**LEVY, "tariffName": name}) is True, name
    assert is_levy_product({**LEVY, "tariffType": "grid"}) is True
    # products that merely carry a place or a similar word in their name
    for name in ("Basic Energie", "Gemeinde Strom Plus", "Naturstrom Abgabe", "Energie Commune", "Konzessionsabgabe"):
        assert is_levy_product({**LEVY, "tariffName": name}) is False, name
    assert is_levy_product({**LEVY, "standardBasegroup": True}) is False
    assert is_levy_product({**LEVY, "tariffType": "regional_fees"}) is False
    assert is_levy_product({**LEVY, "prices": {"energy": []}}) is False
    assert is_levy_product({**LEVY, "prices": {"energy": [{"price": 0}]}}) is False
    assert is_levy_product({**LEVY, "prices": {"energy": [{"price": 0.006}, {"price": 0.004}]}}) is False  # two rates: a product
    assert is_levy_product(ENERGY) is False and is_levy_product(GRID) is False


def test_levy_published_as_a_product_is_added_to_the_price():
    parsed = _parse([ENERGY, GRID, LEVY], grid_product="Basic Netz", energy_product="Basic Energie")
    assert parsed["municipalities"] == {}
    assert parsed["product_levy"] == {"rp_kwh": 0.6, "product_name": "EW Abgabe an die Gemeinde", "valid_from": "2026-01-01", "valid_to": "2026-12-31"}
    # the record keeps its place among the products (its sensor stays), marked
    assert parsed["energy"]["EW Abgabe an die Gemeinde"]["municipal_levy"] is True
    assert "municipal_levy" not in parsed["energy"]["Basic Energie"]
    assert levy_of_entry(parsed, "")["rp_kwh"] == 0.6
    assert levy_of_entry(parsed, None)["rp_kwh"] == 0.6


def test_a_file_with_a_municipality_section_is_read_as_before():
    with_levy_record = _parse([ENERGY, GRID, FEES, LEVY], grid_product="Basic Netz", municipality="Musterwil")
    without = _parse([ENERGY, GRID, FEES], grid_product="Basic Netz", municipality="Musterwil")
    assert with_levy_record["product_levy"] is None and without["product_levy"] is None
    assert with_levy_record["municipalities"] == without["municipalities"]
    assert "municipal_levy" not in with_levy_record["energy"]["EW Abgabe an die Gemeinde"]  # an ordinary product there
    assert levy_of_entry(without, "Musterwil")["rp_kwh"] == 0.5
    assert levy_of_entry(without, "Gone") is None  # the chosen municipality vanished: no price without its levy
    assert levy_of_entry(with_levy_record, "Musterwil")["rp_kwh"] == 0.5


def test_a_file_with_neither_adds_nothing():
    parsed = _parse([ENERGY, GRID])
    assert parsed["product_levy"] is None
    assert levy_of_entry(parsed, "") == {"rp_kwh": 0.0}


def test_an_entry_set_to_the_levy_product_does_not_pay_it_twice():
    parsed = _parse([ENERGY, GRID, LEVY], grid_product="Basic Netz", energy_product="EW Abgabe an die Gemeinde")
    assert parsed["product_levy"] is None
    assert levy_of_entry(parsed, "") == {"rp_kwh": 0.0}


def test_next_years_set_brings_its_own_levy_and_falls_back_to_todays():
    today = _parse([ENERGY, GRID, LEVY])
    raised = {**LEVY, "startDate": "2027-01-01", "endDate": "2027-12-31", "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.009}]}}
    assert levy_of_entry(_parse([ENERGY, GRID, raised]), "", today)["rp_kwh"] == 0.9
    assert levy_of_entry(_parse([ENERGY, GRID]), "", today)["rp_kwh"] == 0.6
    # with municipality sections the same rule as ever: next year's, else today's
    fees_today = _parse([ENERGY, GRID, FEES], municipality="Musterwil")
    assert levy_of_entry(_parse([ENERGY, GRID]), "Musterwil", fees_today)["rp_kwh"] == 0.5


def test_the_levy_is_not_offered_as_a_product():
    raw = {"tariffs": [ENERGY, GRID, LEVY]}
    assert _list_products(raw, "electricity") == ["Basic Energie"]
    assert _list_products(raw, "grid") == ["Basic Netz"]
    # with a municipality section nothing is hidden
    assert _list_products({"tariffs": [ENERGY, GRID, FEES, LEVY]}, "electricity") == ["Basic Energie", "EW Abgabe an die Gemeinde"]

    products = _parse([ENERGY, GRID, LEVY])["energy"]
    assert _choosable(products, "Basic Energie") == ["Basic Energie"]
    assert _choosable(products, "EW Abgabe an die Gemeinde") == ["Basic Energie", "EW Abgabe an die Gemeinde"]  # an entry already set to it keeps it


def _price(tariffs, municipality=""):
    """The entry's price at a weekday night, as the combined-price sensor shows it."""
    data = {CONF_ENERGY_PRODUCT: "Basic Energie", CONF_GRID_PRODUCT: "Basic Netz", CONF_MUNICIPALITY: municipality}
    entry = SimpleNamespace(data=data, options={CONF_NATIONAL_LEVIES_RP: 0.0, CONF_VAT_PERCENT: 8.1})
    parsed = _parse(tariffs, grid_product="Basic Netz", energy_product="Basic Energie", municipality=municipality)
    found = entry_price_at(SimpleNamespace(data=parsed), entry)
    return None if found is None else found[0](datetime(2026, 11, 18, 23, 30))[0]


def test_the_price_of_an_entry_carries_the_levy():
    # 9 Rp energy at night + 8 Rp grid, 8.1 % VAT
    assert _price([ENERGY, GRID]) == round(17.0 * 1.081 / 100, 5)
    assert _price([ENERGY, GRID, LEVY]) == round(17.6 * 1.081 / 100, 5)
    # a municipality section decides as before, a levy-named product beside it changes nothing
    assert _price([ENERGY, GRID, FEES], "Musterwil") == round(17.5 * 1.081 / 100, 5)
    assert _price([ENERGY, GRID, FEES, LEVY], "Musterwil") == round(17.5 * 1.081 / 100, 5)
    assert _price([ENERGY, GRID, FEES], "Gone") is None
