# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.components.logbook.const import (
    LOGBOOK_ENTRY_CONTEXT_ID,
    LOGBOOK_ENTRY_DOMAIN,
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID, CONF_DEVICE_ID, CONF_ENTITY_ID, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback

from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE,
    EVENT_MESHTASTIC_API_TEXT_MESSAGE_OUT,
)
from .const import (
    DOMAIN,
    EVENT_MESHTASTIC_DOMAIN_EVENT,
    EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE,
    EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE,
    EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI,
    MeshtasticDomainEventData,
    MeshtasticDomainEventType,
    MeshtasticDomainMessageLogEventData,
)
from .entity import (
    GatewayChannelEntity,
    GatewayDirectMessageEntity,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .data import MeshtasticConfigEntry


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    @callback
    def async_describe_message_event(event: Event) -> dict[str, str]:
        entity_name = entity.name if (entity := entity_registry.entities.get(event.data[ATTR_ENTITY_ID])) else None

        if from_name := event.data.get(EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME):
            device_name = from_name
        elif device := device_registry.devices.get(event.data[ATTR_DEVICE_ID]):
            device_name = device.name
        else:
            device_name = "?"

        message = event.data.get(EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE)
        icon = (
            "mdi:message-lock"
            if event.data.get(EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI, False)
            else "mdi:message"
        )

        return {
            LOGBOOK_ENTRY_DOMAIN: DOMAIN,
            LOGBOOK_ENTRY_NAME: entity_name,
            LOGBOOK_ENTRY_MESSAGE: f"«{message}» by {device_name}",
            LOGBOOK_ENTRY_ENTITY_ID: event.data[ATTR_ENTITY_ID],
            LOGBOOK_ENTRY_CONTEXT_ID: event.context_id,
            LOGBOOK_ENTRY_ICON: icon,
        }

    async_describe_event(DOMAIN, EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG, async_describe_message_event)


async def async_setup_message_logger(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> CALLBACK_TYPE:  # noqa: PLR0915
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    def _publish_message_log_event(  # noqa: PLR0913
        hass: HomeAssistant,
        entry: MeshtasticConfigEntry,
        from_device_id: str,
        from_node_id: str,
        to_channel_entity_id: str,
        to_dm_entity_id: str,
        message: str,
    ) -> None:
        if (node_info := entry.runtime_data.client.get_node_info(int(from_node_id))) is not None:
            from_name = f"{node_info.long_name} ({node_info.user_id})"
        else:
            from_name = f"!{from_node_id:08x}"
        message_log_event_data: MeshtasticDomainMessageLogEventData = {
            CONF_ENTITY_ID: to_dm_entity_id or to_channel_entity_id,
            CONF_DEVICE_ID: from_device_id,
            EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_FROM_NAME: from_name,
            EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_PKI: bool(to_dm_entity_id),
            EVENT_MESHTASTIC_MESSAGE_LOG_EVENT_DATA_ATTR_MESSAGE: message,
        }
        hass.bus.async_fire(event_type=EVENT_MESHTASTIC_DOMAIN_MESSAGE_LOG, event_data=message_log_event_data)

    async def _on_text_message(
        event: Event, *, produce_domain_event: bool = True, produce_log_event: bool = True
    ) -> None:
        event_data = deepcopy(event.data)
        config_entry_id = event_data.pop(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID, None)
        if config_entry_id != entry.entry_id:
            return

        data = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, None)
        if data is None:
            return

        from_node_id = data["from"]
        from_device = device_registry.async_get_device(identifiers={(DOMAIN, str(from_node_id))})

        gateway_node_id = data["gateway"]
        to = data["to"]
        to_device, to_dm_entity_id = extract_device_and_entity_from_node(config_entry_id, gateway_node_id, to)
        to_device, to_channel_entity_id = extract_device_and_entity_from_channel(
            config_entry_id, gateway_node_id, to, to_device
        )
        message = data["message"]

        if produce_domain_event:
            if from_device:
                domain_event_data: MeshtasticDomainEventData = {
                    CONF_DEVICE_ID: from_device.id,
                    CONF_TYPE: MeshtasticDomainEventType.MESSAGE_SENT,
                    EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE: message,
                }
                if to_channel_entity_id:
                    domain_event_data[CONF_ENTITY_ID] = to_channel_entity_id
                if to_dm_entity_id:
                    domain_event_data[CONF_ENTITY_ID] = to_dm_entity_id
                hass.bus.async_fire(event_type=EVENT_MESHTASTIC_DOMAIN_EVENT, event_data=domain_event_data)

            if to_device:
                domain_event_data: MeshtasticDomainEventData = {
                    CONF_DEVICE_ID: to_device.id,
                    CONF_TYPE: MeshtasticDomainEventType.MESSAGE_RECEIVED,
                    EVENT_MESHTASTIC_DOMAIN_EVENT_DATA_ATTR_MESSAGE: message,
                }

                if to_channel_entity_id:
                    domain_event_data[CONF_ENTITY_ID] = to_channel_entity_id
                if to_dm_entity_id:
                    domain_event_data[CONF_ENTITY_ID] = to_dm_entity_id

                hass.bus.async_fire(event_type=EVENT_MESHTASTIC_DOMAIN_EVENT, event_data=domain_event_data)

        if produce_log_event and (to_dm_entity_id or to_channel_entity_id):
            _publish_message_log_event(
                hass,
                entry,
                from_device.id if from_device is not None else None,
                from_node_id,
                to_channel_entity_id,
                to_dm_entity_id,
                message,
            )

    def extract_device_and_entity_from_channel(
        config_entry_id: str, gateway_node_id: int, to: Mapping[str, Any], to_device: dr.DeviceEntry | None
    ) -> tuple[None, dr.DeviceEntry | None]:
        if (to_channel_id := to.get("channel", None)) is not None:
            channel_unique_id = GatewayChannelEntity.build_unique_id(config_entry_id, gateway_node_id, to_channel_id)
            to_channel_entity_id = entity_registry.async_get_entity_id(DOMAIN, DOMAIN, channel_unique_id)
            to_device = device_registry.async_get_device(identifiers={(DOMAIN, str(gateway_node_id))})
        else:
            to_channel_entity_id = None
        return to_device, to_channel_entity_id

    def extract_device_and_entity_from_node(
        config_entry_id: str, gateway_node_id: int, to: Mapping[str, Any]
    ) -> tuple[dr.DeviceEntry | None, str | None]:
        if (to_node_id := to.get("node", None)) is not None:
            to_device = device_registry.async_get_device(identifiers={(DOMAIN, str(to_node_id))})
            dm_unique_id = GatewayDirectMessageEntity.build_unique_id(config_entry_id, gateway_node_id)
            to_dm_entity_id = entity_registry.async_get_entity_id(DOMAIN, DOMAIN, dm_unique_id)
        else:
            to_device = None
            to_dm_entity_id = None
        return to_device, to_dm_entity_id

    async def _on_text_message_in(event: Event) -> None:
        await _on_text_message(event)

    async def _on_text_message_out(event: Event) -> None:
        await _on_text_message(event, produce_domain_event=False)

    listen_in = hass.bus.async_listen(EVENT_MESHTASTIC_API_TEXT_MESSAGE, _on_text_message_in)
    listen_out = hass.bus.async_listen(EVENT_MESHTASTIC_API_TEXT_MESSAGE_OUT, _on_text_message_out)

    def cancel_listen() -> None:
        listen_in()
        listen_out()

    return cancel_listen
