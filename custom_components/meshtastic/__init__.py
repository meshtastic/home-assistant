"""
Custom integration to integrate Meshtastic with Home Assistant.

For more details about this integration, please refer to
https://github.com/broglep/homeassistant-meshtastic
"""

from __future__ import annotations

import base64
import datetime
from collections import defaultdict
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_PORT,
    CONF_TYPE,
    Platform,
)
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers.typing import UNDEFINED, ConfigType
from homeassistant.loader import async_get_loaded_integration
from homeassistant.util.hass_dict import HassKey

from meshtastic import BROADCAST_ADDR

from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    MeshtasticApiClient,
)
from .const import (
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    CONF_OPTION_FILTER_NODES,
    CURRENT_CONFIG_VERSION_MAJOR,
    CURRENT_CONFIG_VERSION_MINOR,
    DOMAIN,
    LOGGER,
    SERVICE_SEND_TEXT,
    ConnectionType,
)
from .coordinator import MeshtasticDataUpdateCoordinator
from .data import MeshtasticConfigEntry, MeshtasticData
from .device_trigger import TRIGGER_MESSAGE_RECEIVED, TRIGGER_MESSAGE_SENT
from .entity import GatewayChannelEntity, GatewayEntity, MeshtasticCoordinatorEntity
from .helpers import fetch_meshtastic_hardware_names

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, MutableMapping

    from homeassistant.helpers.device_registry import DeviceRegistry
    from homeassistant.helpers.entity import Entity

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

ENTITY_ID_FORMAT = DOMAIN + ".{}"
PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA
PLATFORM_SCHEMA_BASE = cv.PLATFORM_SCHEMA_BASE
SCAN_INTERVAL = datetime.timedelta(hours=1)

DATA_COMPONENT: HassKey[EntityComponent[MeshtasticCoordinatorEntity]] = HassKey(DOMAIN)

SERVICE_SEND_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required("text"): cv.string,
        vol.Required("to"): cv.string,
        vol.Optional("from"): cv.string,
        vol.Required("ack", default=False): cv.boolean,
    }
)

_SEND_TEXT_CANT_HANDLE_RESPONSE = object()
_service_send_text_handlers: dict[str, Callable[[ServiceCall], Awaitable[ServiceResponse]]] = {}
_remove_listeners: MutableMapping[str, list[Callable[[], None]]] = defaultdict(list)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    component = hass.data[DATA_COMPONENT] = EntityComponent[MeshtasticCoordinatorEntity](
        LOGGER, DOMAIN, hass, SCAN_INTERVAL
    )

    await component.async_setup(config)

    # handler that forwards service call to appropriate handler from config entry
    async def handle_service_send_text(call: ServiceCall) -> ServiceResponse:
        for _handle_send_text_handler in _service_send_text_handlers.values():
            res = await _handle_send_text_handler(call)
            if res != _SEND_TEXT_CANT_HANDLE_RESPONSE:
                return res

        msg = "No gateway could handle the request"
        raise ServiceValidationError(msg)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_TEXT,
        handle_service_send_text,
        schema=SERVICE_SEND_TEXT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

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
    await _setup_meshtastic_devices(hass, entry, client)
    await _setup_meshtastic_entities(hass, entry, client)
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

    await _setup_service_send_text_handler(hass, entry, client)
    await _setup_client_api_text_message_listener(hass, entry)

    return True


async def _setup_service_send_text_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    device_registry = dr.async_get(hass)
    gateway_node = await client.async_get_own_node()

    async def handle_send_text(call: ServiceCall) -> ServiceResponse | object:  # noqa: PLR0912
        def _convert_device_id_to_node_id(device_id: str) -> int:
            device = device_registry.async_get(device_id)
            if device is None:
                msg = f"No device found with id {from_id}"
                raise ServiceValidationError(msg)

            return next((int(i[1]) for i in device.identifiers if i[0] == DOMAIN), None)

        if "from" in call.data:
            from_id = call.data["from"]
            if from_id.startswith("!"):
                if gateway_node["user"]["id"] != from_id:
                    return _SEND_TEXT_CANT_HANDLE_RESPONSE
            elif from_id.isnumeric():
                if int(from_id) != gateway_node["num"]:
                    return _SEND_TEXT_CANT_HANDLE_RESPONSE
            elif from_id.isalnum():
                node_id = _convert_device_id_to_node_id(from_id)
                if node_id != gateway_node["num"]:
                    return _SEND_TEXT_CANT_HANDLE_RESPONSE
        try:
            if "to" in call.data:
                to = call.data["to"]
                if to.startswith("!"):
                    pass
                elif to.isnumeric():
                    to = int(to)
                elif to.isalnum():
                    to = int(_convert_device_id_to_node_id(to))
            else:
                to = BROADCAST_ADDR

            await client.send_text(text=call.data["text"], destination_id=to, want_ack=call.data["ack"])

            if not call.return_response:
                return None
            return {"to": to}  # noqa: TRY300
        except Exception as e:
            LOGGER.warning("Error sending text", exc_info=True)
            msg = "Failed to send text"
            raise ServiceValidationError(msg) from e

    _service_send_text_handlers[entry.entry_id] = handle_send_text


async def _setup_client_api_text_message_listener(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> None:
    device_registry = dr.async_get(hass)

    async def _on_text_message(event: Event) -> None:
        event_data = deepcopy(event.data)
        config_entry_id = event_data.pop(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID, None)
        if config_entry_id != entry.entry_id:
            return

        data = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, None)
        if data is None:
            return

        from_node_id = data["from"]
        from_device = device_registry.async_get_device(identifiers={(DOMAIN, str(from_node_id))})
        to_node_id = data["to"]
        to_device = device_registry.async_get_device(identifiers={(DOMAIN, str(to_node_id))})

        if from_device:
            hass.bus.async_fire(
                event_type=f"{DOMAIN}_event",
                event_data={
                    CONF_DEVICE_ID: from_device.id,
                    CONF_TYPE: TRIGGER_MESSAGE_SENT,
                    "message": data["message"],
                },
            )

        if to_device:
            hass.bus.async_fire(
                event_type=f"{DOMAIN}_event",
                event_data={
                    CONF_DEVICE_ID: to_device.id,
                    CONF_TYPE: TRIGGER_MESSAGE_RECEIVED,
                    "message": data["message"],
                },
            )

    _remove_listeners[entry.entry_id].append(hass.bus.async_listen(EVENT_MESHTASTIC_API_TEXT_MESSAGE, _on_text_message))


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
    gateway_node_id = cast(int, gateway_node["num"])
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
    device_registry.async_update_device(
        d.id,
        new_connections=connections,
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
    await _add_entities_for_entry(hass, [gateway_node_entity], entry)
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
        )
        for channel in channels
        if channel["role"] != "DISABLED"
    ]
    await _add_entities_for_entry(hass, channel_entities, entry)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    for entity in [e for e in hass.data[DATA_COMPONENT].entities if e.registry_entry.config_entry_id == entry.entry_id]:
        await hass.data[DATA_COMPONENT].async_remove_entity(entity.entity_id)

    if entry.runtime_data and entry.runtime_data.client:
        await entry.runtime_data.client.disconnect()

    del _service_send_text_handlers[entry.entry_id]

    for remove_listener in _remove_listeners.pop(entry.entry_id, []):
        remove_listener()

    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> None:
    if config_entries.current_entry.get() is None:
        config_entries.current_entry.set(entry)
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


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
