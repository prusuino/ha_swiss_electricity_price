# Changelog

## 1.3.0 — 2026-07-17

- Setup now asks for your energy tariff product and grid tariff product separately, picked directly from the operator's own product names, instead of inferring the grid product from a single "customer tariff" selection. The previous naming-convention guess (energy product name + " Netz") does not reliably hold across operators — verified against real operator data, where the two products carry the same customer category but neither the same name nor a matching category label. Existing entries are migrated automatically, reproducing the same guess they already relied on, so already-working setups keep working unchanged.
- Fixed: a config entry's national-levies and VAT number entities (and, by extension, every other entity of that instance) could get stuck showing "unavailable" after a Home Assistant restart if the operator's tariff-file host happened to be briefly unreachable at boot — the whole entry's setup was gated on that first network fetch succeeding, even for the two number entities that only ever read local config values and never needed the network at all. They're now set up independently of it.
- Fixed: adjusting the national-levies or VAT number briefly dropped every entity of that instance to "unavailable" and re-fetched the operator's tariff file over the network, just to make the combined-price sensors pick up the new rate. They now update immediately from already-fetched data instead.

## 1.2.0 — 2026-07-17

- Five new diagnostic sensors per operator instance (`sensor.elcom_<operator>_diagnostics_host` / `_path` / `_filename` / `_year` / `_last_checked`) showing which tariff-file source is currently in use: the resolved tariff-file's host, directory path, and file name (split apart so no single value is too long to fit the diagnostics card; the full URL and the ElCom-registered URL are available as attributes on the host sensor), the year found in the filename, and the timestamp of the last successful check — useful for spotting a stale or misdetected link without digging through the logs. Not shown for fully manual rate entries, which have no tariff file to report on.
- Setting up the same operator + municipality + customer-tariff combination (or the same operator twice in manual mode) a second time is now rejected instead of silently creating a duplicate entry.

## 1.1.0 — 2026-07-17

Initial public release.

- Nationwide Swiss coverage: works with any grid operator registered with ElCom, not just a single utility
- Config flow with a searchable operator picker (search by name, refine and resubmit) sourced live from ElCom's official LINDAS registry, instead of scrolling through ~600 entries
- Each operator is labeled with a direct-link or auto-detect hint so link-quality issues are visible before setup, not after — the "direct link" hint is backed by a live reachability + content check (not just the URL's shape), since ElCom's self-reported registry is often stale or points at soft-404 pages
- Manual tariff file URL override during setup, for the (common) case where ElCom's registered link for an operator doesn't work at all
- Fully manual rate entry as a further fallback, offered automatically when neither the registered link nor a manual URL resolves: high/low tariff rate, high-tariff time window and weekdays, optional monthly base/grid fee — still gets a current-price sensor and adjustable national levies/VAT
- The operator's ElCom registration is re-checked on every update (not just once at setup): operators re-report their tariff-file link annually, and a stale link that keeps answering with old data would otherwise never be noticed. The stored link is corrected automatically when it changes.
- The actual tariff-file link is freshly re-discovered on every update instead of being cached after the first resolve — so a changed filename behind an operator's page that itself didn't change is picked up too, not just an outright broken link.
- A user-supplied manual URL override is never touched by either of the above — it stays exactly as entered.
- A manual rate entry is re-checked once a day in the background for a newly working operator link, raising a repair notification (Settings → System → Repairs) when found, since municipality/customer tariff still need to be picked by hand before switching over.
- All energy + grid tariff products as `sensor.elcom_<operator>_<product>`, with automatic high/low tariff switching by weekday + time
- Fixed high-tariff / low-tariff reference sensors (`_high` / `_low`) alongside every time-based tariff sensor
- `sensor.elcom_<operator>_tariff_level` — indicator of whether the configured tariff is currently on high or low tariff
- Metering fees and all served municipalities' surcharges as individual sensors
- `sensor.elcom_<operator>_price_current` / `_high` / `_low` — combined price (energy + grid + municipal surcharge + national levies + VAT) for the configured municipality and customer tariff, with a `calculation` attribute spelling out the full calculation
- `number.elcom_<operator>_national_levies` / `number.elcom_<operator>_vat_percent` — national levies and VAT rate, directly adjustable
- Multi-language support (German, English, French, Italian) for entity names, device info, tariff-level labels, and the calculation text
- Multi-instance: one entry per operator/municipality/customer-tariff combination
- Data refreshed every 24 hours
