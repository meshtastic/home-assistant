# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Reloading an entry or toggling the web client leaves nothing behind."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Self
from unittest.mock import AsyncMock, patch

import pytest

import custom_components.meshtastic as integration
from custom_components.meshtastic import helpers
from custom_components.meshtastic.api import EVENT_MESHTASTIC_API_NODE_UPDATED

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.usefixtures("fake_client")
async def test_a_reload_does_not_stack_node_update_listeners(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    after_setup = hass.bus.async_listeners().get(EVENT_MESHTASTIC_API_NODE_UPDATED, 0)
    assert after_setup > 0

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.bus.async_listeners().get(EVENT_MESHTASTIC_API_NODE_UPDATED, 0) == after_setup


async def test_reenabling_the_web_client_registers_its_routes_once(hass: HomeAssistant) -> None:
    with (
        patch.object(integration.meshtastic_web, "async_setup", AsyncMock(return_value=True)) as register_routes,
        patch.object(integration.frontend, "async_register_frontend", AsyncMock()) as register_panel,
        patch.object(integration.frontend, "async_unregister_frontend", AsyncMock()),
    ):
        assert await integration.async_setup_meshtastic_web(hass)
        assert await integration.async_unload_meshtastic_web(hass)
        assert await integration.async_setup_meshtastic_web(hass)

    assert register_routes.await_count == 1
    assert register_panel.await_count == 2


class _Response:
    def __init__(self, payload: list[dict[str, str]]) -> None:
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def json(self) -> list[dict[str, str]]:
        return self._payload


class _Session:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def get(self, _url: str, **kwargs: Any) -> _Response:
        self.requests.append(kwargs)
        return _Response([{"hwModelSlug": "HELTEC_V3", "displayName": "Heltec V3"}])


async def test_hardware_names_are_fetched_once_with_a_time_limit(hass: HomeAssistant) -> None:
    session = _Session()
    with patch.object(helpers, "async_get_clientsession", return_value=session):
        first = await helpers.fetch_meshtastic_hardware_names(hass)
        second = await helpers.fetch_meshtastic_hardware_names(hass)

    assert first == second == {"HELTEC_V3": "Heltec V3"}
    assert len(session.requests) == 1
    assert session.requests[0]["timeout"].total == helpers._HARDWARE_NAMES_TIMEOUT_SECONDS
