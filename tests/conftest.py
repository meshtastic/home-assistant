# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Shared fixtures: a fake radio client so no test opens a socket."""

from __future__ import annotations

import base64
import zlib
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.meshtastic.aiomeshtastic.interface import ConnectionState
from custom_components.meshtastic.const import (
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_FILTER_NODES,
    DOMAIN,
    ConnectionType,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from homeassistant.core import HomeAssistant

HOST = "192.0.2.10"
GATEWAY_KEY = base64.b64encode(bytes(range(32))).decode()
PEER_KEY = base64.b64encode(bytes(range(100, 132))).decode()
GATEWAY_NUM = 0x11111111
PEER_NUM = 0x22222222


def canonical(key: str) -> int:
    return zlib.crc32(base64.b64decode(key)) & 0xFFFFFFFF


def make_node(num: int, long_name: str, public_key: str | None = None, **extra: Any) -> dict[str, Any]:
    user: dict[str, Any] = {"id": f"!{num:08x}", "longName": long_name, "shortName": long_name[:4]}
    if public_key:
        user["publicKey"] = public_key
    return {"num": num, "user": user, **extra}


def make_nodes() -> dict[int, dict[str, Any]]:
    return {
        GATEWAY_NUM: make_node(GATEWAY_NUM, "Gateway", GATEWAY_KEY),
        PEER_NUM: make_node(PEER_NUM, "Peer", PEER_KEY),
    }


class FakeClient:
    """Stands in for MeshtasticApiClient; holds the node DB the tests mutate."""

    instances: list[FakeClient]

    def __init__(self, nodes: Mapping[int, dict[str, Any]], gateway_num: int) -> None:
        self.nodes = dict(nodes)
        self.gateway_num = gateway_num
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.metadata = {"firmwareVersion": "2.8.0"}
        self.my_info = {"myNodeNum": gateway_num}
        self.connection_state = ConnectionState.CONNECTED

    def add_connection_state_listener(self, listener: Any) -> Any:  # noqa: ARG002
        return lambda: None

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    def get_own_node(self) -> dict[str, Any]:
        return self.nodes[self.gateway_num]

    def get_node_info(self, node_id: int) -> None:  # noqa: ARG002
        return None

    async def async_get_own_node(self) -> dict[str, Any]:
        return self.get_own_node()

    async def async_get_all_nodes(self) -> dict[int, dict[str, Any]]:
        return dict(self.nodes)

    async def async_get_node_local_config(self) -> dict[str, Any]:
        return {
            "security": {"privateKey": "secret", "adminKey": ["a"]},
            "network": {"wifiSsid": "home", "wifiPsk": "wifi-password"},
            "bluetooth": {"fixedPin": 123456, "mode": "FIXED_PIN"},
        }

    async def async_get_node_module_config(self) -> dict[str, Any]:
        return {"mqtt": {"address": "mqtt.example", "password": "mqtt-password"}}

    async def async_get_channels(self) -> list[dict[str, Any]]:
        settings = {"name": "LongFast", "psk": "c2VjcmV0", "uplinkEnabled": False, "downlinkEnabled": False}
        return [{"index": 0, "role": "PRIMARY", "settings": settings}]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:  # noqa: ARG001
    return


@pytest.fixture
def nodes() -> dict[int, dict[str, Any]]:
    return make_nodes()


@pytest.fixture
def fake_client(nodes: dict[int, dict[str, Any]]) -> Generator[FakeClient]:
    client = FakeClient(nodes, GATEWAY_NUM)
    with (
        patch("custom_components.meshtastic.MeshtasticApiClient", return_value=client),
        patch("custom_components.meshtastic.fetch_meshtastic_hardware_names", AsyncMock(return_value={})),
    ):
        yield client


@pytest.fixture
def validate_input(nodes: dict[int, dict[str, Any]]) -> Generator[AsyncMock]:
    """Patch the config flow's connection boundary to return the gateway node and the node DB."""
    with patch(
        "custom_components.meshtastic.config_flow.validate_input_for_connection",
        AsyncMock(return_value=(nodes[GATEWAY_NUM], nodes)),
    ) as mock:
        yield mock


def make_entry(options: dict[str, Any] | None = None, **kwargs: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        minor_version=2,
        unique_id=str(GATEWAY_NUM),
        title="Gateway",
        data={
            CONF_CONNECTION_TYPE: ConnectionType.TCP.value,
            CONF_CONNECTION_TCP_HOST: HOST,
            CONF_CONNECTION_TCP_PORT: 4403,
        },
        options=options
        if options is not None
        else {CONF_OPTION_FILTER_NODES: [{"id": GATEWAY_NUM, "name": "Gateway"}, {"id": PEER_NUM, "name": "Peer"}]},
        **kwargs,
    )


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = make_entry()
    entry.add_to_hass(hass)
    return entry
