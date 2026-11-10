# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

import voluptuous as vol
from homeassistant.components.device_automation import (
    DEVICE_TRIGGER_BASE_SCHEMA,
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_PLATFORM,
    CONF_TYPE,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, EVENT_MESHTASTIC_DOMAIN_EVENT, MeshtasticDomainEventData, MeshtasticDomainEventType
from .entity import MeshtasticDeviceClass

TRIGGER_MESSAGE_RECEIVED = "message.received"
TRIGGER_DIRECT_MESSAGE_RECEIVED = "direct_message.received"
TRIGGER_CHANNEL_MESSAGE_RECEIVED = "channel_message.received"

TRIGGER_MESSAGE_SENT = "message.sent"
TRIGGER_DIRECT_MESSAGE_SENT = "direct_message.sent"
TRIGGER_CHANNEL_MESSAGE_SENT = "channel_message.sent"


TRIGGER_TYPES = {
    TRIGGER_MESSAGE_RECEIVED,
    TRIGGER_DIRECT_MESSAGE_RECEIVED,
    TRIGGER_CHANNEL_MESSAGE_RECEIVED,
    TRIGGER_MESSAGE_SENT,
    TRIGGER_DIRECT_MESSAGE_SENT,
    TRIGGER_CHANNEL_MESSAGE_SENT,
}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
        vol.Optional(CONF_ENTITY_ID): cv.entity_id_or_uuid,
    }
)


async def async_validate_trigger_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    config = TRIGGER_SCHEMA(config)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(config[CONF_DEVICE_ID])

    if not device:
        raise InvalidDeviceAutomationConfig(
            translation_domain=DOMAIN,
            translation_key="trigger_device_not_found",
            translation_placeholders={"device_id": config[CONF_DEVICE_ID]},
        )

    return config


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list:
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    device = device_registry.async_get(device_id)
    assert device is not None  # noqa: S101 - dereferenced on the next line
    is_gateway = device.via_device_id is None
    triggers = []

    def add_trigger(trigger_type: str, entity_id: str | None = None) -> None:
        trigger = {
            # Required fields of TRIGGER_BASE_SCHEMA
            CONF_PLATFORM: CONF_DEVICE,
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            # Required fields of TRIGGER_SCHEMA
            CONF_TYPE: trigger_type,
        }

        if entity_id is not None:
            trigger[CONF_ENTITY_ID] = entity_id

        triggers.append(trigger)

    # Generic Trigger
    add_trigger(TRIGGER_MESSAGE_SENT)
    if is_gateway:
        add_trigger(TRIGGER_MESSAGE_RECEIVED)

    # Channel & Direct Message specific triggers
    if is_gateway:
        for entry in er.async_entries_for_device(entity_registry, device_id):
            if entry.domain != DOMAIN:
                continue
            if entry.original_device_class == MeshtasticDeviceClass.CHANNEL:
                add_trigger(TRIGGER_CHANNEL_MESSAGE_SENT, entry.entity_id)
                add_trigger(TRIGGER_CHANNEL_MESSAGE_RECEIVED, entry.entity_id)
            elif entry.original_device_class == MeshtasticDeviceClass.MESSAGES:
                add_trigger(TRIGGER_DIRECT_MESSAGE_SENT, entry.entity_id)
                add_trigger(TRIGGER_DIRECT_MESSAGE_RECEIVED, entry.entity_id)

    else:
        for entry in entity_registry.entities.values():
            if entry.domain != DOMAIN:
                continue
            if entry.original_device_class == MeshtasticDeviceClass.CHANNEL:
                add_trigger(TRIGGER_CHANNEL_MESSAGE_SENT, entry.entity_id)
            elif entry.original_device_class == MeshtasticDeviceClass.MESSAGES:
                add_trigger(TRIGGER_DIRECT_MESSAGE_SENT, entry.entity_id)

    return triggers


async def async_attach_trigger(
    hass: HomeAssistant, config: ConfigType, action: TriggerActionType, trigger_info: TriggerInfo
) -> CALLBACK_TYPE:
    event_type = (
        MeshtasticDomainEventType.MESSAGE_SENT
        if config[CONF_TYPE] in (TRIGGER_MESSAGE_SENT, TRIGGER_DIRECT_MESSAGE_SENT, TRIGGER_CHANNEL_MESSAGE_SENT)
        else MeshtasticDomainEventType.MESSAGE_RECEIVED
    )
    event_data: MeshtasticDomainEventData = {
        CONF_DEVICE_ID: config[CONF_DEVICE_ID],
        CONF_TYPE: event_type,
    }

    if CONF_ENTITY_ID in config:
        event_data[CONF_ENTITY_ID] = config[CONF_ENTITY_ID]

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: Platform.EVENT,
            event_trigger.CONF_EVENT_TYPE: EVENT_MESHTASTIC_DOMAIN_EVENT,
            event_trigger.CONF_EVENT_DATA: event_data,
        }
    )
    return await event_trigger.async_attach_trigger(hass, event_config, action, trigger_info, platform_type=CONF_DEVICE)
