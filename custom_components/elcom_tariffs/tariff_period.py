"""Picks the tariff records that apply to a given day out of an operator's
tariff file.

Operators are required to re-report every year, and many simply append next
year's tariffs to the same file instead of replacing last year's: BKW's
file, for example, carries the complete 2026 and 2027 tariff sets side by
side (2027 first), under identical product names. Parsing such a file
without looking at the validity dates lets whichever year happens to come
last win for one product, and whichever comes first win for another — and
the validity-year diagnostic then reports a year nobody is being billed
for yet.

Pure functions, no Home Assistant imports, so the selection logic can be
exercised against real operator files outside of Home Assistant.
"""
from __future__ import annotations

from datetime import date


def parse_iso_date(value: object) -> date | None:
    """`YYYY-MM-DD` (optionally followed by a time) -> date, None if the
    value is missing or not in that shape. Lenient on purpose: a record
    with an unparseable date is kept rather than dropped, see
    select_current_tariffs()."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def select_current_tariffs(tariffs: list[dict], today: date) -> tuple[list[dict], str]:
    """The records of `tariffs` that apply on `today`, plus a short reason
    for the log: "current", "upcoming", "expired" or "undated".

    1. Records whose startDate..endDate covers today win (endDate missing
       means open-ended). Records without a parseable startDate are always
       kept — they cannot be told apart by year, and dropping them would
       lose data from operators that leave the dates out altogether.
    2. If no record is valid today (the file only holds next year's tariffs,
       published early, or only last year's, not yet replaced), the nearest
       upcoming set is used — the records sharing the earliest future
       startDate — and failing that the most recent expired set. Showing the
       nearest tariffs beats showing nothing; the validity-year diagnostic
       reveals which year is being shown.
    """
    undated: list[dict] = []
    dated: list[tuple[date, date | None, dict]] = []
    for record in tariffs:
        start = parse_iso_date(record.get("startDate"))
        if start is None:
            undated.append(record)
            continue
        dated.append((start, parse_iso_date(record.get("endDate")), record))

    if not dated:
        return list(tariffs), "undated"

    current = [r for start, end, r in dated if start <= today and (end is None or today <= end)]
    if current:
        return current + undated, "current"

    future_starts = [start for start, _end, _r in dated if start > today]
    if future_starts:
        chosen = min(future_starts)
        return [r for start, _end, r in dated if start == chosen] + undated, "upcoming"

    chosen = max(start for start, _end, _r in dated)
    return [r for start, _end, r in dated if start == chosen] + undated, "expired"


_SECONDARY_METER_MARKERS = (
    "zweitzähler",
    "zweitzaehler",
    "compteurs secondaires",
    "compteur secondaire",
    "contatori secondari",
    "contatore secondario",
    "secondary meter",
)


def is_secondary_meter_fee(record: dict) -> bool:
    """True for a regional-fee record that applies to secondary meters
    only. BKW lists a reduced "Konzessionsabgabe für Zweitzähler" next to
    the regular concession fee of the same municipality; both carry the
    same municipality name, and the secondary-meter one comes later in the
    file, so it would silently replace the regular levy. The marker is
    looked for in the record's name and comment, in the four national
    languages and English."""
    text = f"{record.get('tariffName', '')} {record.get('comment', '')}".lower()
    return any(marker in text for marker in _SECONDARY_METER_MARKERS)


def tariff_validity_end(tariffs: list[dict]) -> date | None:
    """The latest endDate among the given (already selected) records, or
    None when none carries one — used to know when the selection has to be
    redone."""
    ends = [parse_iso_date(r.get("endDate")) for r in tariffs]
    ends = [e for e in ends if e is not None]
    return max(ends) if ends else None
