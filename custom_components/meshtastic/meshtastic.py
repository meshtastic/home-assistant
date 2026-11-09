# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""
The `meshtastic` entity platform: the gateway, its channels and its direct messages.

`EntityComponent.async_setup_entry` loads this module, so the entities are added through a platform bound to
the config entry and HA attaches them and their devices to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.logbook import DOMAIN as LOGBOOK_DOMAIN
from homeassistant.util import slugify

from .const import DOMAIN
from .entity import GatewayChannelEntity, GatewayDirectMessageEntity, GatewayEntity
from .field_metadata import without_sensitive_channel, without_sensitive_local_config, without_sensitive_module_config

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import MeshtasticConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    client = config_entry.runtime_data.client
    gateway_node = await client.async_get_own_node()
    local_config = await client.async_get_node_local_config()
    module_config = await client.async_get_node_module_config()

    gateway_node_entity = GatewayEntity(
        config_entry_id=config_entry.entry_id,
        node=gateway_node["num"],
        long_name=gateway_node["user"]["longName"],
        short_name=gateway_node["user"]["shortName"],
        # state attributes reach the recorder, backups and every API client, so secrets never go in
        local_config=without_sensitive_local_config(local_config),
        module_config=without_sensitive_module_config(module_config),
    )
    has_logbook = LOGBOOK_DOMAIN in hass.config.all_components
    gateway_direct_message = GatewayDirectMessageEntity(
        config_entry_id=config_entry.entry_id,
        gateway_node=gateway_node["num"],
        gateway_entity=gateway_node_entity,
        has_logbook=has_logbook,
    )
    channel_entities = [
        GatewayChannelEntity(
            config_entry_id=config_entry.entry_id,
            gateway_node=gateway_node["num"],
            gateway_entity=gateway_node_entity,
            index=channel["index"],
            name=channel["settings"]["name"],
            primary=channel["role"] == "PRIMARY",
            secondary=channel["role"] == "SECONDARY",
            settings=channel["settings"],
            has_logbook=has_logbook,
        )
        for channel in map(without_sensitive_channel, await client.async_get_channels())
        if channel["role"] != "DISABLED"
    ]

    entities = [gateway_node_entity, gateway_direct_message, *channel_entities]
    for entity in entities:
        # with a device attached HA prefixes the device name to a base object id; a preset entity id is used verbatim
        entity.entity_id = f"{DOMAIN}.{slugify(entity.suggested_object_id)}"
    async_add_entities(entities)
