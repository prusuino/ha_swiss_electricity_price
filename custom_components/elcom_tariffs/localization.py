"""Runtime string localization (entity names, device info, tariff-level labels).

Home Assistant's built-in translation system (strings.json / translations/*.json)
only covers config/options flow text. Entity names, device info, and the
tariff-level / calculation text are set directly by this integration's Python
code and are not covered by that mechanism, so we do our own minimal lookup
here, keyed by hass.config.language. Falls back to English for any language we
don't have strings for.

Raw tariff/product names and municipality names come directly from the
selected operator's own tariff data and are never translated — they are the
operator's official product/place names, not descriptive text.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant

SUPPORTED_LANGUAGES = ("de", "en", "fr", "it")

STRINGS: dict[str, dict[str, str]] = {
    "device_name": {
        "de": "{operator} Strompreise ({municipality}, {energy_product})",
        "en": "{operator} Electricity Prices ({municipality}, {energy_product})",
        "fr": "Prix de l'électricité {operator} ({municipality}, {energy_product})",
        "it": "Prezzi elettricità {operator} ({municipality}, {energy_product})",
    },
    "device_name_manual": {
        "de": "{operator} Strompreise (manuell)",
        "en": "{operator} Electricity Prices (manual)",
        "fr": "Prix de l'électricité {operator} (manuel)",
        "it": "Prezzi elettricità {operator} (manuale)",
    },
    "model": {
        "de": "Maschinenlesbare Tarife (ElCom / VSE-Standard)",
        "en": "Machine-Readable Tariffs (ElCom / VSE Standard)",
        "fr": "Tarifs lisibles par machine (ElCom / norme AES)",
        "it": "Tariffe leggibili da macchina (ElCom / standard AES)",
    },
    "tariff_level_high": {
        "de": "Hochtarif",
        "en": "High tariff",
        "fr": "Tarif haut",
        "it": "Tariffa alta",
    },
    "tariff_level_low": {
        "de": "Niedertarif",
        "en": "Low tariff",
        "fr": "Tarif bas",
        "it": "Tariffa bassa",
    },
    "tariff_level_medium": {
        "de": "Mitteltarif",
        "en": "Medium tariff",
        "fr": "Tarif moyen",
        "it": "Tariffa media",
    },
    "tariff_level_flat": {
        "de": "Einheitstarif",
        "en": "Flat tariff",
        "fr": "Tarif unique",
        "it": "Tariffa unica",
    },
    "tariff_status_sensor_name": {
        "de": "Tarifstufe",
        "en": "Tariff Level",
        "fr": "Niveau tarifaire",
        "it": "Livello tariffario",
    },
    "diagnostics_host_name": {
        "de": "Diagnose: Quelle (Host)",
        "en": "Diagnostics: Source (Host)",
        "fr": "Diagnostic : source (hôte)",
        "it": "Diagnostica: fonte (host)",
    },
    "diagnostics_path_name": {
        "de": "Diagnose: Pfad",
        "en": "Diagnostics: Path",
        "fr": "Diagnostic : chemin",
        "it": "Diagnostica: percorso",
    },
    "diagnostics_filename_name": {
        "de": "Diagnose: Dateiname",
        "en": "Diagnostics: File Name",
        "fr": "Diagnostic : nom du fichier",
        "it": "Diagnostica: nome del file",
    },
    "diagnostics_year_name": {
        "de": "Diagnose: Gültigkeitsjahr",
        "en": "Diagnostics: Validity Year",
        "fr": "Diagnostic : année de validité",
        "it": "Diagnostica: anno di validità",
    },
    "diagnostics_last_checked_name": {
        "de": "Diagnose: Letzte Prüfung",
        "en": "Diagnostics: Last Checked",
        "fr": "Diagnostic : dernière vérification",
        "it": "Diagnostica: ultimo controllo",
    },
    "municipality_levy_prefix": {
        "de": "Gemeindeabgabe",
        "en": "Municipality Levy",
        "fr": "Taxe communale",
        "it": "Tassa comunale",
    },
    "current_price_name": {
        "de": "Strompreis aktuell",
        "en": "Current Electricity Price",
        "fr": "Prix actuel de l'électricité",
        "it": "Prezzo attuale dell'elettricità",
    },
    "price_level_name": {
        "de": "Strompreis {level}",
        "en": "Electricity Price {level}",
        "fr": "Prix de l'électricité {level}",
        "it": "Prezzo dell'elettricità {level}",
    },
    "national_levies_name": {
        "de": "Nationale Stromabgaben",
        "en": "National Electricity Levies",
        "fr": "Redevances nationales sur l'électricité",
        "it": "Oneri nazionali sull'elettricità",
    },
    "vat_name": {
        "de": "MWST-Satz",
        "en": "VAT Rate",
        "fr": "Taux de TVA",
        "it": "Aliquota IVA",
    },
    "grid_fee_name": {
        "de": "Grundgebühr",
        "en": "Base Fee",
        "fr": "Redevance de base",
        "it": "Tassa di base",
    },
    "weekday_mo": {"de": "Montag", "en": "Monday", "fr": "Lundi", "it": "Lunedì"},
    "weekday_tu": {"de": "Dienstag", "en": "Tuesday", "fr": "Mardi", "it": "Martedì"},
    "weekday_we": {"de": "Mittwoch", "en": "Wednesday", "fr": "Mercredi", "it": "Mercoledì"},
    "weekday_th": {"de": "Donnerstag", "en": "Thursday", "fr": "Jeudi", "it": "Giovedì"},
    "weekday_fr": {"de": "Freitag", "en": "Friday", "fr": "Vendredi", "it": "Venerdì"},
    "weekday_sa": {"de": "Samstag", "en": "Saturday", "fr": "Samedi", "it": "Sabato"},
    "weekday_su": {"de": "Sonntag", "en": "Sunday", "fr": "Dimanche", "it": "Domenica"},
    "operator_direct_hint": {
        "de": " (direkter Link)",
        "en": " (direct link)",
        "fr": " (lien direct)",
        "it": " (link diretto)",
    },
    "operator_uncertain_hint": {
        "de": " (Auto-Erkennung, evtl. nicht möglich)",
        "en": " (auto-detect, may not work)",
        "fr": " (détection automatique, incertaine)",
        "it": " (rilevamento automatico, incerto)",
    },
    "calculation_formula": {
        "de": (
            "{energy_rp:.2f} Rp Energie ({energy_product}) + {grid_rp:.2f} Rp Netz ({grid_product}) "
            "+ {municipality_rp:.2f} Rp Gemeindeabgabe ({municipality}) + {national_rp:.2f} Rp nationale Abgaben "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% MWST) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "en": (
            "{energy_rp:.2f} Rp energy ({energy_product}) + {grid_rp:.2f} Rp grid ({grid_product}) "
            "+ {municipality_rp:.2f} Rp municipality levy ({municipality}) + {national_rp:.2f} Rp national levies "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% VAT) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "fr": (
            "{energy_rp:.2f} Rp énergie ({energy_product}) + {grid_rp:.2f} Rp réseau ({grid_product}) "
            "+ {municipality_rp:.2f} Rp taxe communale ({municipality}) + {national_rp:.2f} Rp redevances nationales "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% TVA) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "it": (
            "{energy_rp:.2f} Rp energia ({energy_product}) + {grid_rp:.2f} Rp rete ({grid_product}) "
            "+ {municipality_rp:.2f} Rp tassa comunale ({municipality}) + {national_rp:.2f} Rp oneri nazionali "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% IVA) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
    },
    "calculation_formula_manual": {
        "de": (
            "{tariff_rp:.2f} Rp manuell eingegebener Tarif + {national_rp:.2f} Rp nationale Abgaben "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% MWST) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "en": (
            "{tariff_rp:.2f} Rp manually entered tariff + {national_rp:.2f} Rp national levies "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% VAT) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "fr": (
            "{tariff_rp:.2f} Rp tarif saisi manuellement + {national_rp:.2f} Rp redevances nationales "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% TVA) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
        "it": (
            "{tariff_rp:.2f} Rp tariffa inserita manualmente + {national_rp:.2f} Rp oneri nazionali "
            "= {subtotal_rp:.2f} Rp, × {factor:.3f} ({vat:g}% IVA) "
            "= {final_rp:.2f} Rp/kWh = {final_chf:.4f} CHF/kWh"
        ),
    },
}

TARIFF_LEVEL_KEY_MAP: dict[str, str] = {
    "high": "tariff_level_high",
    "low": "tariff_level_low",
    "medium": "tariff_level_medium",
    "flat": "tariff_level_flat",
}


def get_language(hass: HomeAssistant) -> str:
    lang = (hass.config.language or "en").lower().split("-")[0]
    return lang if lang in SUPPORTED_LANGUAGES else "en"


def t(key: str, hass: HomeAssistant, **kwargs) -> str:
    """Look up a localized string by key, formatted with kwargs."""
    lang = get_language(hass)
    template = STRINGS.get(key, {}).get(lang) or STRINGS.get(key, {}).get("en") or key
    return template.format(**kwargs) if kwargs else template


def tariff_level_text(level: str | None, hass: HomeAssistant) -> str | None:
    """Translate a canonical tariff-level key ("high"/"low"/"medium"/"flat") for display."""
    if not level:
        return None
    key = TARIFF_LEVEL_KEY_MAP.get(level)
    return t(key, hass) if key else level
