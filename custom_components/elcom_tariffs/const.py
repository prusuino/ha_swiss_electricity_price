"""Constants for the Swiss Electricity Tariffs (ElCom) integration."""
DOMAIN = "elcom_tariffs"

# LINDAS (Swiss federal Linked Data Service) SPARQL endpoint, cached variant
# recommended for production use by ElCom's own documentation.
LINDAS_SPARQL_URL = "https://cached.lindas.admin.ch/query"

UPDATE_INTERVAL_HOURS = 24

# Sent with every request to an operator's own host (tariff file, link
# verification, auto-detection page). Home Assistant's shared session
# identifies itself as "HomeAssistant/<version> aiohttp/<version>
# Python/<version>", and at least one operator's web server (Technische
# Betriebe Flawil, nginx) answers any user agent containing "aiohttp"
# with HTTP 406 Not Acceptable, so the registered tariff file could not
# be fetched although it is valid and publicly reachable (#7). The
# LINDAS directory queries keep the default user agent.
USER_AGENT = (
    "SwissElectricityPrice/1.7.0 "
    "(Home Assistant integration; +https://github.com/prusuino/ha_swiss_electricity_price)"
)
OPERATOR_REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.8"}

CONF_OPERATOR_ID = "operator_id"
CONF_OPERATOR_NAME = "operator_name"
CONF_TARIFF_URL = "tariff_url"
# True if CONF_TARIFF_URL is a user-supplied override rather than the
# operator's own ElCom-registered link — if so, it is never auto-corrected
# (the user chose it specifically because the registered one didn't work).
CONF_URL_IS_MANUAL = "url_is_manual"
CONF_MUNICIPALITY = "municipality"
# The energy and grid tariff product to use for the combined price sensors,
# picked directly from the operator's own product names (tariffName) rather
# than inferred/matched automatically — operators do not reliably pair an
# energy product with a same-named or same-customerType grid product (their
# own naming and customerType text can drift even for the same segment), so
# guessing the pairing silently produces wrong or missing combined prices.
CONF_ENERGY_PRODUCT = "energy_product"
CONF_GRID_PRODUCT = "grid_product"
CONF_NATIONAL_LEVIES_RP = "national_levies_rp"
# Setup choice: the operator's grid price already contains the federal levies (as the
# VSE tariff format prescribes) -> national levies start at 0 instead of the default.
CONF_GRID_INCLUDES_LEVIES = "grid_includes_levies"
CONF_VAT_PERCENT = "vat_percent"
# Start date (ISO) of the upcoming tariff period the user has already been
# notified about, so the notification is sent once per period.
CONF_NEXT_PERIOD_NOTIFIED = "next_period_notified"

# Manual tariff entry (fallback when neither the operator's registered link
# nor a user-supplied URL can be reached / parsed).
CONF_MANUAL_HIGH_RP = "manual_high_rp"
CONF_MANUAL_LOW_RP = "manual_low_rp"
CONF_MANUAL_HIGH_FROM = "manual_high_from"
CONF_MANUAL_HIGH_TO = "manual_high_to"
CONF_MANUAL_HIGH_WEEKDAYS = "manual_high_weekdays"
CONF_MANUAL_GRID_FEE_CHF = "manual_grid_fee_chf"

DEFAULT_MANUAL_HIGH_FROM = "06:00"
DEFAULT_MANUAL_HIGH_TO = "22:00"
DEFAULT_MANUAL_HIGH_WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr"]

# National electricity levies (grid surcharge Art. 35 EnG 2.30, system services
# 0.27, electricity reserve 0.41, solidarity surcharge 0.05 — 2026 figures), in
# Rp/kWh. The VSE tariff format defines the "grid" price as *including* these,
# and operators such as BKW publish it that way; others publish the grid price
# net of them and list the levies separately on the bill. Which one applies is
# chosen at setup (CONF_GRID_INCLUDES_LEVIES); this is the amount added when
# the grid price does NOT include them, adjustable afterwards.
DEFAULT_NATIONAL_LEVIES_RP = 3.03
DEFAULT_VAT_PERCENT = 8.1
