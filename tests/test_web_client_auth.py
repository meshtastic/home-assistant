# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The web client's radio API is reachable only under a token an admin obtained."""

from __future__ import annotations

import asyncio
import datetime as dt
from http import HTTPStatus
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2
from custom_components.meshtastic.const import (
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_WEB_CLIENT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    DOMAIN,
)
from custom_components.meshtastic.meshtastic_web import TOKEN_IDLE_TIMEOUT, WebClientTokens

from .conftest import GATEWAY_NUM, PEER_NUM, make_entry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

    from .conftest import FakeClient

NODES = [{"id": GATEWAY_NUM, "name": "Gateway"}, {"id": PEER_NUM, "name": "Peer"}]


async def setup_gateway(hass: HomeAssistant, *, web_client: bool = True) -> tuple[MockConfigEntry, str]:
    assert await async_setup_component(hass, "http", {})
    entry = make_entry(
        {CONF_OPTION_FILTER_NODES: NODES, CONF_OPTION_WEB_CLIENT: {CONF_OPTION_WEB_CLIENT_ENABLE: web_client}}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if not web_client:
        # the web client is set up on demand; set it up anyway so the command exists
        from custom_components.meshtastic import async_setup_meshtastic_web  # noqa: PLC0415

        await async_setup_meshtastic_web(hass)
    gateway = er.async_get(hass).async_get_entity_id(DOMAIN, DOMAIN, f"{entry.entry_id}_gateway_{GATEWAY_NUM}")
    return entry, gateway


async def open_web_client(
    hass_ws_client: WebSocketGenerator, hass: HomeAssistant, entity_id: str, access_token: str | None = None
) -> dict:
    client = await (hass_ws_client(hass, access_token) if access_token else hass_ws_client(hass))
    await client.send_json_auto_id({"type": "meshtastic/web_client/open", "entity_id": entity_id})
    return await client.receive_json()


@pytest.mark.usefixtures("fake_client")
async def test_an_admin_gets_a_tokened_proxy_path_that_reaches_the_api(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry, gateway = await setup_gateway(hass)

    response = await open_web_client(hass_ws_client, hass, gateway)

    assert response["success"], response
    path = parse_qs(urlparse(response["result"]["url"]).query)["path"][0]
    assert path.startswith(f"/meshtastic/web/{entry.entry_id}/")
    http = await hass_client_no_auth()
    # past the token check, the API asks for the session cookie the client sets on connect
    assert (await http.get(f"{path}/api/v1/fromradio")).status == HTTPStatus.BAD_REQUEST


@pytest.mark.usefixtures("fake_client")
async def test_the_api_refuses_a_missing_wrong_or_foreign_token(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    entry, gateway = await setup_gateway(hass)
    response = await open_web_client(hass_ws_client, hass, gateway)
    token = parse_qs(urlparse(response["result"]["url"]).query)["path"][0].rsplit("/", 1)[1]
    http = await hass_client_no_auth()

    wrong = await http.get(f"/meshtastic/web/{entry.entry_id}/not-a-token/api/v1/fromradio")
    foreign = await http.get(f"/meshtastic/web/another-entry/{token}/api/v1/fromradio")
    put = await http.put(
        f"/meshtastic/web/{entry.entry_id}/not-a-token/api/v1/toradio",
        data=b"",
        headers={"Content-Type": "application/x-protobuf"},
    )
    untokened = await http.get(f"/meshtastic/web/{entry.entry_id}/api/v1/fromradio")

    assert wrong.status == HTTPStatus.FORBIDDEN
    assert foreign.status == HTTPStatus.FORBIDDEN
    assert put.status == HTTPStatus.FORBIDDEN
    assert untokened.headers.get("Content-Type", "").startswith("text/html")


@pytest.mark.usefixtures("fake_client")
async def test_a_stale_token_is_not_counted_as_a_failed_login(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    entry, _ = await setup_gateway(hass)
    http = await hass_client_no_auth()

    with patch("homeassistant.components.http.ban.process_wrong_login") as wrong_login:
        for _ in range(3):
            await http.get(f"/meshtastic/web/{entry.entry_id}/lost-token/api/v1/fromradio")

    wrong_login.assert_not_called()


@pytest.mark.usefixtures("fake_client")
async def test_a_non_admin_gets_no_token(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
) -> None:
    _, gateway = await setup_gateway(hass)

    response = await open_web_client(hass_ws_client, hass, gateway, access_token=hass_read_only_access_token)

    assert not response["success"]
    assert response["error"]["code"] == "unauthorized"


@pytest.mark.usefixtures("fake_client")
async def test_no_token_while_the_web_client_is_off(hass: HomeAssistant, hass_ws_client: WebSocketGenerator) -> None:
    _, gateway = await setup_gateway(hass, web_client=False)

    response = await open_web_client(hass_ws_client, hass, gateway)

    assert response["error"]["code"] == "not_enabled"


@pytest.mark.usefixtures("fake_client")
async def test_an_old_gateway_link_leads_to_the_panel(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    _, gateway = await setup_gateway(hass)
    http = await hass_client_no_auth()

    response = await http.get(f"/meshtastic/web/{gateway.split('.', 1)[1]}", allow_redirects=False)

    assert response.status == HTTPStatus.TEMPORARY_REDIRECT
    assert response.headers["Location"] == "/meshtastic"


@pytest.mark.parametrize("change", ["demoted", "deactivated", "removed"])
@pytest.mark.usefixtures("fake_client")
async def test_a_token_ends_with_its_admin(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    hass_admin_user: MockUser,
    change: str,
) -> None:
    _, gateway = await setup_gateway(hass)
    websocket = await hass_ws_client(hass)
    await websocket.send_json_auto_id({"type": "meshtastic/web_client/open", "entity_id": gateway})
    response = await websocket.receive_json()
    await websocket.close()
    path = parse_qs(urlparse(response["result"]["url"]).query)["path"][0]
    http = await hass_client_no_auth()
    assert (await http.get(f"{path}/api/v1/fromradio")).status == HTTPStatus.BAD_REQUEST

    if change == "demoted":
        await hass.auth.async_update_user(hass_admin_user, group_ids=[GROUP_ID_READ_ONLY])
    elif change == "deactivated":
        await hass.auth.async_update_user(hass_admin_user, is_active=False)
    else:
        await hass.auth.async_remove_user(hass_admin_user)

    assert (await http.get(f"{path}/api/v1/fromradio")).status == HTTPStatus.FORBIDDEN


async def test_two_tabs_in_one_browser_keep_their_own_sessions(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
    fake_client: FakeClient,
) -> None:
    async def from_radio_stream() -> AsyncIterator[mesh_pb2.FromRadio]:
        await asyncio.Event().wait()
        yield mesh_pb2.FromRadio()

    fake_client._interface = SimpleNamespace(  # type: ignore[attr-defined]
        _connection=SimpleNamespace(forward=AsyncMock()), from_radio_stream=from_radio_stream
    )
    _, gateway = await setup_gateway(hass)
    paths = [
        parse_qs(urlparse((await open_web_client(hass_ws_client, hass, gateway))["result"]["url"]).query)["path"][0]
        for _ in range(2)
    ]
    # one client, so one cookie jar: two tabs of the same browser
    http = await hass_client_no_auth()

    try:
        for config_id, path in enumerate(paths, start=1):
            put = await http.put(
                f"{path}/api/v1/toradio",
                data=mesh_pb2.ToRadio(want_config_id=config_id).SerializeToString(),
                headers={"Content-Type": "application/x-protobuf"},
            )
            assert put.status == HTTPStatus.OK
        with patch("custom_components.meshtastic.meshtastic_web.FROM_RADIO_HOLD_SECONDS", 0.01):
            statuses = [(await http.get(f"{path}/api/v1/fromradio")).status for path in paths]
    finally:
        consumers = [task for task in asyncio.all_tasks() if task.get_name() == "meshtasic-web-consume"]
        for consumer in consumers:
            consumer.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)

    assert statuses == [HTTPStatus.OK, HTTPStatus.OK]


def test_a_token_expires_when_idle() -> None:
    tokens = WebClientTokens()
    token = tokens.issue("entry", "user")
    assert tokens.check("entry", token)

    later = dt.datetime.now(tz=dt.UTC) + TOKEN_IDLE_TIMEOUT + dt.timedelta(seconds=1)
    with patch("custom_components.meshtastic.meshtastic_web.dt.datetime") as clock:
        clock.now.return_value = later
        assert not tokens.check("entry", token)
