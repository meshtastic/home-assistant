# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""
Custom integration to integrate Meshtastic with Home Assistant.

For more details about this integration, please refer to
https://github.com/meshtastic/home-assistant
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_HOST,
    CONF_PORT,
    Platform,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceConnectionCollisionError
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.typing import UNDEFINED, ConfigType
from homeassistant.loader import async_get_loaded_integration

from . import frontend, meshtastic_web, services
from .aiomeshtastic.interface import ConnectionState
from .api import (
    MeshtasticApiClient,
)
from .const import (
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_TCP_PROXY,
    CONF_OPTION_TCP_PROXY_ENABLE,
    CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT,
    CONF_OPTION_WEB_CLIENT,
    CONF_OPTION_WEB_CLIENT_ENABLE,
    CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT,
    CURRENT_CONFIG_VERSION_MAJOR,
    CURRENT_CONFIG_VERSION_MINOR,
    DOMAIN,
    LOGGER,
    ConnectionType,
)
from .coordinator import MeshtasticDataUpdateCoordinator
from .data import DATA_COMPONENT, MeshtasticConfigEntry, MeshtasticData
from .entity import MeshtasticDeviceClass, MeshtasticEntity
from .helpers import fetch_meshtastic_hardware_names
from .logbook import async_setup_message_logger
from .meshtastic_tcp import async_setup_tcp_proxy, async_unload_tcp_proxy
from .renumber import canonical_node_num, find_renumbered_nodes, node_public_key, rewrite_unique_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceRegistry

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.NOTIFY]

ENTITY_ID_FORMAT = DOMAIN + ".{}"
PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA
PLATFORM_SCHEMA_BASE = cv.PLATFORM_SCHEMA_BASE
SCAN_INTERVAL = dt.timedelta(hours=1)


_remove_listeners: MutableMapping[str, list[Callable[[], None]]] = defaultdict(list)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    component = hass.data[DATA_COMPONENT] = EntityComponent[MeshtasticEntity](LOGGER, DOMAIN, hass, SCAN_INTERVAL)

    await component.async_setup(config)
    await services.async_setup_services(hass)

    return True


async def async_setup_meshtastic_web(hass: HomeAssistant) -> bool:
    if hass.data[DOMAIN].config.get("meshtastic_web_loaded", False):
        return True

    try:
        await meshtastic_web.async_setup(hass)
        await frontend.async_register_frontend(hass)
        hass.data[DOMAIN].config["meshtastic_web_loaded"] = True
    except:  # noqa: E722
        LOGGER.warning("Failed to setup frontend", exc_info=True)
        return False
    else:
        return True


async def async_unload_meshtastic_web(hass: HomeAssistant) -> bool:
    if not hass.data[DOMAIN].config.get("meshtastic_web_loaded", False):
        return True

    try:
        await frontend.async_unregister_frontend(hass)
        hass.data[DOMAIN].config["meshtastic_web_loaded"] = False
    except:  # noqa: E722
        LOGGER.warning("Failed to unload frontend", exc_info=True)
        return False
    else:
        return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    coordinator = MeshtasticDataUpdateCoordinator(hass=hass)
    if coordinator.config_entry is None:
        coordinator.config_entry = entry

    client = MeshtasticApiClient(entry.data, hass=hass, config_entry_id=entry.entry_id)

    try:
        await client.connect()
    except Exception as e:
        raise ConfigEntryNotReady from e

    try:
        await _async_setup_connected_entry(hass, entry, client, coordinator)
    except BaseException:
        # unload never runs for a failed setup, and many radios accept only one API connection;
        # the options flow must not pick up the stopped client either
        entry.runtime_data = None  # type: ignore[assignment]  # cleared so the options flow skips the stopped client
        with contextlib.suppress(Exception):
            await client.disconnect()
        # a retry must not find the entity platform of the failed attempt still registered
        with contextlib.suppress(ValueError):
            await hass.data[DATA_COMPONENT].async_unload_entry(entry)
        raise

    return True


async def _async_setup_connected_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    client: MeshtasticApiClient,
    coordinator: MeshtasticDataUpdateCoordinator,
) -> None:
    gateway_node = await client.async_get_own_node()
    await _async_follow_renumbered_nodes(hass, entry, client, gateway_node)
    entry.runtime_data = MeshtasticData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        gateway_node=gateway_node,
    )

    entry.async_on_unload(client.add_connection_state_listener(coordinator.async_connection_state_changed))
    if client.connection_state != ConnectionState.CONNECTED:
        coordinator.async_connection_state_changed(client.connection_state)

    if entry.state == ConfigEntryState.SETUP_IN_PROGRESS:
        await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await _setup_meshtastic_devices(hass, entry, client)
    # the devices exist first, so the platform's entities attach to them
    await hass.data[DATA_COMPONENT].async_setup_entry(entry)

    await services.async_register_gateway(hass, entry)

    # listeners
    cancel_message_logger = await async_setup_message_logger(hass, entry)
    _remove_listeners[entry.entry_id].append(cancel_message_logger)

    if entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
        CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
    ):
        await async_setup_meshtastic_web(hass)

    if entry.options.get(CONF_OPTION_TCP_PROXY, {}).get(
        CONF_OPTION_TCP_PROXY_ENABLE, CONF_OPTION_TCP_PROXY_ENABLE_DEFAULT
    ):
        await async_setup_tcp_proxy(hass, entry)


async def _async_follow_renumbered_nodes(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    client: MeshtasticApiClient,
    gateway_node: Mapping[str, Any],
) -> None:
    nodes = await client.async_get_all_nodes()
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    filter_nodes = entry.options.get(CONF_OPTION_FILTER_NODES, [])

    renumbered = find_renumbered_nodes({el["id"]: el.get("public_key") for el in filter_nodes}, nodes)

    # the gateway is this entry's own connection, so continuity holds whatever its key does
    gateway_num = gateway_node.get("num")
    gateway_prefix = f"{entry.entry_id}_{MeshtasticDeviceClass.GATEWAY}_"
    old_gateway_num = next(
        (
            int(e.unique_id[len(gateway_prefix) :].split("_")[0])
            for e in entries
            if e.unique_id.startswith(gateway_prefix)
        ),
        None,
    )
    if old_gateway_num is not None and _is_gateway_renumber(old_gateway_num, gateway_node, filter_nodes, nodes):
        renumbered[old_gateway_num] = gateway_node["num"]

    for old_num, new_num in renumbered.items():
        if device_registry.async_get_device_by_identifier((DOMAIN, str(new_num)), entry.entry_id) is not None:
            LOGGER.warning("Node %s renumbered to %s, which already has a device; not merging", old_num, new_num)
            continue
        device = device_registry.async_get_device_by_identifier((DOMAIN, str(old_num)), entry.entry_id)
        if device is not None:
            device_registry.async_update_device(device.id, new_identifiers={(DOMAIN, str(new_num))})
        LOGGER.info("Node %s renumbered to %s, moved its device and entities", old_num, new_num)

    if renumbered:
        node_unique_ids = {f"meshtastic_node_{old}": f"meshtastic_node_{new}" for old, new in renumbered.items()}
        candidates = list(entries) + [
            e for e in entity_registry.entities.values() if e.platform == DOMAIN and e.unique_id in node_unique_ids
        ]
        for registry_entry in candidates:
            new_unique_id = node_unique_ids.get(registry_entry.unique_id) or rewrite_unique_id(
                registry_entry.unique_id, renumbered
            )
            if new_unique_id == registry_entry.unique_id or entity_registry.async_get_entity_id(
                registry_entry.domain, registry_entry.platform, new_unique_id
            ):
                continue
            entity_registry.async_update_entity(registry_entry.entity_id, new_unique_id=new_unique_id)

    # keep each selected node's key so a later renumber is followed even after the radio forgets the old entry
    new_filter_nodes = []
    for el in filter_nodes:
        num = renumbered.get(el["id"], el["id"])
        key = node_public_key(nodes.get(num)) or el.get("public_key")
        new_filter_nodes.append({**el, "id": num} | ({"public_key": key} if key else {}))

    updates: dict[str, Any] = {}
    if new_filter_nodes != filter_nodes:
        updates["options"] = {**entry.options, CONF_OPTION_FILTER_NODES: new_filter_nodes}
    if old_gateway_num in renumbered and entry.unique_id == str(old_gateway_num):
        updates["unique_id"] = str(gateway_num)
    if updates:
        hass.config_entries.async_update_entry(entry, **updates)


def _is_gateway_renumber(
    old_num: int,
    gateway_node: Mapping[str, Any],
    filter_nodes: list[dict[str, Any]],
    nodes: Mapping[int, Mapping[str, Any]],
) -> bool:
    # a swapped port or reused address puts another radio behind the same connection, so its number changing
    # is only a renumber when the key is the old gateway's, or, with no old key known, the firmware 2.8 one
    new_num = gateway_node.get("num")
    if new_num is None or new_num == old_num:
        return False
    new_key = node_public_key(gateway_node)
    old_key = next((el.get("public_key") for el in filter_nodes if el["id"] == old_num), None) or node_public_key(
        nodes.get(old_num)
    )
    same = old_key == new_key if old_key else new_key is not None and canonical_node_num(new_key) == new_num
    if not same:
        LOGGER.warning(
            "Gateway changed from node %s to %s with another key; treating it as a new radio", old_num, new_num
        )
    return same


async def _setup_meshtastic_devices(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> Mapping[str, Any]:
    gateway_node = await client.async_get_own_node()
    nodes = await client.async_get_all_nodes()
    device_registry = dr.async_get(hass)
    filter_nodes = entry.options.get(CONF_OPTION_FILTER_NODES, [])
    filter_node_nums = [el["id"] for el in filter_nodes]
    device_hardware_names = await fetch_meshtastic_hardware_names(hass)
    for node_id, node in nodes.items():
        if node_id in filter_node_nums:
            await _setup_meshtastic_device(
                client, device_hardware_names, device_registry, entry, gateway_node, node, node_id
            )

        else:
            await _remove_meshtastic_device(device_registry, entry, node_id)
    return gateway_node


async def _remove_meshtastic_device(
    device_registry: DeviceRegistry, entry: MeshtasticConfigEntry, node_id: int
) -> None:
    device = device_registry.async_get_device_by_identifier((DOMAIN, str(node_id)), entry.entry_id)
    if device:
        device_registry.async_remove_device(device.id)


async def _setup_meshtastic_device(  # noqa: PLR0913
    client: MeshtasticApiClient,
    device_hardware_names: Mapping[str, str],
    device_registry: DeviceRegistry,
    entry: MeshtasticConfigEntry,
    gateway_node: Mapping[str, Any],
    node: Mapping[str, Any],
    node_id: int,
) -> None:
    gateway_node_id = cast("int", gateway_node["num"])
    mac_address = base64.b64decode(node["user"]["macaddr"]).hex(":") if "macaddr" in node["user"] else None
    connections = set()
    if mac_address:
        connections.add((dr.CONNECTION_NETWORK_MAC, mac_address))
    hops_away = node.get("hopsAway", 99)
    snr = node.get("snr", 0)
    existing_device = device_registry.async_get_device_by_identifier((DOMAIN, str(node_id)), entry.entry_id)
    # None clears the via device (the gateway has none); UNDEFINED keeps it while the gateway device is unregistered
    via_device_id = None
    if gateway_node_id != node_id:
        gateway_device = device_registry.async_get_device_by_identifier((DOMAIN, str(gateway_node_id)), entry.entry_id)
        via_device_id = gateway_device.id if gateway_device is not None else UNDEFINED

    if existing_device:
        connections.update(existing_device.connections)

    # remove our own entry
    connections = {
        (k, v) for k, v in connections if k != DOMAIN or (k == DOMAIN and not v.startswith(f"{gateway_node_id}/"))
    }

    # add our own entry with updated data
    if gateway_node_id != node_id:
        connections.add((DOMAIN, f"{gateway_node_id}/{node_id}/{hops_away}/{snr}"))

    # MessageToDict omits hwModel for UNSET and placeholder nodes spell it out; keep any known model
    hw_model = node["user"].get("hwModel")
    known = hw_model not in (None, "UNSET")
    d = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(node_id))},
        name=node["user"]["longName"],
        model=device_hardware_names.get(hw_model) if known else UNDEFINED,
        model_id=str(hw_model) if known else UNDEFINED,
        serial_number=node["user"]["id"],
        via_device_id=via_device_id,
        sw_version=client.metadata.get("firmwareVersion")
        if gateway_node["num"] == node_id and client.metadata
        else None,
    )
    try:
        device_registry.async_update_device(
            d.id,
            new_connections=connections,
            via_device_id=via_device_id,
        )
    except DeviceConnectionCollisionError as e:
        LOGGER.debug("Conflict with other device connections, only using meshtastic connections. %s", e)
        own_connections = {(k, v) for k, v in connections if k == DOMAIN}
        device_registry.async_update_device(
            d.id,
            new_connections=own_connections,
            via_device_id=via_device_id,
        )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    # ensure that we disconnect first to prevent later issues with duplicate connection in case of errors
    try:
        if entry.runtime_data and entry.runtime_data.client:
            await entry.runtime_data.client.disconnect()
    except:  # noqa: E722
        LOGGER.warning("Failed to disconnect client during unload of entry", exc_info=True)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await hass.data[DATA_COMPONENT].async_unload_entry(entry)

        await services.async_unregister_gateway(hass, entry)

        for remove_listener in _remove_listeners.pop(entry.entry_id, []):
            remove_listener()

        active_entries = hass.config_entries.async_entries(DOMAIN, include_ignore=False, include_disabled=False)
        any_web_client_enabled = any(
            e.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
                CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
            )
            for e in active_entries
        )

        if not any_web_client_enabled:
            await async_unload_meshtastic_web(hass)

        await async_unload_tcp_proxy(hass, entry)

    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MeshtasticConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow deleting a node device unless it is the gateway or a node the entry still selects."""
    kept_nodes = {str(el["id"]) for el in entry.options.get(CONF_OPTION_FILTER_NODES, [])}
    if entry.runtime_data is not None:
        kept_nodes.add(str(entry.runtime_data.gateway_node["num"]))
    elif entry.unique_id is not None:
        kept_nodes.add(entry.unique_id)

    return not any(domain == DOMAIN and node in kept_nodes for domain, node in device_entry.identifiers)


async def async_migrate_entry(hass: HomeAssistant, config_entry: MeshtasticConfigEntry) -> bool:
    LOGGER.debug("Migrating configuration from version %s.%s", config_entry.version, config_entry.minor_version)

    if config_entry.version > CURRENT_CONFIG_VERSION_MAJOR:
        # This means the user has downgraded from a future version
        return False

    if config_entry.version == 1:
        new_data = {**config_entry.data}
        if config_entry.minor_version < 2:  # noqa: PLR2004
            new_data.update(
                {
                    CONF_CONNECTION_TYPE: ConnectionType.TCP.value,
                    CONF_CONNECTION_TCP_HOST: new_data.pop(CONF_HOST),
                    CONF_CONNECTION_TCP_PORT: new_data.pop(CONF_PORT),
                }
            )

        hass.config_entries.async_update_entry(
            config_entry,
            data=new_data,
            minor_version=CURRENT_CONFIG_VERSION_MINOR,
            version=CURRENT_CONFIG_VERSION_MAJOR,
        )

    LOGGER.debug(
        "Migration to configuration version %s.%s successful", config_entry.version, config_entry.minor_version
    )

    return True
