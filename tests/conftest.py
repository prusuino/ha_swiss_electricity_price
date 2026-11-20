"""Shared fixtures for the Swiss Electricity Price tests.

Tests that need a Home Assistant instance request `hass` and
`enable_custom_integrations`; the ElCom directory and tariff-file reads
are patched per test, or all at once with `operator_host`."""
from __future__ import annotations

import pytest

from .common import WEDNESDAY_MORNING, OperatorHost


@pytest.fixture
def operator_host(hass, enable_custom_integrations):
    """The operator directory and the operator's tariff file, faked for the config flow and the coordinator."""
    with OperatorHost().patched() as host:
        yield host


@pytest.fixture
async def wednesday_morning(hass, freezer):
    """Swiss local time, stopped on a weekday at 10:00 (high tariff)."""
    await hass.config.async_set_time_zone("Europe/Zurich")
    freezer.move_to(WEDNESDAY_MORNING)
    return freezer
