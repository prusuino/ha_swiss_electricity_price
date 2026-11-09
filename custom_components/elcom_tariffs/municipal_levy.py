"""Municipal levies of a municipality that the operator's file lists more than once.

The VSE tariff format allows any number of `regional_fees` records, and
every one of them can carry an entry for the same municipality. The
machine-readable files (11/2026) do this in four flavours:

* one record per grid product, both carrying the same `customerType`
  text ("up to 100 000 kWh a year (0.4 kV)", "16 kV"): the record that
  belongs to the chosen grid product can be told automatically;
* one record per network level (`customerVoltageLevel` 7, 5, 3), which
  every grid product states as well: a customer has to do with the
  records of the own level only, and where that leaves one rate it is
  the levy;
* one record per consumption band, named so only in the record's own
  title ("< 50 MWh"): nothing links it to a grid product, the customer
  has to say which one it is;
* several levies that are all on the bill side by side (a concession
  fee, a levy for public lighting, one for energy efficiency): they add
  up, and again only the customer knows which of them the bill shows.

So every record is kept, and `resolve()` decides which of them make up
the levy of an entry: the customer's own choice first, then the only
record, then the record with the customer type of the chosen grid
product, then the record of its network level. When none of this gives
one answer, the last record in the file that the customer could choose
is used and the caller raises a repair issue asking for that choice.
"""
from __future__ import annotations

from collections import Counter

# How the levy of a municipality was arrived at (`resolved_by`).
RESOLVED_SINGLE = "single_record"
RESOLVED_CUSTOMER_TYPE = "customer_type"
RESOLVED_VOLTAGE_LEVEL = "voltage_level"
RESOLVED_SELECTION = "selection"
UNRESOLVED = "unresolved"


def normalize_customer_type(value) -> str:
    """Customer-type text for comparing: case and spacing do not count."""
    return " ".join(str(value or "").split()).casefold()


def normalize_voltage_level(value) -> str:
    """Network level of a record for comparing. The files write it as a
    number or as text (7, "7"); nothing or 0 means it is not stated."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value if value is not None else "").strip()
    return "" if text == "0" else text


def assign_keys(records: list[dict]) -> None:
    """Give each record of one municipality the key it is chosen by.

    The key is stored in the entry data, so it has to survive the next
    fetch and, where possible, the next year: the record's title, which
    is the most stable thing it has. Titles are not unique, though. An
    operator can publish one record per network level under one title,
    so the level is added where records of one title differ in it. Or
    two entries for a municipality sit in one record and are told apart
    only in the entry's comment, so the comment is added where the title
    still repeats, and a counter where even that repeats.
    """
    keys: list[str] = []
    for record in records:
        key = record["tariff_name"] or "?"
        level = normalize_voltage_level(record.get("voltage_level"))
        same = [r for r in records if r["tariff_name"] == record["tariff_name"]]
        if len({normalize_voltage_level(r.get("voltage_level")) for r in same}) > 1:
            if level:
                key = f"{key} | level {level}"
            same = [r for r in same if normalize_voltage_level(r.get("voltage_level")) == level]
        if len(same) > 1 and record.get("comment"):
            key = key + " | " + record["comment"]
        keys.append(key)
    seen: Counter[str] = Counter()
    repeated = Counter(keys)
    for record, key in zip(records, keys):
        seen[key] += 1
        record["key"] = f"{key} #{seen[key]}" if repeated[key] > 1 and seen[key] > 1 else key


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate(record: dict) -> tuple[float, float]:
    return round(_number(record.get("rp_kwh")), 6), round(_number(record.get("base_price_chf")), 6)


def has_alternatives(records: list[dict]) -> bool:
    """Whether the records differ in what they charge. Several records
    with one and the same rate are no choice at all."""
    return len({_rate(r) for r in records}) > 1


def _of_level(records: list[dict], voltage_level) -> list[dict]:
    """The records a customer on this network level has to do with: those
    of the level and those that state none. All of them where the level
    is unknown or no record carries it."""
    level = normalize_voltage_level(voltage_level)
    levels = [normalize_voltage_level(r.get("voltage_level")) for r in records]
    if not level or level not in levels:
        return records
    return [r for r, own in zip(records, levels) if own in ("", level)]


def resolve(
    records: list[dict],
    grid_customer_type: str | None = None,
    selected: list[str] | None = None,
    grid_voltage_level=None,
) -> dict:
    """The levy of one municipality from all its records (in file order).

    Returns the municipality entry as the sensors read it: `rp_kwh` and
    the other figures of the record(s) that apply, plus every record
    (`records`), the keys of those the customer chooses between
    (`choices`: the records of the grid product's network level), the
    keys of the applied ones (`applied`), how they were found
    (`resolved_by`) and the chosen keys that are no longer on offer
    (`missing`).
    """
    choices = _of_level(records, grid_voltage_level)
    missing: list[str] = []
    applied: list[dict] = []
    resolved_by = UNRESOLVED
    if selected:
        keys = {r["key"] for r in choices}
        missing = [key for key in selected if key not in keys]
        applied = [r for r in choices if r["key"] in selected]
        if applied:
            resolved_by = RESOLVED_SELECTION
    if not applied:
        wanted = normalize_customer_type(grid_customer_type)
        matching = [r for r in records if normalize_customer_type(r.get("customer_type")) == wanted] if wanted else []
        own = [r for r in choices if any(r is other for other in matching)]
        if not has_alternatives(records):
            applied, resolved_by = [records[-1]], RESOLVED_SINGLE
        elif matching and not has_alternatives(matching):
            applied, resolved_by = [matching[-1]], RESOLVED_CUSTOMER_TYPE
        elif not has_alternatives(choices):
            applied, resolved_by = [choices[-1]], RESOLVED_VOLTAGE_LEVEL
        elif own and not has_alternatives(own):
            applied, resolved_by = [own[-1]], RESOLVED_CUSTOMER_TYPE
        else:
            # No single answer: the last record on offer. Without network
            # levels in the file that is its last record, as before.
            applied, resolved_by = [choices[-1]], UNRESOLVED

    last = applied[-1]
    if len(applied) == 1:
        rp_kwh, base = last["rp_kwh"], last.get("base_price_chf")
    else:
        rp_kwh = round(sum(_number(r.get("rp_kwh")) for r in applied), 6)
        base = round(sum(_number(r.get("base_price_chf")) for r in applied), 6)
    comments: list[str] = []
    for record in applied:
        if record.get("comment") and record["comment"] not in comments:
            comments.append(record["comment"])
    return {
        "rp_kwh": rp_kwh,
        "base_price_chf": base,
        "valid_from": last.get("valid_from"),
        "valid_to": last.get("valid_to"),
        "comment": "; ".join(comments) or None,
        "records": records,
        "choices": [r["key"] for r in choices],
        "applied": [r["key"] for r in applied],
        "resolved_by": resolved_by,
        "missing": missing,
    }


def choice_records(levy: dict | None) -> list[dict]:
    """The records of a municipality entry the customer chooses between."""
    records = (levy or {}).get("records") or []
    keys = (levy or {}).get("choices")
    return records if keys is None else [r for r in records if r["key"] in keys]


def needs_choice(levy: dict | None) -> bool:
    """Whether the customer has to say which record applies: nothing told
    them apart, or a record chosen earlier is no longer on offer (even
    where the grid product then names one: a choice is not replaced
    without a word)."""
    if not levy:
        return False
    return levy.get("resolved_by") == UNRESOLVED or (
        bool(levy.get("missing")) and has_alternatives(choice_records(levy))
    )


def offers_choice(levy: dict | None) -> bool:
    """Whether the municipality has records to choose between at all."""
    return bool(levy) and has_alternatives(choice_records(levy))


def record_labels(records: list[dict]) -> dict[str, str]:
    """Each record as listed for choosing: its title, what tells it apart
    from the others (network level, comment, customer type) and its rate."""
    comments = {r.get("comment") for r in records}
    customer_types = {normalize_customer_type(r.get("customer_type")) for r in records}
    levels = {normalize_voltage_level(r.get("voltage_level")) for r in records}
    labels: dict[str, str] = {}
    for record in records:
        parts = [record["tariff_name"] or "?"]
        if len(levels) > 1 and normalize_voltage_level(record.get("voltage_level")):
            parts.append("network level " + normalize_voltage_level(record.get("voltage_level")))
        if len(comments) > 1 and record.get("comment"):
            parts.append(record["comment"])
        if len(customer_types) > 1 and record.get("customer_type"):
            parts.append(" ".join(str(record["customer_type"]).split()))
        parts.append(f"{_number(record.get('rp_kwh')):g} Rp/kWh")
        labels[record["key"]] = " · ".join(parts)
    return labels


def applied_text(levy: dict) -> str:
    """The applied record(s) of a municipality entry, as one line."""
    labels = record_labels(levy.get("records") or [])
    return " + ".join(labels[key] for key in levy.get("applied") or [] if key in labels)


def levy_attributes(levy: dict) -> dict:
    """What the municipality levy sensor tells about its record(s): the
    applied one(s), how they were found and, where the file has more than
    one, all of them."""
    records = levy.get("records") or []
    if not records:
        return {}
    applied_keys = levy.get("applied") or []
    applied = [r for r in records if r["key"] in applied_keys]
    names: list[str] = []
    customer_types: list[str] = []
    for record in applied:
        # Entries of one record share its title: named once.
        if (record["tariff_name"] or "?") not in names:
            names.append(record["tariff_name"] or "?")
        if record.get("customer_type") and record["customer_type"] not in customer_types:
            customer_types.append(record["customer_type"])
    attributes = {
        "tariff_record": " + ".join(names) or None,
        "customer_type": " + ".join(customer_types) or None,
        "resolved_by": levy.get("resolved_by"),
    }
    if len(records) > 1:
        attributes["records"] = [
            {
                "name": r["tariff_name"],
                "customer_type": r.get("customer_type"),
                "voltage_level": normalize_voltage_level(r.get("voltage_level")) or None,
                "comment": r.get("comment"),
                "rp_kwh": r["rp_kwh"],
                "applied": r["key"] in applied_keys,
            }
            for r in records
        ]
    return attributes
