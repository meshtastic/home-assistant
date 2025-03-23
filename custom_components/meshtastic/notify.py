from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er

from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    ATTR_EVENT_MESHTASTIC_API_NODE,
    EVENT_MESHTASTIC_API_NODE_UPDATED,
)
from .const import (
    ATTR_SERVICE_DATA_ACK,
    ATTR_SERVICE_DATA_CHANNEL,
    ATTR_SERVICE_DATA_FROM,
    ATTR_SERVICE_DATA_TO,
    ATTR_SERVICE_SEND_TEXT_DATA_TEXT,
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_NOTIFY_PLATFORM,
    CONF_OPTION_NOTIFY_PLATFORM_CHANNELS,
    CONF_OPTION_NOTIFY_PLATFORM_CHANNELS_DEFAULT,
    CONF_OPTION_NOTIFY_PLATFORM_NODES,
    CONF_OPTION_NOTIFY_PLATFORM_NODES_DEFAULT,
    DOMAIN,
    LOGGER,
    SERVICE_SEND_TEXT,
    ConfigOptionNotifyPlatformNodes,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import Event, HomeAssistant, _DataT
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.entity_platform import AddEntitiesCallback, EntityPlatform
    from homeassistant.helpers.entity_registry import EntityRegistry

    from .data import MeshtasticConfigEntry


def _create_node_entity_filter_factory(config_entry: MeshtasticConfigEntry) -> Callable[[int], bool]:
    create_nodes = config_entry.options.get(CONF_OPTION_NOTIFY_PLATFORM, {}).get(
        CONF_OPTION_NOTIFY_PLATFORM_NODES, CONF_OPTION_NOTIFY_PLATFORM_NODES_DEFAULT
    )
    if create_nodes == ConfigOptionNotifyPlatformNodes.NONE:
        return lambda _: False

    if create_nodes == ConfigOptionNotifyPlatformNodes.ALL:
        return lambda _: True

    if create_nodes == ConfigOptionNotifyPlatformNodes.SELECTED:
        filter_nodes = config_entry.options.get(CONF_OPTION_FILTER_NODES, [])
        filter_node_nums = [el["id"] for el in filter_nodes]
        return lambda node_id: node_id in filter_node_nums

    raise ValueError


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entity_registry = er.async_get(hass)
    platform = entity_platform.async_get_current_platform()
    await _add_node_entities(hass, config_entry, platform, entity_registry, async_add_entities)
    await _add_channel_entities(hass, config_entry, platform, entity_registry, async_add_entities)

    should_create_node = _create_node_entity_filter_factory(config_entry)

    @callback
    def _api_node_updated(event: Event[_DataT]) -> None:
        event_data = deepcopy(event.data)
        config_entry_id = event_data.pop(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID, None)
        if config_entry_id != config_entry.entry_id:
            return
        node_id = event_data.get(ATTR_EVENT_MESHTASTIC_API_NODE, None)
        node_info = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, None)

        if not should_create_node(node_id):
            return

        if "user" not in node_info or "longName" not in node_info["user"]:
            return

        entity = MeshtasticNodeNotify(node_id=node_id, entity_name=f"{node_info['user']['longName']}")
        registered_entity_id = entity_registry.async_get_entity_id(
            platform.domain, platform.platform_name, entity.unique_id
        )
        if registered_entity_id is None or registered_entity_id not in platform.domain_entities:
            async_add_entities([entity])
        else:
            existing_entity = platform.domain_entities[registered_entity_id]
            if existing_entity.name != entity.name:
                existing_entity.update_from(entity)
                entity_registry.async_update_entity(registered_entity_id, name=entity.name)

    hass.bus.async_listen(EVENT_MESHTASTIC_API_NODE_UPDATED, _api_node_updated)


@callback
async def _add_node_entities(
    hass: HomeAssistant,
    config_entry: MeshtasticConfigEntry,
    platform: EntityPlatform,
    entity_registry: EntityRegistry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    should_create_node = _create_node_entity_filter_factory(config_entry)
    nodes = await config_entry.runtime_data.client.async_get_all_nodes()
    entities = [
        MeshtasticNodeNotify(node_id=node_id, entity_name=f"{node_info['user']['longName']}")
        for node_id, node_info in nodes.items()
        if should_create_node(node_id)
    ]
    platform = entity_platform.async_get_current_platform()
    entity_registry = er.async_get(hass)
    new_entities = []
    for e in entities:
        registered_entity_id = entity_registry.async_get_entity_id(platform.domain, platform.platform_name, e.unique_id)
        if registered_entity_id is None or registered_entity_id not in platform.domain_entities:
            new_entities.append(e)
        else:
            existing_entity = platform.domain_entities[registered_entity_id]
            if existing_entity.name != e.name:
                existing_entity.update_from(e)
                entity_registry.async_update_entity(registered_entity_id, name=e.name)
    if new_entities:
        async_add_entities(new_entities)


def _channel_global_id(channel: Mapping[str, Any]) -> str:
    h = hashlib.blake2b(key=b"global_channel_id", digest_size=16)
    h.update(base64.b64decode(channel["settings"]["psk"]))
    return base64.b32encode(h.digest()).decode().rstrip("=")


async def _add_channel_entities(
    hass: HomeAssistant,
    config_entry: MeshtasticConfigEntry,
    platform: EntityPlatform,
    entity_registry: EntityRegistry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if not (
        config_entry.options.get(CONF_OPTION_NOTIFY_PLATFORM, {}).get(
            CONF_OPTION_NOTIFY_PLATFORM_CHANNELS, CONF_OPTION_NOTIFY_PLATFORM_CHANNELS_DEFAULT
        )
    ):
        return

    gateway = await config_entry.runtime_data.client.async_get_own_node()
    channels = await config_entry.runtime_data.client.async_get_channels()
    entities = [
        MeshtasticChannelNotify(channel, gateway_node_id=gateway["num"])
        for channel in channels
        if channel["role"] != "DISABLED"
    ]
    platform = entity_platform.async_get_current_platform()
    entity_registry = er.async_get(hass)
    new_entities = []
    for e in entities:
        registered_entity_id = entity_registry.async_get_entity_id(platform.domain, platform.platform_name, e.unique_id)
        if registered_entity_id is None or registered_entity_id not in platform.domain_entities:
            new_entities.append(e)
        else:
            existing_entity = platform.domain_entities[registered_entity_id]
            existing_entity.update_from(e)

    if new_entities:
        async_add_entities(new_entities)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    pass


class MeshtasticNodeNotify(NotifyEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:radio-handheld"

    def __init__(
        self,
        node_id: int,
        entity_name: str | None,
        device_info: DeviceInfo | None = None,
        supported_features: NotifyEntityFeature = None,
    ) -> None:
        self._node_id = node_id
        self._attr_unique_id = f"meshtastic_node_{node_id}"
        self._attr_supported_features = supported_features if supported_features is not None else NotifyEntityFeature(0)
        self._attr_device_info = device_info
        self._attr_name = f"Node {entity_name}"
        self._attr_extra_state_attributes = {"node_id": node_id, "user_id": f"!{node_id:08x}"}

    async def async_send_message(self, message: str, title: str | None = None) -> None:  # noqa: ARG002
        service_data = {
            ATTR_SERVICE_SEND_TEXT_DATA_TEXT: message,
            ATTR_SERVICE_DATA_TO: str(self._node_id),
            ATTR_SERVICE_DATA_ACK: True,
        }

        await self.hass.services.async_call(DOMAIN, SERVICE_SEND_TEXT, service_data, blocking=True)

    def update_from(self, entity: MeshtasticNodeNotify) -> None:
        if entity._node_id != self._node_id:
            return

        self._attr_name = entity._attr_name

    @property
    def suggested_object_id(self) -> str | None:
        return f"mesh {self._attr_name}"


class MeshtasticChannelNotify(NotifyEntity):
    _attr_should_poll = False
    _attr_icon = "mdi:forum"
    _attr_translation_key = "mesh_channel"

    def __init__(
        self,
        channel: Mapping[str, Any],
        gateway_node_id: int,
        device_info: DeviceInfo | None = None,
        supported_features: NotifyEntityFeature = None,
    ) -> None:
        global_channel_id = _channel_global_id(channel)
        self._global_channel_id = global_channel_id
        self._attr_unique_id = f"meshtastic_channel_{global_channel_id}"
        self._attr_supported_features = supported_features if supported_features is not None else NotifyEntityFeature(0)
        self._attr_device_info = device_info

        if channel["settings"]["name"]:
            self._attr_has_entity_name = True
            self._attr_name = channel["settings"]["name"]
        elif channel["settings"]["psk"] == "AQ==":
            self._attr_has_entity_name = True
            self._attr_name = "Primary Default"
        elif channel["role"] == "PRIMARY":
            self._attr_has_entity_name = True
            self._attr_name = "Primary"
        elif channel["role"] == "SECONDARY":
            self._attr_has_entity_name = True
            self._attr_name = "Secondary"

        if self._attr_name:
            self._attr_name = f"Channel {self._attr_name}"

        self._attr_extra_state_attributes = {}
        self.add_gateway(gateway_node_id, channel["index"])

    async def async_send_message(self, message: str, title: str | None = None) -> None:  # noqa: ARG002
        if not self.gateways:
            msg = "No gateway available"
            raise ServiceValidationError(msg)

        for gateway_node_id, gateway_node_channel_index in self.gateways.items():
            service_data = {
                ATTR_SERVICE_SEND_TEXT_DATA_TEXT: message,
                ATTR_SERVICE_DATA_FROM: gateway_node_id,
                ATTR_SERVICE_DATA_CHANNEL: gateway_node_channel_index,
                ATTR_SERVICE_DATA_ACK: True,
            }
            try:
                await self.hass.services.async_call(DOMAIN, SERVICE_SEND_TEXT, service_data, blocking=True)
            except HomeAssistantError:
                LOGGER.info("Failed to send to channel, trying next gateway", exc_info=True)
            else:
                return

        msg = "Could not send to channel"
        raise ServiceValidationError(msg)

    @property
    def suggested_object_id(self) -> str | None:
        return f"mesh {self._attr_name}"

    def add_gateway(self, gateway_node_id: int, channel_index: int) -> None:
        if "gateways" not in self._attr_extra_state_attributes:
            self._attr_extra_state_attributes["gateways"] = {}

        self._attr_extra_state_attributes["gateways"][gateway_node_id] = channel_index

    @property
    def gateways(self) -> Mapping[int, int]:
        return MappingProxyType(self._attr_extra_state_attributes.get("gateways", {}))

    def update_from(self, entity: MeshtasticChannelNotify) -> None:
        if entity._global_channel_id != self._global_channel_id:
            return

        for gateway_node_id, channel_index in entity.gateways.items():
            self.add_gateway(gateway_node_id, channel_index)
