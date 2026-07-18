# Swiss Electricity Price (ElCom)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant custom integration that works with **any Swiss electricity grid operator**, based on the legally mandated "machine-readable tariffs" data file (ElCom/StromVV requirement).

## Background

Every Swiss electricity distribution grid operator (Verteilnetzbetreiber) is legally required (Art. 7b para. 1 StromVV) to publish a machine-readable JSON file listing all their current tariffs each year, and to report the download link to the Swiss Federal Electricity Commission (ElCom) annually. ElCom publishes this registry as open government data.

This integration queries ElCom's official registry directly to find each operator's reported tariff file link. In practice, not every one of the ~600 registered operators reports a link that resolves cleanly: some report a stable direct link to the JSON file, others only a general company web page (from which the actual file, if published there at all, has to be auto-discovered). This is a data-quality gap in ElCom's own registry, not a limitation of this integration — so during setup, every operator in the picker is labeled with a **direct link** or **auto-detect** hint, so you know what to expect before choosing.

## What it provides

All entities are created dynamically from the selected operator's own live JSON — no product, municipality, or operator names are hardcoded. Product names (e.g. "Basis Energie") and municipality names are the operator's own official names and are never translated.

| Entity | Description |
|---|---|
| `sensor.elcom_<operator>_<product>` | All energy and grid tariff products the operator publishes. State = currently active Rp/kWh rate (automatic high/low tariff switching by weekday + time, where applicable). Attribute `schedule` lists all time windows. |
| `sensor.elcom_<operator>_<product>_high` / `_low` | Fixed high-tariff / low-tariff reference price for each product, independent of the current time. Only created for products with more than one price level. |
| `sensor.elcom_<operator>_<type>` | Metering fees (CHF/month, flat rate), if published. |
| `sensor.elcom_<operator>_municipality_levy_<municipality>` | Municipal surcharge (Rp/kWh) for each municipality the operator serves. |
| `sensor.elcom_<operator>_tariff_level` | Whether **your configured energy tariff** is currently on high or low tariff. |
| `sensor.elcom_<operator>_price_current` | Combined current price for your configured municipality + energy/grid tariff products, including national levies and VAT. Attribute `calculation` spells out the full calculation in plain text. |
| `sensor.elcom_<operator>_price_high` / `_low` | Fixed combined high-tariff / low-tariff price (same formula, at the fixed rate instead of the currently active one). |
| `sensor.elcom_<operator>_diagnostics_host` / `_path` / `_filename` | Where the operator's tariff file is currently being fetched from — split into host, directory, and file name so each fits the diagnostics card. The full URL and the ElCom-registered URL are available as attributes on the host sensor. |
| `sensor.elcom_<operator>_diagnostics_year` | The tariff data's own validity year (from the file's `startDate`, not any date that happens to appear in the file name — operators often publish next year's tariffs months in advance). |
| `sensor.elcom_<operator>_diagnostics_last_checked` | When the tariff file was last successfully fetched and parsed. |
| `number.elcom_<operator>_national_levies` | National electricity levies (Rp/kWh), directly adjustable. Default 3.03. |
| `number.elcom_<operator>_vat_percent` | VAT rate (%), directly adjustable. Default 8.1. |

Data updates every 24 hours (tariffs only change once a year). Multiple instances are supported — add one per operator/municipality/tariff combination you want to track.

Operators re-report to ElCom annually, and some leave an old file in place rather than replacing it in place for the new year — so on every update, the operator's current ElCom registration is re-checked (and the stored link corrected automatically if it changed), and the actual tariff-file link is freshly re-discovered rather than reused from a cached value, so a changed filename behind an unchanged page is picked up too. A user-supplied manual URL override is never touched by any of this, since it was chosen specifically to bypass a broken registration.

## Language

Entity names, device info, tariff-level labels, and the calculation text adapt automatically to your Home Assistant language setting — German, English, French, and Italian are supported, with English as the fallback for any other language.

## Installation

### HACS (recommended)

1. In HACS, go to **Integrations → ⋮ → Custom repositories**, add this repository URL with category **Integration**.
2. Search for **"Swiss Electricity Price"** and install.
3. Restart Home Assistant.

### Manual

1. Copy the `custom_components/elcom_tariffs` folder into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **"Swiss Electricity Price (ElCom)"**.
3. Search for and choose your **grid operator**. Operators marked "direct link" are far more likely to work; operators marked "auto-detect" may fail. To refine the search, just change the search text and submit again.
4. If your operator's link still doesn't work, select it anyway and paste the actual tariff file URL into **Manual tariff file URL** — find it on the operator's own website (it's a JSON file, usually linked from a "tariffs" or "Strompreise" page). This is the recommended fix when ElCom's own registry entry for your operator is stale or broken, which happens for a meaningful share of Swiss operators.
5. Choose your **municipality**, then your **energy tariff product** and **grid tariff product**, populated live from the selected operator's own tariff file — picked separately since operators don't always name or categorize their energy and grid products the same way, so pairing them automatically isn't reliable.
6. Done — all tariff, municipality, diagnostic, and metering-fee sensors appear immediately. National levies and VAT can be adjusted afterwards via the integration's **Configure** dialog.

### If no link works at all

If neither the registered link nor a manual URL can be resolved, you're automatically offered a fallback screen instead of a dead end: enter your high-tariff and (optional) low-tariff rate by hand, the daily time window the high tariff applies in, which weekdays it applies on, and an optional monthly base/grid fee — found on your electricity bill or the operator's rate sheet. You still get a current-price sensor, high/low reference sensors, and adjustable national levies/VAT, just without the municipality- and product-level breakdown that only the operator's own file provides.

A manual entry is also re-checked once a day in the background: if the operator's own tariff file starts working, a repair notification (Settings → System → Repairs) tells you so. Municipality and tariff products still need to be picked by hand, so switching over means removing and re-adding the integration entry rather than an automatic in-place upgrade — but the operator's link should then resolve on the first try.

## Notes

- Can be added multiple times, once per operator/municipality/energy-product/grid-product combination; setting one up a second time is rejected instead of silently creating a duplicate.
- If an operator's tariff file is unreachable, or its format changes, its entities become `unavailable` rather than reporting a stale or incorrect value.
- This integration is unofficial and not affiliated with ElCom or any Swiss grid operator. It only reads publicly published Open Data.
- Coverage depends entirely on the quality of each operator's own registration with ElCom — this integration cannot fix a broken or missing link reported by the operator itself.

## Disclaimer

This integration is provided **as-is, without any warranty**. Prices are computed from third-party published data and may be inaccurate, delayed, or unavailable. Do not rely on it as your sole source for financial or contractual decisions — always verify against your actual electricity bill. The author(s) accept **no responsibility or liability** for any damage, financial loss, incorrect readings, or other issues arising from using this integration, whether it stops working, behaves unexpectedly, or never worked correctly for your setup in the first place.

## License

MIT — see [LICENSE](LICENSE).

## Support

If this integration is useful to you, you can support its development:

<a href="https://www.buymeacoffee.com/prusuino"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="41"></a>
