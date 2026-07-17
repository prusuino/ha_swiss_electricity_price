"""Fetches the official ElCom directory of Swiss grid operators and their
registered machine-readable tariff file link, via the LINDAS (Linked Data
Service) SPARQL endpoint.

Every Swiss distribution grid operator is legally required (Art. 7b para. 1
StromVV) to publish its tariffs as a machine-readable JSON file and report
the download link to ElCom annually. ElCom publishes this as open government
data on LINDAS. Data quality varies a lot in practice: some operators report
a working direct link to the JSON file, others only their general company
website, and — importantly — some report a URL ending in .json that is
simply dead (moved, removed, or never valid) since ElCom does not itself
verify what operators report. A URL merely *looking* like a direct JSON
link is therefore not a reliable signal on its own, so the directory is
live-verified (HEAD request) before being labeled — see
async_fetch_operator_directory() and resolve_tariff_url() below.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import LINDAS_SPARQL_URL

_LOGGER = logging.getLogger(__name__)

_VERIFY_CONCURRENCY = 40
_VERIFY_TIMEOUT_SECONDS = 4

_DIRECTORY_QUERY = """
PREFIX schema: <http://schema.org/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX cube: <https://cube.link/>
PREFIX : <https://energy.ld.admin.ch/elcom/electricityprice/dimension/>

SELECT DISTINCT ?operator ?identifier ?name ?url
WHERE {{
 <https://energy.ld.admin.ch/elcom/electricityprice> cube:observationSet/cube:observation ?obs .
  ?obs
    :period "{year}"^^xsd:gYear;
    :operator ?operator;
    :urltr ?url .
  ?operator schema:name ?name ;
            schema:identifier ?identifier .
  FILTER(isLiteral(?identifier))
}}
ORDER BY ?name
"""


def _is_direct_json_url(url: str) -> bool:
    return url.strip().lower().rstrip("/").endswith(".json")


def _with_scheme(url: str) -> str:
    url = url.strip()
    return url if url.startswith("http") else "https://" + url


async def _verify_reachable(session, url: str) -> bool:
    """Live-check whether a URL that looks like a direct JSON link actually
    serves JSON. A URL merely ending in .json is not enough — ElCom's
    registry is self-reported and often stale (moved or removed files) —
    and an HTTP status check alone is not enough either: many Swiss
    municipal/CMS-hosted sites answer a missing path with a "soft 404"
    (HTTP 200 with an HTML error or SPA shell page) instead of a real 404,
    which a status-only check would wrongly accept. So this does a real GET
    and sniffs the start of the body for JSON rather than trusting the
    status code alone."""
    try:
        async with session.get(url, timeout=_VERIFY_TIMEOUT_SECONDS, allow_redirects=True) as resp:
            if resp.status >= 400:
                return False
            chunk = await resp.content.read(200)
    except Exception:
        return False
    text = chunk.lstrip()
    return text[:1] in (b"{", b"[")


async def async_fetch_operator_directory(
    hass: HomeAssistant, year: int | None = None, verify: bool = True
) -> list[dict]:
    """Fetch all Swiss grid operators and their registered tariff file link
    for the given year (defaults to the current year).

    Returns a list of dicts: id (ElCom operator IRI), uid (Swiss company UID),
    name, url (as registered — may or may not be a direct JSON link), and
    likely_direct (bool). When verify=True (default), any URL that looks
    like a direct JSON link is live-checked with a HEAD/GET request and
    likely_direct only stays True if that check actually succeeds — a URL
    shape alone is not a reliable signal, see module docstring. Operators
    without a direct link may still work via resolve_tariff_url()'s scrape
    fallback, or may not work at all; for those, likely_direct is always
    False without a live check (would require fetching and scanning every
    operator's page, which is far more expensive).
    """
    year = year or date.today().year
    session = async_get_clientsession(hass)
    query = _DIRECTORY_QUERY.format(year=year)
    headers = {
        "Content-Type": "application/sparql-query",
        "Accept": "application/sparql-results+json",
    }
    async with session.post(LINDAS_SPARQL_URL, data=query, headers=headers, timeout=30) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    operators: dict[str, dict] = {}
    for row in data.get("results", {}).get("bindings", []):
        op_id = row["operator"]["value"]
        name = row["name"]["value"]
        url = row["url"]["value"]
        operators[op_id] = {
            "id": op_id,
            "uid": row["identifier"]["value"],
            "name": name,
            "url": url,
            "likely_direct": _is_direct_json_url(url),
        }

    if verify:
        semaphore = asyncio.Semaphore(_VERIFY_CONCURRENCY)
        candidates = [o for o in operators.values() if o["likely_direct"]]

        async def _check(op: dict) -> None:
            async with semaphore:
                op["likely_direct"] = await _verify_reachable(session, _with_scheme(op["url"]))

        await asyncio.gather(*(_check(op) for op in candidates))

    return sorted(operators.values(), key=lambda o: o["name"].lower())


_OPERATOR_URL_QUERY = """
PREFIX schema: <http://schema.org/>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
PREFIX cube: <https://cube.link/>
PREFIX : <https://energy.ld.admin.ch/elcom/electricityprice/dimension/>

SELECT ?url
WHERE {{
 <https://energy.ld.admin.ch/elcom/electricityprice> cube:observationSet/cube:observation ?obs .
  ?obs
    :period "{year}"^^xsd:gYear;
    :operator <{operator_id}>;
    :urltr ?url .
}}
LIMIT 1
"""


async def async_fetch_operator_url(
    hass: HomeAssistant, operator_id: str, year: int | None = None
) -> str | None:
    """Look up a single operator's currently registered tariff file URL for
    the given year (defaults to the current year) — a light, targeted query
    used to periodically re-check for a newer/changed registration without
    re-fetching the entire ~600-operator directory. Returns None if the
    operator has no entry for that year (e.g. not yet reported)."""
    year = year or date.today().year
    session = async_get_clientsession(hass)
    query = _OPERATOR_URL_QUERY.format(year=year, operator_id=operator_id)
    headers = {
        "Content-Type": "application/sparql-query",
        "Accept": "application/sparql-results+json",
    }
    async with session.post(LINDAS_SPARQL_URL, data=query, headers=headers, timeout=30) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    bindings = data.get("results", {}).get("bindings", [])
    return bindings[0]["url"]["value"] if bindings else None


async def async_resolve_tariff_url(hass: HomeAssistant, registered_url: str) -> str:
    """Resolve an operator's registered URL to an actual machine-readable
    tariff JSON URL.

    If the registered URL already points directly at a .json file, it is
    used as-is. Otherwise, the page it points to is fetched and scanned for
    a link to a .json file (the same auto-discovery approach used by
    operators like eug that publish a "tariffs" page rather than a stable
    direct link). Raises ValueError if no JSON file can be found either way.
    """
    url = _with_scheme(registered_url)

    if _is_direct_json_url(url):
        return url

    session = async_get_clientsession(hass)
    async with session.get(url, timeout=25) as resp:
        resp.raise_for_status()
        html = await resp.text()

    match = re.search(r'href="([^"]+\.json)"', html)
    if not match:
        raise ValueError(
            f"No machine-readable tariff file could be found at the registered URL ({registered_url})"
        )
    found = match.group(1)
    if found.startswith("/"):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        found = f"{parts.scheme}://{parts.netloc}{found}"
    elif not found.startswith("http"):
        found = url.rsplit("/", 1)[0] + "/" + found
    return found
