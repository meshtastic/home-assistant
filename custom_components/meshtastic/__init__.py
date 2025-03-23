"""
Custom integration to integrate Meshtastic with Home Assistant.

For more details about this integration, please refer to
https://github.com/broglep/homeassistant-meshtastic
"""

from __future__ import annotations

import asyncio
import base64
import datetime
from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from homeassistant import config_entries
from homeassistant.components.logbook import DOMAIN as LOGBOOK_DOMAIN
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
from .api import (
    MeshtasticApiClient,
)
from .const import (
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_FILTER_NODES,
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
from .entity import (
    GatewayChannelEntity,
    GatewayDirectMessageEntity,
    GatewayEntity,
    MeshtasticEntity,
)
from .helpers import fetch_meshtastic_hardware_names
from .logbook import async_setup_message_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, MutableMapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.device_registry import DeviceRegistry
    from homeassistant.helpers.entity import Entity

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.NOTIFY]

ENTITY_ID_FORMAT = DOMAIN + ".{}"
PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA
PLATFORM_SCHEMA_BASE = cv.PLATFORM_SCHEMA_BASE
SCAN_INTERVAL = datetime.timedelta(hours=1)


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

    gateway_node = await client.async_get_own_node()
    entry.runtime_data = MeshtasticData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        gateway_node=gateway_node,
    )

    if entry.state == ConfigEntryState.SETUP_IN_PROGRESS:
        await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    await _setup_meshtastic_devices(hass, entry, client)
    await _setup_meshtastic_entities(hass, entry, client)

    await services.async_register_gateway(hass, entry)

    # listeners
    cancel_message_logger = await async_setup_message_logger(hass, entry)
    _remove_listeners[entry.entry_id].append(cancel_message_logger)

    if entry.options.get(CONF_OPTION_WEB_CLIENT, {}).get(
        CONF_OPTION_WEB_CLIENT_ENABLE, CONF_OPTION_WEB_CLIENT_ENABLE_DEFAULT
    ):
        await async_setup_meshtastic_web(hass)

    return True


async def _setup_meshtastic_devices(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
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
    device = device_registry.async_get_device(identifiers={(DOMAIN, str(node_id))})
    # only clean up devices if they are exclusively from us
    if device:
        if device.config_entries == {entry.entry_id}:
            device_registry.async_remove_device(device.id)
        else:
            device_registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


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
    existing_device = device_registry.async_get_device(identifiers={(DOMAIN, str(node_id))})
    via_device = None
    if existing_device is not None and existing_device.config_entries != {entry.entry_id}:
        # get other meshtastic connections

        connection_parts = [
            tuple(v.split("/"))
            for k, v in existing_device.connections
            if k == DOMAIN and not v.startswith(f"{gateway_node_id}/")
        ]
        meshtastic_connections = [
            (int(source), int(target), int(hops), float(snr)) for source, target, hops, snr in connection_parts
        ]
        if node_id == gateway_node_id:
            # add ourselves with highest prio so we don't get another via device
            meshtastic_connections.append((gateway_node_id, node_id, -1, 999))
        else:
            meshtastic_connections.append((gateway_node_id, node_id, hops_away, snr))
        try:
            sorted_connections = sorted(meshtastic_connections, key=lambda x: (x[2], -x[3]))
            closest_gateway = sorted_connections[0][0]
            via_device = (DOMAIN, str(closest_gateway))
        except Exception:  # noqa: BLE001
            LOGGER.warning("Failed to find closest gateway", exc_info=True)
    else:
        via_device = (DOMAIN, str(gateway_node_id)) if gateway_node_id != node_id else None

    # remove via_device when it is set to ourself
    if (via_device is not None and int(via_device[1]) == node_id) or (gateway_node_id == node_id):
        via_device = None

    if existing_device:
        connections.update(existing_device.connections)

    # remove our own entry
    connections = {
        (k, v) for k, v in connections if k != DOMAIN or (k == DOMAIN and not v.startswith(f"{gateway_node_id}/"))
    }

    # add our own entry with updated data
    if gateway_node_id != node_id:
        connections.add((DOMAIN, f"{gateway_node_id}/{node_id}/{hops_away}/{snr}"))

    d = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(node_id))},
        name=node["user"]["longName"],
        model=device_hardware_names.get(node["user"]["hwModel"], None),
        model_id=node["user"]["hwModel"],
        serial_number=node["user"]["id"],
        via_device=via_device,
        sw_version=client.metadata.get("firmwareVersion")
        if gateway_node["num"] == node_id and client.metadata
        else None,
    )
    try:
        device_registry.async_update_device(
            d.id,
            new_connections=connections,
            via_device_id=None if via_device is None else UNDEFINED,
        )
    except DeviceConnectionCollisionError as e:
        LOGGER.debug("Conflict with other device connections, only using meshtastic connections. %s", e)
        own_connections = {(k, v) for k, v in connections if k == DOMAIN}
        device_registry.async_update_device(
            d.id,
            new_connections=own_connections,
            via_device_id=None if via_device is None else UNDEFINED,
        )


async def _setup_meshtastic_entities(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    gateway_node = await client.async_get_own_node()
    local_config = await client.async_get_node_local_config()
    module_config = await client.async_get_node_module_config()

    gateway_node_entity = GatewayEntity(
        config_entry_id=entry.entry_id,
        node=gateway_node["num"],
        long_name=gateway_node["user"]["longName"],
        short_name=gateway_node["user"]["shortName"],
        local_config=local_config,
        module_config=module_config,
    )
    has_logbook = LOGBOOK_DOMAIN in hass.config.all_components
    gateway_direct_message = GatewayDirectMessageEntity(
        config_entry_id=entry.entry_id,
        gateway_node=gateway_node["num"],
        gateway_entity=gateway_node_entity,
        has_logbook=has_logbook,
    )

    await _add_entities_for_entry(hass, [gateway_node_entity, gateway_direct_message], entry)
    channels = await client.async_get_channels()
    channel_entities = [
        GatewayChannelEntity(
            config_entry_id=entry.entry_id,
            gateway_node=gateway_node["num"],
            gateway_entity=gateway_node_entity,
            index=channel["index"],
            name=channel["settings"]["name"],
            primary=channel["role"] == "PRIMARY",
            secondary=channel["role"] == "SECONDARY",
            settings=channel["settings"],
            has_logbook=has_logbook,
        )
        for channel in channels
        if channel["role"] != "DISABLED"
    ]
    await _add_entities_for_entry(hass, channel_entities, entry)


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
        for entity in [
            e for e in hass.data[DATA_COMPONENT].entities if e.registry_entry.config_entry_id == entry.entry_id
        ]:
            await hass.data[DATA_COMPONENT].async_remove_entity(entity.entity_id)

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

    return unload_ok


_reload_lock = asyncio.Lock()


async def async_reload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> None:
    async with _reload_lock:
        token = None
        if config_entries.current_entry.get() is None:
            token = config_entries.current_entry.set(entry)
        try:
            await async_unload_entry(hass, entry)
            await async_setup_entry(hass, entry)
        finally:
            if token:
                config_entries.current_entry.reset(token)


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


async def _add_entities_for_entry(hass: HomeAssistant, entities: list[Entity], entry: MeshtasticConfigEntry) -> None:
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    await hass.data[DATA_COMPONENT].async_add_entities(entities)
    # attach entities to config entry (as async_add_entities does not support apply config_entry_id from entities)
    for e in entities:
        device_id = UNDEFINED
        if e.device_info:
            device = device_registry.async_get_device(identifiers=e.device_info["identifiers"])
            if device:
                device_id = device.id
        try:
            entity_registry.async_update_entity(e.entity_id, config_entry_id=entry.entry_id, device_id=device_id)
        except:  # noqa: E722
            LOGGER.warning("Failed to update entity %s", e, exc_info=True)
