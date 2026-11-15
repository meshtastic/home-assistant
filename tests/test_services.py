# SPDX-FileCopyrightText: 2026 ozcet
#
# SPDX-License-Identifier: MIT

"""Tests for routing service calls to gateways with incomplete metadata."""

from collections import defaultdict
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant, ServiceCall

from custom_components.meshtastic import services
from custom_components.meshtastic.const import DOMAIN, SERVICE_SEND_TEXT


@pytest.mark.parametrize("user", [None, {}, {"longName": "Gateway"}])
async def test_missing_gateway_user_id_allows_another_gateway_to_handle_call(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, user: dict[str, str] | None
) -> None:
    """Skip a gateway without an ID and route an ID-addressed call to a matching gateway."""
    partial_node: dict[str, Any] = {"num": 1}
    if user is not None:
        partial_node["user"] = user
    partial_client = AsyncMock()
    partial_client.async_get_own_node.return_value = partial_node
    matching_client = AsyncMock()
    matching_client.async_get_own_node.return_value = {"num": 2, "user": {"id": "!00000002"}}
    partial_send = AsyncMock(return_value=None)
    matching_send = AsyncMock(return_value=None)
    handlers: dict[str, dict[str, Any]] = defaultdict(dict)
    handlers["partial"][SERVICE_SEND_TEXT] = await services._build_default_handler(hass, partial_client, partial_send)
    handlers["matching"][SERVICE_SEND_TEXT] = await services._build_default_handler(
        hass, matching_client, matching_send
    )
    monkeypatch.setattr(services, "_service_handlers", handlers)
    await services.async_setup_services(hass)

    await hass.services.async_call(DOMAIN, SERVICE_SEND_TEXT, {"from": "!00000002", "text": "hello"}, blocking=True)

    partial_send.assert_not_awaited()
    matching_send.assert_awaited_once()


@pytest.mark.parametrize(
    ("user", "from_id", "matches"),
    [
        (None, "1", True),
        ({}, "1", True),
        ({"id": "!00000001"}, "!00000001", True),
        ({"id": "!00000001"}, "!00000002", False),
        (None, "!00000001", False),
        ({}, "!00000001", False),
    ],
)
async def test_gateway_selection_with_partial_and_complete_metadata(
    hass: HomeAssistant, user: dict[str, str] | None, from_id: str, *, matches: bool
) -> None:
    """Preserve numeric routing and match a textual ID only when it is present."""
    gateway: dict[str, Any] = {"num": 1}
    if user is not None:
        gateway["user"] = user
    client = AsyncMock()
    client.async_get_own_node.return_value = gateway
    send = AsyncMock(return_value=None)
    handler = await services._build_default_handler(hass, client, send)
    call = ServiceCall(hass, DOMAIN, SERVICE_SEND_TEXT, {"from": from_id})

    result = await handler(call)

    if matches:
        assert result is None
        send.assert_awaited_once_with(call, services.MeshInterface.BROADCAST_ADDR, None)
    else:
        assert result is services._SERVICE_CANT_HANDLE_RESPONSE
        send.assert_not_awaited()
