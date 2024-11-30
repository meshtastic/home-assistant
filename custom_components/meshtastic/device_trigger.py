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
    CONF_PLATFORM,
    CONF_TYPE,
    Platform,
)
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from custom_components.meshtastic import DOMAIN

TRIGGER_MESSAGE_RECEIVED = "message.received"
TRIGGER_MESSAGE_SENT = "message.sent"

TRIGGER_TYPES = {TRIGGER_MESSAGE_RECEIVED, TRIGGER_MESSAGE_SENT}

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES),
    }
)


async def async_validate_trigger_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    config = TRIGGER_SCHEMA(config)

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(config[CONF_DEVICE_ID])

    if not device:
        msg = f"Trigger invalid, device with ID {config[CONF_DEVICE_ID]} not found"
        raise InvalidDeviceAutomationConfig(msg)

    return config


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list:
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)

    is_gateway = device.via_device_id is None
    triggers = [
        {
            # Required fields of TRIGGER_BASE_SCHEMA
            CONF_PLATFORM: CONF_DEVICE,
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            # Required fields of TRIGGER_SCHEMA
            CONF_TYPE: TRIGGER_MESSAGE_SENT,
        }
    ]

    if is_gateway:
        triggers.append(
            {
                # Required fields of TRIGGER_BASE_SCHEMA
                CONF_PLATFORM: CONF_DEVICE,
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                # Required fields of TRIGGER_SCHEMA
                CONF_TYPE: TRIGGER_MESSAGE_RECEIVED,
            }
        )

    return triggers


async def async_attach_trigger(
    hass: HomeAssistant, config: ConfigType, action: TriggerActionType, trigger_info: TriggerInfo
) -> CALLBACK_TYPE:
    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: Platform.EVENT,
            event_trigger.CONF_EVENT_TYPE: f"{DOMAIN}_event",
            event_trigger.CONF_EVENT_DATA: {
                CONF_DEVICE_ID: config[CONF_DEVICE_ID],
                CONF_TYPE: config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(hass, event_config, action, trigger_info, platform_type=CONF_DEVICE)
