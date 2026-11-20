"""The ElCom operator directory and the search for an operator's tariff
file: the directory query, the live check of links that look like a file,
the lookup of one operator's link and the resolution of a registered
address to the JSON file. A fake session answers every request."""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.elcom_tariffs.const import LINDAS_SPARQL_URL
from custom_components.elcom_tariffs.directory import (
    async_fetch_operator_directory,
    async_fetch_operator_url,
    async_resolve_tariff_url,
    async_verify_direct_links,
)

OPERATOR = "https://energy.ld.admin.ch/elcom/electricityprice/operator/"
HOST = "https://example.invalid"


class HttpError(Exception):
    """What raise_for_status() raises for an error status."""


class FakeResponse:
    """One HTTP answer: a body (text, bytes or JSON data), a status and the final URL."""

    def __init__(self, body: bytes | str | dict = b"", *, status: int = 200, url: str | None = None) -> None:
        if isinstance(body, dict):
            body = json.dumps(body)
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.url = url

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc_info) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise HttpError(f"HTTP {self.status}")

    async def read(self) -> bytes:
        return self.body

    async def json(self, content_type: str | None = None) -> dict:
        return json.loads(self.body)

    @property
    def content(self) -> SimpleNamespace:
        async def read(count: int) -> bytes:
            return self.body[:count]

        return SimpleNamespace(read=read)


class FakeSession:
    """Answers GET requests from `pages` (URL -> response or exception, 404
    for anything else) and every POST with `sparql`; keeps what was asked."""

    def __init__(self, pages: dict[str, FakeResponse | Exception] | None = None, sparql: dict | None = None) -> None:
        self.pages = pages or {}
        self.sparql = sparql or {"results": {"bindings": []}}
        self.requested: list[str] = []
        self.headers: list[dict] = []
        self.queries: list[tuple[str, str]] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requested.append(url)
        self.headers.append(kwargs.get("headers") or {})
        page = self.pages.get(url, FakeResponse(status=404))
        if isinstance(page, Exception):
            raise page
        if page.url is None:
            page.url = url
        return page

    def post(self, url: str, data: str = "", **kwargs) -> FakeResponse:
        self.queries.append((url, data))
        return FakeResponse(self.sparql)


@contextmanager
def fake_session(session: FakeSession, module: str = "directory"):
    with patch(f"custom_components.elcom_tariffs.{module}.async_get_clientsession", return_value=session):
        yield session


def _row(number: int, name: str, url: str) -> dict:
    return {
        "operator": {"value": f"{OPERATOR}{number}"},
        "identifier": {"value": f"CHE-000.000.{number:03d}"},
        "name": {"value": name},
        "url": {"value": url},
    }


def _bindings(*rows: dict) -> dict:
    return {"results": {"bindings": list(rows)}}


async def test_the_directory_lists_the_operators_by_name(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-11-18 10:00:00+01:00")
    session = FakeSession(sparql=_bindings(
        _row(1, "Musterwerk AG", f"{HOST}/tarife.json"),
        _row(2, "anderes Werk", f"{HOST}/tarife/"),
        _row(3, "Beispiel Energie", "example.invalid/Preise.JSON/"),
    ))
    with fake_session(session):
        operators = await async_fetch_operator_directory(hass, verify=False)
    assert [operator["name"] for operator in operators] == ["anderes Werk", "Beispiel Energie", "Musterwerk AG"]
    assert operators[2] == {
        "id": f"{OPERATOR}1", "uid": "CHE-000.000.001", "name": "Musterwerk AG", "url": f"{HOST}/tarife.json", "likely_direct": True,
    }
    # a link is taken for a file by its ending alone, as long as nobody checks
    assert [operator["likely_direct"] for operator in operators] == [False, True, True]
    assert session.requested == []
    url, query = session.queries[0]
    assert url == LINDAS_SPARQL_URL
    assert ':period "2026"^^xsd:gYear' in query

    with fake_session(session):
        await async_fetch_operator_directory(hass, year=2027, verify=False)
    assert ':period "2027"^^xsd:gYear' in session.queries[1][1]


async def test_an_empty_directory_answer_is_an_empty_list(hass: HomeAssistant) -> None:
    with fake_session(FakeSession(sparql={})):
        assert await async_fetch_operator_directory(hass) == []


async def test_links_that_look_like_a_file_are_checked_on_the_operators_host(hass: HomeAssistant) -> None:
    session = FakeSession(
        pages={
            f"{HOST}/a/tarife.json": FakeResponse(b'\xef\xbb\xbf \n{"tariffs": []}'),
            f"{HOST}/b/tarife.json": FakeResponse("<!doctype html><title>Page not found</title>"),
            f"{HOST}/d/tarife.json": TimeoutError("no answer"),
            f"{HOST}/e/tarife.json": FakeResponse("[]"),
        },
        sparql=_bindings(
            _row(1, "A serves the file", "example.invalid/a/tarife.json"),
            _row(2, "B answers with an error page", f"{HOST}/b/tarife.json"),
            _row(3, "C has removed the file", f"{HOST}/c/tarife.json"),
            _row(4, "D does not answer", f"{HOST}/d/tarife.json"),
            _row(5, "E serves a list", f"{HOST}/e/tarife.json"),
            _row(6, "F registered its website", f"{HOST}/f/tarife"),
        ),
    )
    with fake_session(session):
        operators = await async_fetch_operator_directory(hass)
    assert {operator["name"][0]: operator["likely_direct"] for operator in operators} == {
        "A": True, "B": False, "C": False, "D": False, "E": True, "F": False,
    }
    # a link without a scheme is asked for over https; the website is not fetched at all
    assert sorted(session.requested) == [f"{HOST}/{letter}/tarife.json" for letter in "abcde"]
    assert all(headers["User-Agent"].startswith("SwissElectricityPrice/") for headers in session.headers)
    assert all("aiohttp" not in headers["User-Agent"] for headers in session.headers)


async def test_only_the_operators_handed_in_are_checked(hass: HomeAssistant) -> None:
    shown = [
        {"name": "Musterwerk AG", "url": f"{HOST}/tarife.json", "likely_direct": True},
        {"name": "Anderes Werk", "url": f"{HOST}/anderes", "likely_direct": False},
    ]
    session = FakeSession()
    with fake_session(session):
        await async_verify_direct_links(hass, shown)
    assert session.requested == [f"{HOST}/tarife.json"]
    assert shown[0]["likely_direct"] is False  # the file is not there (HTTP 404)


async def test_the_link_of_one_operator_is_looked_up_by_year(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-11-18 10:00:00+01:00")
    session = FakeSession(sparql=_bindings({"url": {"value": f"{HOST}/tarife.json"}}))
    with fake_session(session):
        assert await async_fetch_operator_url(hass, f"{OPERATOR}123") == f"{HOST}/tarife.json"
        assert await async_fetch_operator_url(hass, f"{OPERATOR}123", 2027) == f"{HOST}/tarife.json"
    assert f":operator <{OPERATOR}123>" in session.queries[0][1]
    assert ':period "2026"^^xsd:gYear' in session.queries[0][1]
    assert ':period "2027"^^xsd:gYear' in session.queries[1][1]

    with fake_session(FakeSession()):
        assert await async_fetch_operator_url(hass, f"{OPERATOR}123", 2027) is None  # not reported yet


async def test_a_link_to_the_file_is_used_as_it_is(hass: HomeAssistant) -> None:
    session = FakeSession()
    with fake_session(session):
        assert await async_resolve_tariff_url(hass, "example.invalid/files/tarife.json") == f"{HOST}/files/tarife.json"
        assert await async_resolve_tariff_url(hass, f" {HOST}/files/Tarife.JSON ") == f"{HOST}/files/Tarife.JSON"
    assert session.requested == []


async def test_an_address_that_redirects_to_the_file_resolves_to_where_it_ends(hass: HomeAssistant) -> None:
    session = FakeSession({f"{HOST}/de/tarife-json": FakeResponse('{"tariffs": []}', url=f"{HOST}/media/tarife_2026.json")})
    with fake_session(session):
        assert await async_resolve_tariff_url(hass, f"{HOST}/de/tarife-json") == f"{HOST}/media/tarife_2026.json"
    assert session.headers[0]["User-Agent"].startswith("SwissElectricityPrice/")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        # a link from the root of the host
        ('<a href="/media/tarife_2026.json">Tarife</a>', f"{HOST}/media/tarife_2026.json"),
        # a link next to the page
        ("<a href='daten/strompreise.json?v=3'>Preise</a>", f"{HOST}/kunden/daten/strompreise.json?v=3"),
        # a full address on another host
        ('<a href="https://cdn.example.invalid/elcom.json">ElCom</a>', "https://cdn.example.invalid/elcom.json"),
        # site furniture is passed over, and the link that speaks of tariffs wins over an earlier one
        (
            '<link rel="manifest" href="/site.webmanifest.json"><a href="/wp-json/oembed.json">x</a>'
            '<a href="/media/team.json">Team</a><a href="/media/TARIF-2026.json">Tarife</a><a href="/media/team.json">Team</a>',
            f"{HOST}/media/TARIF-2026.json",
        ),
        # nothing speaks of tariffs: the first file that is not site furniture
        ('<a href="/favicon.json">x</a><a href="/media/daten.json">Daten</a><a href="/media/mehr.json">Mehr</a>', f"{HOST}/media/daten.json"),
    ],
)
async def test_the_file_is_found_on_the_operators_page(hass: HomeAssistant, html: str, expected: str) -> None:
    session = FakeSession({f"{HOST}/kunden/tarife": FakeResponse(f"<html><body>{html}</body></html>")})
    with fake_session(session):
        assert await async_resolve_tariff_url(hass, f"{HOST}/kunden/tarife") == expected


@pytest.mark.parametrize(
    "html",
    ["<p>Our tariffs are available as a PDF.</p>", '<link rel="manifest" href="/manifest.json"><a href="/schema.json">x</a>'],
)
async def test_a_page_without_a_file_is_an_error(hass: HomeAssistant, html: str) -> None:
    session = FakeSession({f"{HOST}/tarife": FakeResponse(html)})
    with fake_session(session), pytest.raises(ValueError, match="No machine-readable tariff file"):
        await async_resolve_tariff_url(hass, f"{HOST}/tarife")


async def test_a_page_that_cannot_be_fetched_is_an_error(hass: HomeAssistant) -> None:
    with fake_session(FakeSession()), pytest.raises(HttpError, match="HTTP 404"):
        await async_resolve_tariff_url(hass, f"{HOST}/tarife")
