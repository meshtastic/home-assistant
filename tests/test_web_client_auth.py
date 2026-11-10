# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The web client's radio API is reachable only under a token an admin obtained."""

from __future__ import annotations

import datetime as dt
from http import HTTPStatus
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.meshtastic.const import (
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_WEB_CLIENT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    DOMAIN,
)
from custom_components.meshtastic.meshtastic_web import TOKEN_IDLE_TIMEOUT, WebClientTokens

from .conftest import GATEWAY_NUM, PEER_NUM, make_entry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator, WebSocketGenerator

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

    assert wrong.status == HTTPStatus.UNAUTHORIZED
    assert foreign.status == HTTPStatus.UNAUTHORIZED
    assert put.status == HTTPStatus.UNAUTHORIZED
    assert untokened.headers.get("Content-Type", "").startswith("text/html")


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


def test_a_token_expires_when_idle() -> None:
    tokens = WebClientTokens()
    token = tokens.issue("entry", "user")
    assert tokens.check("entry", token)

    later = dt.datetime.now(tz=dt.UTC) + TOKEN_IDLE_TIMEOUT + dt.timedelta(seconds=1)
    with patch("custom_components.meshtastic.meshtastic_web.dt.datetime") as clock:
        clock.now.return_value = later
        assert not tokens.check("entry", token)
