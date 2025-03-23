from typing import TYPE_CHECKING, cast

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.device_automation import async_validate_entity_schema
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
)
from homeassistant.helpers.typing import ConfigType, TemplateVarsType, VolDictType

from .aiomeshtastic.interface import TelemetryType
from .const import (
    ATTR_SERVICE_DATA_ACK,
    ATTR_SERVICE_DATA_TO,
    ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE,
    ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE,
    DOMAIN,
    SERVICE_REQUEST_POSITION,
    SERVICE_REQUEST_TELEMETRY,
    SERVICE_SEND_DIRECT_MESSAGE,
)

if TYPE_CHECKING:
    from homeassistant.helpers.template import Template

ACTION_TYPE_SEND_MESSAGE = "send_message"
ACTION_TYPE_REQUEST_TELEMETRY = "request_telemetry"
ACTION_TYPE_REQUEST_POSITION = "request_position"

MESSAGE_ACTION_TYPES = {ACTION_TYPE_SEND_MESSAGE, ACTION_TYPE_REQUEST_TELEMETRY, ACTION_TYPE_REQUEST_POSITION}

ACTION_SEND_MESSAGE_ATTR_MESSAGE = "message"
ACTION_REQUEST_TELEMETRY_ATTR_TELEMETRY_TYPE = "telemetry_type"

TELEMETRY_TYPE_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=[str(v) for v in TelemetryType], translation_key="telemetry_type_selector")
)

MESSAGE_ACTION_BASE_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(MESSAGE_ACTION_TYPES),
    }
)

MESSAGE_ACTION_SCHEMA_SEND_MESSAGE_EXTRA = vol.Schema({vol.Required(ACTION_SEND_MESSAGE_ATTR_MESSAGE): cv.template})

MESSAGE_ACTION_SCHEMA_SEND_MESSAGE = MESSAGE_ACTION_BASE_SCHEMA.extend(MESSAGE_ACTION_SCHEMA_SEND_MESSAGE_EXTRA.schema)

MESSAGE_ACTION_SCHEMA_REQUEST_TELEMETRY_EXTRA = vol.Schema(
    {vol.Required(ACTION_REQUEST_TELEMETRY_ATTR_TELEMETRY_TYPE): TELEMETRY_TYPE_SELECTOR}
)
MESSAGE_ACTION_SCHEMA_REQUEST_TELEMETRY = MESSAGE_ACTION_BASE_SCHEMA.extend(
    MESSAGE_ACTION_SCHEMA_REQUEST_TELEMETRY_EXTRA.schema
)

_ACTION_SCHEMA = vol.Any(
    MESSAGE_ACTION_BASE_SCHEMA, MESSAGE_ACTION_SCHEMA_SEND_MESSAGE, MESSAGE_ACTION_SCHEMA_REQUEST_TELEMETRY
)


async def async_validate_action_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    config = async_validate_entity_schema(hass, config, _ACTION_SCHEMA)

    action_type = config.get(CONF_TYPE)
    if action_type == ACTION_TYPE_SEND_MESSAGE and ACTION_SEND_MESSAGE_ATTR_MESSAGE not in config:
        msg = "Message required"
        raise vol.RequiredFieldInvalid(msg)
    if action_type == ACTION_TYPE_REQUEST_TELEMETRY and ACTION_REQUEST_TELEMETRY_ATTR_TELEMETRY_TYPE not in config:
        msg = "Metric type required"
        raise vol.RequiredFieldInvalid(msg)

    return config


async def async_get_actions(hass: HomeAssistant, device_id: str) -> list[dict]:  # noqa: ARG001
    return [
        {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN, CONF_TYPE: ACTION_TYPE_SEND_MESSAGE},
        {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN, CONF_TYPE: ACTION_TYPE_REQUEST_TELEMETRY},
        {CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN, CONF_TYPE: ACTION_TYPE_REQUEST_POSITION},
    ]


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    if config[CONF_TYPE] == ACTION_TYPE_SEND_MESSAGE:
        if ACTION_SEND_MESSAGE_ATTR_MESSAGE not in config:
            msg = f"Missing {ACTION_SEND_MESSAGE_ATTR_MESSAGE} for action"
            raise ValueError(msg)

        message = cast("Template", config[ACTION_SEND_MESSAGE_ATTR_MESSAGE])
        rendered_message = message.async_render(variables=variables)

        service_data = {
            ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE: rendered_message,
            ATTR_SERVICE_DATA_TO: config[CONF_DEVICE_ID],
            ATTR_SERVICE_DATA_ACK: True,
        }

        await hass.services.async_call(
            DOMAIN, SERVICE_SEND_DIRECT_MESSAGE, service_data, blocking=True, context=context
        )

    elif config[CONF_TYPE] == ACTION_TYPE_REQUEST_TELEMETRY:
        telemetry_type = config.get(ACTION_REQUEST_TELEMETRY_ATTR_TELEMETRY_TYPE)
        if telemetry_type is None:
            msg = "Missing telemetry type for action"
            raise ValueError(msg)

        service_data = {
            ATTR_SERVICE_DATA_TO: config[CONF_DEVICE_ID],
            ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE: telemetry_type,
        }
        await hass.services.async_call(DOMAIN, SERVICE_REQUEST_TELEMETRY, service_data, blocking=True, context=context)
    elif config[CONF_TYPE] == ACTION_TYPE_REQUEST_POSITION:
        service_data = {ATTR_SERVICE_DATA_TO: config[CONF_DEVICE_ID]}
        await hass.services.async_call(DOMAIN, SERVICE_REQUEST_POSITION, service_data, blocking=True, context=context)


async def async_get_action_capabilities(hass: HomeAssistant, config: ConfigType) -> dict[str, vol.Schema]:  # noqa: ARG001
    action_type = config[CONF_TYPE]

    fields: VolDictType = {}

    if action_type == ACTION_TYPE_SEND_MESSAGE:
        fields[vol.Required(ACTION_SEND_MESSAGE_ATTR_MESSAGE)] = cv.string  # template is not rendered
    elif action_type == ACTION_TYPE_REQUEST_TELEMETRY:
        fields[vol.Required(ACTION_REQUEST_TELEMETRY_ATTR_TELEMETRY_TYPE)] = TELEMETRY_TYPE_SELECTOR
    else:
        return {}

    return {"extra_fields": vol.Schema(fields)}
