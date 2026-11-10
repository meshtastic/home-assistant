# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Config flow: manual TCP setup, duplicate gateway, connection errors and reconfigure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.meshtastic.api import MeshtasticApiClientCommunicationError
from custom_components.meshtastic.config_flow import CannotConnectError, validate_input_for_connection
from custom_components.meshtastic.const import (
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_NODE,
    CONF_OPTION_NOTIFY_PLATFORM,
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_WEB_CLIENT,
    DOMAIN,
)

from .conftest import GATEWAY_KEY, GATEWAY_NUM, HOST, PEER_NUM, canonical, make_entry, make_node

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

NEW_HOST = "192.0.2.99"


def _setup_entry_patch() -> Any:
    return patch("custom_components.meshtastic.async_setup_entry", return_value=True)


async def _start_manual_tcp(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual_tcp"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_tcp"
    return result["flow_id"]


async def test_manual_tcp_flow_creates_entry_with_nodes_and_options(
    hass: HomeAssistant, validate_input: AsyncMock
) -> None:
    flow_id = await _start_manual_tcp(hass)
    connection = {CONF_CONNECTION_TCP_HOST: HOST, CONF_CONNECTION_TCP_PORT: 4403}

    result = await hass.config_entries.flow.async_configure(flow_id, connection)
    assert result["step_id"] == "node"
    validate_input.assert_awaited_once()
    assert validate_input.await_args.args[1] == {CONF_CONNECTION_TYPE: "tcp", **connection}

    # the first pick asks for another, so the node step comes back without the picked node on offer
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_OPTION_NODE: str(PEER_NUM), "add_another_node": True}
    )
    assert result["step_id"] == "node"
    assert str(PEER_NUM) not in str(result["data_schema"].schema[CONF_OPTION_NODE].config["options"])

    result = await hass.config_entries.flow.async_configure(flow_id, {CONF_OPTION_NODE: str(GATEWAY_NUM)})
    assert result["step_id"] == "notify_platform"
    result = await hass.config_entries.flow.async_configure(flow_id, {"channels": False, "nodes": "selected"})
    assert result["step_id"] == "web_client"
    result = await hass.config_entries.flow.async_configure(flow_id, {"enable": False})
    assert result["step_id"] == "tcp_proxy"
    result = await hass.config_entries.flow.async_configure(flow_id, {"enable": False, "port": 4403})
    assert result["step_id"] == "chat"

    with _setup_entry_patch() as setup_entry:
        result = await hass.config_entries.flow.async_configure(flow_id, {"enable": False})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Gateway"
    assert result["data"] == {CONF_CONNECTION_TYPE: "tcp", **connection}
    # chat is off unless asked for; the other chat options keep their defaults
    assert result["options"].pop("chat")["enable"] is False
    assert result["options"] == {
        CONF_OPTION_FILTER_NODES: [{"id": PEER_NUM, "name": "Peer"}, {"id": GATEWAY_NUM, "name": "Gateway"}],
        CONF_OPTION_NOTIFY_PLATFORM: {"channels": False, "nodes": "selected"},
        CONF_OPTION_WEB_CLIENT: {"enable": False},
        CONF_OPTION_TCP_PROXY: {"enable": False, "port": 4403},
    }
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    assert entry.unique_id == str(GATEWAY_NUM)
    setup_entry.assert_awaited_once()


@pytest.mark.usefixtures("validate_input")
async def test_duplicate_gateway_aborts_and_updates_the_connection(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    flow_id = await _start_manual_tcp(hass)

    with _setup_entry_patch():
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_CONNECTION_TCP_HOST: NEW_HOST, CONF_CONNECTION_TCP_PORT: 4403}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_CONNECTION_TCP_HOST] == NEW_HOST
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_cannot_connect_shows_the_form_again(hass: HomeAssistant, validate_input: AsyncMock) -> None:
    validate_input.side_effect = CannotConnectError
    flow_id = await _start_manual_tcp(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CONNECTION_TCP_HOST: HOST, CONF_CONNECTION_TCP_PORT: 4403}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual_tcp"
    assert result["errors"] == {"base": "cannot_connect"}

    validate_input.side_effect = None
    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CONNECTION_TCP_HOST: HOST, CONF_CONNECTION_TCP_PORT: 4403}
    )
    assert result["step_id"] == "node"


async def test_unexpected_error_is_reported_as_unknown(hass: HomeAssistant, validate_input: AsyncMock) -> None:
    validate_input.side_effect = RuntimeError
    flow_id = await _start_manual_tcp(hass)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_CONNECTION_TCP_HOST: HOST, CONF_CONNECTION_TCP_PORT: 4403}
    )

    assert result["errors"] == {"base": "unknown"}


async def test_validate_input_maps_client_errors_to_cannot_connect(hass: HomeAssistant) -> None:
    client = MagicMock()
    client.__aenter__.side_effect = MeshtasticApiClientCommunicationError
    with (
        patch("custom_components.meshtastic.config_flow.MeshtasticApiClient", return_value=client),
        pytest.raises(CannotConnectError),
    ):
        await validate_input_for_connection(
            hass, {CONF_CONNECTION_TYPE: "tcp", CONF_CONNECTION_TCP_HOST: HOST, CONF_CONNECTION_TCP_PORT: 4403}
        )


async def _reconfigure(hass: HomeAssistant, entry: MockConfigEntry, host: str) -> Any:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "reconfigure_tcp"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_tcp"
    with _setup_entry_patch():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONNECTION_TCP_HOST: host, CONF_CONNECTION_TCP_PORT: 4403}
        )
        await hass.async_block_till_done()
    return result


async def test_reconfigure_updates_the_connection_of_the_same_gateway(
    hass: HomeAssistant, validate_input: AsyncMock, entry: MockConfigEntry
) -> None:
    result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CONNECTION_TCP_HOST] == NEW_HOST
    assert validate_input.await_args.kwargs == {"no_nodes": True}


async def test_reconfigure_keeps_a_gateway_that_was_renumbered_with_the_same_key(
    hass: HomeAssistant, validate_input: AsyncMock
) -> None:
    entry = make_entry({CONF_OPTION_FILTER_NODES: [{"id": GATEWAY_NUM, "name": "Gateway", "public_key": GATEWAY_KEY}]})
    entry.add_to_hass(hass)
    renumbered = make_node(canonical(GATEWAY_KEY), "Gateway", GATEWAY_KEY)
    validate_input.return_value = (renumbered, {renumbered["num"]: renumbered})

    result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CONNECTION_TCP_HOST] == NEW_HOST


async def test_reconfigure_to_another_device_aborts_without_changes(
    hass: HomeAssistant, validate_input: AsyncMock, entry: MockConfigEntry
) -> None:
    other = make_node(0x33333333, "Other", "b3RoZXI=")
    validate_input.return_value = (other, {other["num"]: other})

    result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "different_device"
    assert entry.data[CONF_CONNECTION_TCP_HOST] == HOST


async def test_reconfigure_cannot_connect_shows_the_form_again(
    hass: HomeAssistant, validate_input: AsyncMock, entry: MockConfigEntry
) -> None:
    validate_input.side_effect = CannotConnectError

    result = await _reconfigure(hass, entry, NEW_HOST)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_CONNECTION_TCP_HOST] == HOST
