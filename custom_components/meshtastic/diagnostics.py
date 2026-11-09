# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Diagnostics for bug reports: gateway state, node config and the selected nodes, secrets removed."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

from .aiomeshtastic.protobuf import __version__ as PROTOBUF_VERSION  # noqa: N812
from .const import (
    CONF_CONNECTION_BLUETOOTH_ADDRESS,
    CONF_CONNECTION_SERIAL_PORT,
    CONF_CONNECTION_TCP_HOST,
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_TCP_PROXY_ENABLE,
    CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT,
    CONF_OPTION_TCP_PROXY_PORT,
    CONF_OPTION_TCP_PROXY_PORT_DEFAULT,
    CONF_OPTION_WEB_CLIENT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceEntry

    from .data import MeshtasticConfigEntry

# Keys are protobuf JSON names (MessageToDict camelCase) plus the entry's own option keys.
# async_redact_data matches at any depth, so one set covers config, module config, node DB and entry data.
TO_REDACT = {
    # entry data and options
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_BLUETOOTH_ADDRESS,
    CONF_CONNECTION_SERIAL_PORT,
    "public_key",
    "title",
    # keys: Security config, User, Channel settings and the selected-node options
    "publicKey",
    "privateKey",
    "adminKey",
    "psk",
    # identifiers that fingerprint a device
    "macaddr",
    "deviceId",
    # NetworkConfig and its static IPv4 block
    "wifiSsid",
    "wifiPsk",
    "ip",
    "gateway",
    "subnet",
    "dns",
    # MQTT module
    "address",
    "username",
    "password",
    # BluetoothConfig pairing PIN
    "fixedPin",
    # Position, nodes and the gateway's own
    "latitude",
    "longitude",
    "altitude",
    "latitudeI",
    "longitudeI",
    "altitudeHae",
    "altitudeGeoidalSeparation",
}


def _channel_summary(channels: list[Any]) -> list[dict[str, Any]]:
    # Never copy a channel wholesale: its settings carry the PSK and MQTT flags we do not need.
    return [
        {
            "index": channel.get("index"),
            "role": channel.get("role"),
            "name": (channel.get("settings") or {}).get("name"),
        }
        for channel in channels
    ]


def _node_numbers(entry: MeshtasticConfigEntry) -> list[int]:
    return [node["id"] for node in entry.options.get(CONF_OPTION_FILTER_NODES, []) if "id" in node]


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> dict[str, Any]:  # noqa: ARG001
    diagnostics: dict[str, Any] = {
        "config_entry": {
            "title": entry.title,
            "state": str(entry.state),
            "version": f"{entry.version}.{entry.minor_version}",
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "protobuf_version": PROTOBUF_VERSION,
        "tcp_proxy": {
            "enabled": entry.options.get(CONF_OPTION_TCP_PROXY, {}).get(
                CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT
            ),
            "port": entry.options.get(CONF_OPTION_TCP_PROXY, {}).get(
                CONF_OPTION_TCP_PROXY_PORT, CONF_OPTION_TCP_PROXY_PORT_DEFAULT
            ),
        },
        "web_client": {
            "enabled": entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
                CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
            ),
        },
    }

    # runtime_data is unset when setup failed, which is exactly when a report is most needed
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        client = runtime.client
        diagnostics["integration_version"] = str(runtime.integration.version)
        diagnostics["gateway"] = {
            "my_info": client.my_info,
            "metadata": client.metadata,
            "node": client.get_own_node(),
            "local_config": await client.async_get_node_local_config(),
            "module_config": await client.async_get_node_module_config(),
            "channels": _channel_summary(list(await client.async_get_channels())),
        }
        diagnostics["node_db"] = {
            "total_nodes": client.node_count,
            "selected_nodes": _node_numbers(entry),
        }
        diagnostics["coordinator"] = {
            "last_update_success": runtime.coordinator.last_update_success,
            "data": {str(num): node for num, node in (runtime.coordinator.data or {}).items()},
        }

    return async_redact_data(diagnostics, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MeshtasticConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    node_num = next((int(ident) for domain, ident in device.identifiers if domain == DOMAIN and ident.isdigit()), None)
    runtime = getattr(entry, "runtime_data", None)
    coordinator_data = (runtime.coordinator.data or {}) if runtime is not None else {}

    return async_redact_data(
        {
            "node_num": node_num,
            "model": device.model,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "coordinator_data": coordinator_data.get(node_num),
        },
        TO_REDACT,
    )
