from typing import cast

import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.device_automation import async_validate_entity_schema
from homeassistant.const import CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.template import Template
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from custom_components.meshtastic import DOMAIN

MESSAGE_ACTION_TYPES = {"send_message"}

MESSAGE_ACTION_SCHEMA = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(MESSAGE_ACTION_TYPES),
        vol.Required("message"): cv.template,
    }
)

_ACTION_SCHEMA = vol.Any(MESSAGE_ACTION_SCHEMA)


async def async_validate_action_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    return async_validate_entity_schema(hass, config, _ACTION_SCHEMA)


async def async_get_actions(hass: HomeAssistant, device_id: str) -> list[dict]:  # noqa: ARG001
    actions = []

    actions.append({CONF_DEVICE_ID: device_id, CONF_DOMAIN: DOMAIN, CONF_TYPE: "send_message"})

    return actions


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    if config[CONF_TYPE] == "send_message":
        message = cast(Template, config["message"])
        rendered_message = message.async_render(variables=variables)

        device_registry = dr.async_get(hass)
        device = device_registry.async_get(config[CONF_DEVICE_ID])

        _, node_id = next((domain, domain_id) for domain, domain_id in device.identifiers if domain == DOMAIN)
        gateway_id = device.via_device_id
        service_data = {"text": rendered_message, "to": str(node_id), "ack": True}

        if gateway_id is not None:
            gateway_device = device_registry.async_get(gateway_id)
            if gateway_device is not None:
                _, gateway_node_id = next(
                    (domain, domain_id) for domain, domain_id in gateway_device.identifiers if domain == DOMAIN
                )
                service_data["from"] = str(gateway_node_id)

        await hass.services.async_call(DOMAIN, "send_text", service_data, blocking=True, context=context)
