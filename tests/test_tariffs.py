"""Pure tariff logic: window normalisation, the rate that applies at a
moment, the next switch, the record parser and the validity selection."""
from __future__ import annotations

from datetime import date, datetime

from custom_components.elcom_tariffs.coordinator import (
    ElcomTariffCoordinator,
    _parse_hhmm,
    _price_level,
    _time_in_window,
    current_rate,
    decode_tariff_file,
    next_switch_time,
    normalize_months,
    normalize_weekdays,
    weekday_language,
)
from custom_components.elcom_tariffs.tariff_period import (
    is_secondary_meter_fee,
    parse_iso_date,
    select_current_tariffs,
    select_upcoming_tariffs,
    tariff_validity_end,
)

ENERGY = {
    "tariffType": "electricity", "tariffName": "Basic Energie", "customerType": "H4", "startDate": "2026-01-01", "endDate": "2026-12-31",
    "prices": {
        "energy": [
            {"from": "06:00", "to": "22:00", "weekdays": ["Mo", "Di", "Mi", "Do", "Fr"], "price": 0.12},
            {"from": "22:00", "to": "06:00", "price": 0.09},
        ],
        "base": {"price": 5.0, "priceUnit": "CHF/M"},
    },
}
GRID = {**ENERGY, "tariffType": "grid", "tariffName": "Basic Netz",
        "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0.08}], "base": {"price": 9.0, "priceUnit": "CHF/M"}}}
FEES = {
    "tariffType": "regional_fees", "tariffName": "Gemeindeabgaben", "startDate": "2026-01-01",
    "prices": {"municipalityTaxes": [
        {"municipalityName": "Musterwil", "municipalityEnergy": [{"price": 0.005}], "municipalityBase": {"price": 0}},
        {"municipalityName": "Beispielingen", "municipalityEnergy": [{"price": 0.01}], "municipalityBase": {"price": 2}},
    ]},
}
SECONDARY = {**FEES, "tariffName": "Konzessionsabgabe für Zweitzähler",
             "prices": {"municipalityTaxes": [{"municipalityName": "Musterwil", "municipalityEnergy": [{"price": 0.001}], "municipalityBase": {"price": 0}}]}}
DYNAMIC = {"tariffType": "electricity", "tariffName": "Dynamic", "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 0}]}}


def test_weekday_normalisation_in_the_file_language():
    windows = ENERGY["prices"]["energy"]
    assert weekday_language(windows) == "de"
    assert normalize_weekdays(["Mo", "Di", "Mi", "Do", "Fr"], "de") == ["Mo", "Tu", "We", "Th", "Fr"]
    assert normalize_weekdays(["Sa", "Su"], "en") == ["Sa", "Su"]
    assert normalize_weekdays(["all"]) is None
    assert normalize_weekdays([]) is None
    assert normalize_weekdays(["Blursday"]) is None  # unknown spelling: the window must not vanish


def test_month_normalisation():
    assert normalize_months(["Jan", "Feb", "März"]) == ["Jan", "Feb", "Mar"]
    assert normalize_months(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]) is None
    assert normalize_months(None) is None


def test_time_window_math():
    assert _parse_hhmm("06:30") == 390
    assert _parse_hhmm("24:00") == 1440
    assert _time_in_window(600, 360, 1320) is True
    assert _time_in_window(1380, 1320, 360) is True  # 23:00 inside the night window
    assert _time_in_window(600, 1320, 360) is False
    assert _time_in_window(600, 0, 0) is True  # whole day


def test_price_level():
    assert _price_level([0.12, 0.09], 0.12) == "high"
    assert _price_level([0.12, 0.09], 0.09) == "low"
    assert _price_level([0.12, 0.10, 0.09], 0.10) == "medium"
    assert _price_level([0.08], 0.08) == "flat"


def test_parsed_product_and_current_rate():
    parsed = ElcomTariffCoordinator._parse_tariff_records([ENERGY, GRID, FEES, SECONDARY, DYNAMIC])
    assert set(parsed["energy"]) == {"Basic Energie"}  # the dynamic product without prices is left out
    energy = parsed["energy"]["Basic Energie"]
    assert energy["high_rp_kwh"] == 12.0 and energy["low_rp_kwh"] == 9.0
    assert energy["base_price_chf_month"] == 5.0
    assert energy["windows"][0]["weekdays"] == ["Mo", "Tu", "We", "Th", "Fr"]
    assert energy["windows"][1]["weekdays"] is None

    monday_morning = datetime(2026, 11, 9, 10, 0)
    assert current_rate(energy, monday_morning) == (12.0, "high")
    assert current_rate(energy, datetime(2026, 11, 9, 23, 0)) == (9.0, "low")
    # Sunday 10:00 is covered by no window: the lowest rate is the least wrong answer
    assert current_rate(energy, datetime(2026, 11, 8, 10, 0)) == (9.0, "low")
    grid = parsed["grid"]["Basic Netz"]
    assert current_rate(grid, monday_morning) == (8.0, "flat")
    assert grid["high_rp_kwh"] is None

    assert next_switch_time([energy, grid], monday_morning) == datetime(2026, 11, 9, 22, 0)
    assert next_switch_time([energy], datetime(2026, 11, 9, 23, 0)) == datetime(2026, 11, 10, 0, 0)

    assert parsed["municipalities"]["Musterwil"]["rp_kwh"] == 0.5  # the regular levy, not the secondary-meter one
    assert parsed["municipalities"]["Beispielingen"]["base_price_chf"] == 2


def test_prices_entered_in_rappen_are_read_as_such():
    record = {**ENERGY, "prices": {"energy": [{"from": "00:00", "to": "00:00", "price": 15.9}], "base": {}}}
    parsed = ElcomTariffCoordinator._parse_time_tariff(record)
    assert parsed["windows"][0]["rp_kwh"] == 15.9


def test_decode_tariff_file():
    assert decode_tariff_file(b'\xef\xbb\xbf{"a": 1}') == {"a": 1}
    assert decode_tariff_file('{"name": "Zürich"}'.encode("cp1252")) == {"name": "Zürich"}


def test_validity_selection():
    today = date(2026, 11, 9)
    this_year = {"tariffName": "A", "startDate": "2026-01-01", "endDate": "2026-12-31"}
    next_year = {"tariffName": "A", "startDate": "2027-01-01", "endDate": "2027-12-31"}
    last_year = {"tariffName": "A", "startDate": "2025-01-01", "endDate": "2025-12-31"}
    undated = {"tariffName": "B"}
    assert select_current_tariffs([this_year, next_year, undated], today) == ([this_year, undated], "current")
    assert select_current_tariffs([next_year], today) == ([next_year], "upcoming")
    assert select_current_tariffs([last_year], today) == ([last_year], "expired")
    assert select_current_tariffs([undated], today) == ([undated], "undated")
    assert select_upcoming_tariffs([this_year, next_year], today) == ([next_year], date(2027, 1, 1))
    assert select_upcoming_tariffs([this_year], today) == ([], None)
    assert tariff_validity_end([this_year, undated]) == date(2026, 12, 31)
    assert tariff_validity_end([undated]) is None
    assert parse_iso_date("2026-01-01T00:00:00") == date(2026, 1, 1)
    assert parse_iso_date("01.01.2026") is None


def test_secondary_meter_marker():
    assert is_secondary_meter_fee(SECONDARY) is True
    assert is_secondary_meter_fee(FEES) is False
    assert is_secondary_meter_fee({"tariffName": "Taxe", "comment": "compteur secondaire"}) is True
