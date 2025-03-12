from collections import defaultdict
from collections.abc import Awaitable, Callable

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import (
    config_validation as cv,
)
from homeassistant.helpers import (
    device_registry as dr,
)
from homeassistant.helpers import (
    entity_registry as er,
)
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
)

from .aiomeshtastic import MeshInterface
from .aiomeshtastic.interface import TelemetryType
from .api import MeshtasticApiClient
from .const import (
    ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL,
    ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE,
    ATTR_SERVICE_DATA_ACK,
    ATTR_SERVICE_DATA_CHANNEL,
    ATTR_SERVICE_DATA_FROM,
    ATTR_SERVICE_DATA_TO,
    ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE,
    ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE,
    ATTR_SERVICE_SEND_TEXT_DATA_TEXT,
    DOMAIN,
    LOGGER,
    SERVICE_BROADCAST_CHANNEL_MESSAGE,
    SERVICE_REQUEST_POSITION,
    SERVICE_REQUEST_TELEMETRY,
    SERVICE_REQUEST_TRACEROUTE,
    SERVICE_SEND_DIRECT_MESSAGE,
    SERVICE_SEND_TEXT,
    STATE_ATTRIBUTE_CHANNEL_INDEX,
    STATE_ATTRIBUTE_CHANNEL_NODE,
)
from .data import DATA_COMPONENT, MeshtasticConfigEntry

SERVICE_SEND_TEXT_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERVICE_SEND_TEXT_DATA_TEXT): cv.string,
        vol.Optional(ATTR_SERVICE_DATA_TO): cv.string,
        vol.Optional(ATTR_SERVICE_DATA_FROM): cv.string,
        vol.Optional(ATTR_SERVICE_DATA_CHANNEL): cv.string,
        vol.Required(ATTR_SERVICE_DATA_ACK, default=False): cv.boolean,
    }
)

SERVICE_SEND_DIRECT_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERVICE_DATA_TO): cv.string,
        vol.Required(ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE): cv.string,
        vol.Required(ATTR_SERVICE_DATA_ACK, default=True): cv.boolean,
    }
)

SERVICE_BROADCAST_CHANNEL_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL): cv.string,
        vol.Required(ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE): cv.string,
        vol.Required(ATTR_SERVICE_DATA_ACK, default=True): cv.boolean,
    }
)

SERVICE_BASE_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_SERVICE_DATA_FROM): cv.string,
        vol.Required(ATTR_SERVICE_DATA_TO): cv.string,
    }
)

SERVICE_REQUEST_TELEMETRY_SCHEMA = SERVICE_BASE_REQUEST_SCHEMA.extend(
    {
        vol.Required(ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE): SelectSelector(
            SelectSelectorConfig(options=[str(v) for v in TelemetryType], translation_key="telemetry_type_selector")
        ),
    }
)

SERVICE_REQUEST_POSITION_SCHEMA = SERVICE_BASE_REQUEST_SCHEMA.extend({})
SERVICE_REQUEST_TRACEROUTE_SCHEMA = SERVICE_BASE_REQUEST_SCHEMA.extend({})

_SERVICE_CANT_HANDLE_RESPONSE = object()
_service_handlers: dict[str, dict[str, Callable[[ServiceCall], Awaitable[ServiceResponse]]]] = defaultdict(dict)

SUPPORTED_SERVICES = {
    SERVICE_SEND_TEXT: SupportsResponse.OPTIONAL,
    SERVICE_SEND_DIRECT_MESSAGE: SupportsResponse.NONE,
    SERVICE_BROADCAST_CHANNEL_MESSAGE: SupportsResponse.NONE,
    SERVICE_REQUEST_TELEMETRY: SupportsResponse.OPTIONAL,
    SERVICE_REQUEST_POSITION: SupportsResponse.OPTIONAL,
    SERVICE_REQUEST_TRACEROUTE: SupportsResponse.OPTIONAL,
}

SERVICE_TO_SCHEMA = {
    SERVICE_SEND_TEXT: SERVICE_SEND_TEXT_SCHEMA,
    SERVICE_SEND_DIRECT_MESSAGE: SERVICE_SEND_DIRECT_MESSAGE_SCHEMA,
    SERVICE_BROADCAST_CHANNEL_MESSAGE: SERVICE_BROADCAST_CHANNEL_MESSAGE_SCHEMA,
    SERVICE_REQUEST_TELEMETRY: SERVICE_REQUEST_TELEMETRY_SCHEMA,
    SERVICE_REQUEST_POSITION: SERVICE_REQUEST_POSITION_SCHEMA,
    SERVICE_REQUEST_TRACEROUTE: SERVICE_REQUEST_TRACEROUTE_SCHEMA,
}


async def async_setup_services(hass: HomeAssistant) -> None:
    # handler that forwards service call to appropriate handler from config entry
    async def handle_service_call(call: ServiceCall) -> ServiceResponse:
        for handler in [handlers.get(call.service, None) for handlers in _service_handlers.values()]:
            res = await handler(call)
            if res != _SERVICE_CANT_HANDLE_RESPONSE:
                return res

        msg = "No gateway could handle the request"
        raise ServiceValidationError(msg)

    services = hass.services.async_services_for_domain(DOMAIN)

    for service, supports_response in SUPPORTED_SERVICES.items():
        if service not in services:
            hass.services.async_register(
                DOMAIN,
                service,
                handle_service_call,
                schema=SERVICE_TO_SCHEMA[service],
                supports_response=supports_response,
            )


async def async_remove_services(hass: HomeAssistant) -> None:
    active_services = hass.services.async_services_for_domain(DOMAIN)
    for service in SUPPORTED_SERVICES:
        if service in active_services:
            hass.services.async_remove(DOMAIN, service)


async def async_register_gateway(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> None:
    # ensure services are registered (when unloading last entry and setup new one, async_setup will not be called
    await async_setup_services(hass)

    client = entry.runtime_data.client

    # register gateway specific handlers for service
    await _setup_service_send_text_handler(hass, entry, client)
    await _setup_service_send_direct_message_handler(hass, entry, client)
    await _setup_service_broadcast_channel_message_handler(hass, entry, client)
    await _setup_service_request_telemetry_handler(hass, entry, client)
    await _setup_service_request_position_handler(hass, entry, client)
    await _setup_service_request_traceroute_handler(hass, entry, client)


async def async_unregister_gateway(hass: HomeAssistant, entry: MeshtasticConfigEntry) -> None:
    if entry.entry_id in _service_handlers:
        del _service_handlers[entry.entry_id]

    if not _service_handlers:
        await async_remove_services(hass)


async def _build_default_handler(  # noqa: PLR0915
    hass: HomeAssistant,
    client: MeshtasticApiClient,
    implementation: Callable[[ServiceCall, int, int | None], Awaitable[ServiceResponse]],
) -> Callable[[ServiceCall], Awaitable[ServiceResponse | object]]:
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    gateway_node = await client.async_get_own_node()

    def _convert_device_id_to_node_id(device_id: str) -> int:
        device = device_registry.async_get(device_id)
        if device is None:
            msg = f"No device found with id {device_id}"
            raise ServiceValidationError(msg)

        return next((int(i[1]) for i in device.identifiers if i[0] == DOMAIN), None)

    def _convert_entity_to_channel_index(entity_id: str) -> int:
        entity = entity_registry.async_get(entity_id)
        if entity is None:
            msg = f"No entity found with id {entity_id}"
            raise ServiceValidationError(msg)

        channel_entity = hass.data[DATA_COMPONENT].get_entity(entity.entity_id)
        if channel_entity is None:
            msg = f"No entity found with id {entity.entity_id}"
            raise ServiceValidationError(msg)

        return channel_entity.extra_state_attributes[STATE_ATTRIBUTE_CHANNEL_INDEX]

    async def handle_service_call(call: ServiceCall) -> ServiceResponse | object:  # noqa: PLR0912
        if ATTR_SERVICE_DATA_FROM in call.data:
            from_id = call.data[ATTR_SERVICE_DATA_FROM]
            if from_id.startswith("!"):
                if gateway_node["user"]["id"] != from_id:
                    return _SERVICE_CANT_HANDLE_RESPONSE
            elif from_id.isnumeric():
                if int(from_id) != gateway_node["num"]:
                    return _SERVICE_CANT_HANDLE_RESPONSE
            elif from_id.isalnum():
                node_id = _convert_device_id_to_node_id(from_id)
                if node_id != gateway_node["num"]:
                    return _SERVICE_CANT_HANDLE_RESPONSE
        try:
            if ATTR_SERVICE_DATA_TO in call.data:
                to = call.data[ATTR_SERVICE_DATA_TO]
                if to.startswith("!"):
                    pass
                elif to.isnumeric():
                    to = int(to)
                elif to.isalnum():
                    to = int(_convert_device_id_to_node_id(to))
            else:
                to = MeshInterface.BROADCAST_ADDR

            if ATTR_SERVICE_DATA_CHANNEL in call.data:
                channel = call.data[ATTR_SERVICE_DATA_CHANNEL]
                if isinstance(channel, int):
                    channel_index = channel
                elif channel.isnumeric():
                    channel_index = int(channel)
                else:
                    channel_index = _convert_entity_to_channel_index(channel)
            else:
                channel_index = None

            response = await implementation(call, to, channel_index)

            if not call.return_response:
                return None

            service_response = {"sent": {"from": gateway_node["num"], "to": to}}
            if response:
                service_response["data"] = response
            return service_response  # noqa: TRY300
        except HomeAssistantError:
            raise
        except Exception as e:
            LOGGER.warning("Error handling service call", exc_info=True)
            msg = "Unhandled exception while handling service call"
            raise ServiceValidationError(msg) from e

    return handle_service_call


async def _setup_service_send_direct_message_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    device_registry = dr.async_get(hass)
    gateway_node = await client.async_get_own_node()

    async def handle_service_call(call: ServiceCall) -> ServiceResponse | object:
        device_id = call.data[ATTR_SERVICE_DATA_TO]
        device_entry = device_registry.async_get(device_id)
        if device_entry is None:
            msg = f"No device found with id {device_id}"
            raise ServiceValidationError(msg)

        to_node_id = next((int(i[1]) for i in device_entry.identifiers if i[0] == DOMAIN), None)
        if to_node_id == gateway_node["num"]:
            msg = "Can't send direct message to oneself"
            raise ServiceValidationError(msg)

        if device_entry.via_device_id:
            gateway_device_entry = device_registry.async_get(device_entry.via_device_id)
            gateway_node_id = next((int(i[1]) for i in gateway_device_entry.identifiers if i[0] == DOMAIN), None)
            if gateway_node_id != gateway_node["num"]:
                return _SERVICE_CANT_HANDLE_RESPONSE

        text = call.data[ATTR_SERVICE_SEND_DIRECT_MESSAGE_DATA_MESSAGE]
        await client.send_text(text=text, destination_id=to_node_id, want_ack=call.data[ATTR_SERVICE_DATA_ACK])
        return None

    _service_handlers[entry.entry_id][SERVICE_SEND_DIRECT_MESSAGE] = handle_service_call


async def _setup_service_broadcast_channel_message_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    entity_registry = er.async_get(hass)
    gateway_node = await client.async_get_own_node()

    async def handle_service_call(call: ServiceCall) -> ServiceResponse | object:
        channel = call.data[ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_CHANNEL]
        entity_entry = entity_registry.async_get(channel)
        if entity_entry is None:
            msg = f"No entity found with id {channel}"
            raise ServiceValidationError(msg)

        channel_entity = hass.data[DATA_COMPONENT].get_entity(entity_entry.entity_id)
        if channel_entity is None:
            msg = f"No entity found with id {entity_entry.entity_id}"
            raise ServiceValidationError(msg)

        gateway_node_id = channel_entity.extra_state_attributes[STATE_ATTRIBUTE_CHANNEL_NODE]
        if gateway_node_id != gateway_node["num"]:
            return _SERVICE_CANT_HANDLE_RESPONSE

        text = call.data[ATTR_SERVICE_BROADCAST_CHANNEL_MESSAGE_DATA_MESSAGE]
        channel_index = channel_entity.extra_state_attributes[STATE_ATTRIBUTE_CHANNEL_INDEX]

        await client.send_text(text=text, channel_index=channel_index, want_ack=call.data[ATTR_SERVICE_DATA_ACK])
        return None

    _service_handlers[entry.entry_id][SERVICE_BROADCAST_CHANNEL_MESSAGE] = handle_service_call


async def _setup_service_request_telemetry_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    async def handler(call: ServiceCall, to: int, _: int | None) -> ServiceResponse:
        metric_type = next(t for t in TelemetryType if t.value == call.data[ATTR_SERVICE_REQUEST_TELEMETRY_DATA_TYPE])
        return await entry.runtime_data.client.request_telemetry(to, metric_type)

    _service_handlers[entry.entry_id][SERVICE_REQUEST_TELEMETRY] = await _build_default_handler(hass, client, handler)


async def _setup_service_request_position_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    async def handler(call: ServiceCall, to: int, channel_index: int | None) -> ServiceResponse:  # noqa: ARG001
        return await entry.runtime_data.client.request_position(to)

    _service_handlers[entry.entry_id][SERVICE_REQUEST_POSITION] = await _build_default_handler(hass, client, handler)


async def _setup_service_request_traceroute_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    async def handler(call: ServiceCall, to: int, channel_index: int | None) -> ServiceResponse:  # noqa: ARG001
        return await entry.runtime_data.client.request_traceroute(to)

    _service_handlers[entry.entry_id][SERVICE_REQUEST_TRACEROUTE] = await _build_default_handler(hass, client, handler)


async def _setup_service_send_text_handler(
    hass: HomeAssistant, entry: MeshtasticConfigEntry, client: MeshtasticApiClient
) -> None:
    async def handler(call: ServiceCall, to: int, channel_index: int | None) -> None:
        await client.send_text(
            text=call.data[ATTR_SERVICE_SEND_TEXT_DATA_TEXT],
            destination_id=to,
            channel_index=channel_index,
            want_ack=call.data[ATTR_SERVICE_DATA_ACK],
        )

    _service_handlers[entry.entry_id][SERVICE_SEND_TEXT] = await _build_default_handler(hass, client, handler)
