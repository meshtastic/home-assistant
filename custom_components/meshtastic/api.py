"""Sample API Client."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self

import google
from google.protobuf.json_format import MessageToDict
from homeassistant.exceptions import IntegrationError

from meshtastic import BROADCAST_ADDR
from meshtastic.protobuf import portnums_pb2

from .aiomeshtastic import (
    BluetoothConnection as AioBluetoothConnection,
)
from .aiomeshtastic import (
    MeshInterface as AioMeshInterface,
)
from .aiomeshtastic import (
    SerialConnection as AioSerialConnection,
)
from .aiomeshtastic import (
    TcpConnection as AioTcpConnection,
)
from .const import (
    CONF_CONNECTION_BLUETOOTH_ADDRESS,
    CONF_CONNECTION_SERIAL_PORT,
    CONF_CONNECTION_TCP_HOST,
    CONF_CONNECTION_TCP_PORT,
    CONF_CONNECTION_TYPE,
    DOMAIN,
    LOGGER,
    ConnectionType,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Mapping, MutableMapping
    from types import MappingProxyType, TracebackType

    from google.protobuf.message import Message
    from homeassistant.core import HomeAssistant

    from .aiomeshtastic.interface import MeshNode
    from .aiomeshtastic.packet import Packet

_LOGGER = LOGGER.getChild(__name__)


EVENT_MESHTASTIC_API_BASE = f"{DOMAIN}_api"
EVENT_MESHTASTIC_API_NODE_UPDATED = EVENT_MESHTASTIC_API_BASE + "_node_updated"
EVENT_MESHTASTIC_API_TELEMETRY = EVENT_MESHTASTIC_API_BASE + "_telemetry"
EVENT_MESHTASTIC_API_PACKET = EVENT_MESHTASTIC_API_BASE + "_packet"
EVENT_MESHTASTIC_API_TEXT_MESSAGE = EVENT_MESHTASTIC_API_BASE + "_text_message"

ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_EVENT_MESHTASTIC_API_NODE = "node"
ATTR_EVENT_MESHTASTIC_API_DATA = "data"
ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE = "telemetry_type"


class EventMeshtasticApiTelemetryType(StrEnum):
    DEVICE_METRICS = "device_metrics"
    LOCAL_STATS = "local_stats"
    ENVIRONMENT_METRICS = "environment_metrics"
    POWER_METRICS = "power_metrics"


class MeshtasticApiClientError(IntegrationError):
    """Exception to indicate a general API error."""


class MeshtasticApiClientCommunicationError(
    MeshtasticApiClientError,
):
    """Exception to indicate a communication error."""


class MeshtasticApiClient:
    def __init__(
        self,
        data: MappingProxyType[str, Any],
        hass: HomeAssistant,
        config_entry_id: str | None,
        *,
        no_nodes: bool = False,
    ) -> None:
        self._logger = LOGGER.getChild(self.__class__.__name__)
        self._connected = asyncio.Event()
        self._hass = hass
        self._config_entry_id = config_entry_id

        connection_type = data[CONF_CONNECTION_TYPE]

        if connection_type == ConnectionType.TCP.value:
            connection = AioTcpConnection(host=data[CONF_CONNECTION_TCP_HOST], port=data[CONF_CONNECTION_TCP_PORT])
        elif connection_type == ConnectionType.BLUETOOTH.value:
            connection = AioBluetoothConnection(ble_address=data[CONF_CONNECTION_BLUETOOTH_ADDRESS])
        elif connection_type == ConnectionType.SERIAL.value:
            connection = AioSerialConnection(device=data[CONF_CONNECTION_SERIAL_PORT])
        else:
            msg = f"Unsupported connection type {connection_type}"
            raise ValueError(msg)

        self._interface = AioMeshInterface(
            connection=connection, no_nodes=no_nodes, heartbeat_interval=timedelta(minutes=5)
        )
        self._packet_processor: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()

        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TEXT_MESSAGE_APP, callback=self._on_text_message, as_packet=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TELEMETRY_APP, callback=self._on_telemetry, as_dict=True
        )

    async def connect(self) -> None:
        try:
            await self._interface.start()
            await asyncio.wait_for(self._interface.connected_node_ready(), timeout=30)

            self._packet_processor = asyncio.create_task(self._process_meshtastic_packet())

            async def send_time() -> None:
                await asyncio.sleep(1)
                try:
                    await self._interface.send_time()
                except:  # noqa: E722
                    self._logger.debug("Send time failed")

            self._add_background_task(send_time())
        except Exception as e:
            raise MeshtasticApiClientCommunicationError from e

    async def disconnect(self) -> None:
        try:
            self._packet_processor.cancel()
            await self._interface.stop()
        except Exception as e:
            raise MeshtasticApiClientCommunicationError from e

    async def async_get_channels(self) -> list[Mapping[str, Any]]:
        await self._interface.connected_node_ready()
        return [self._message_to_dict(c) for c in self._interface.connected_node_channels()]

    async def async_get_node_local_config(self) -> dict:
        await self._interface.connected_node_ready()
        return self._message_to_dict(self._interface.connected_node_local_config())

    async def async_get_node_module_config(self) -> dict:
        await self._interface.connected_node_ready()
        return self._message_to_dict(self._interface.connected_node_module_config())

    async def async_get_own_node(self) -> Mapping[str, Any]:
        await self._interface.connected_node_ready()
        return self.get_own_node()

    def get_own_node(self) -> Mapping[str, Any]:
        return self._interface.connected_node() or {}

    async def async_get_all_nodes(self) -> Mapping[int, Mapping[str, Any]]:
        await self._interface.connected_node_ready()
        return self._interface.nodes()

    async def send_text(
        self,
        text: str,
        destination_id: int | str = BROADCAST_ADDR,
        *,
        want_ack: bool = False,
        channel_index: int | None = None,
    ) -> bool:
        try:
            await asyncio.wait_for(
                self._interface.send_text_message(
                    text,
                    destination=destination_id,
                    want_ack=want_ack,
                    channel_index=channel_index,
                ),
                timeout=30,
            )
        except TimeoutError:
            return False
        else:
            return True

    @property
    def metadata(self) -> Mapping[str, Any]:
        metadata = self._interface.connected_node_metadata()
        return MessageToDict(metadata) if metadata is not None else {}

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.disconnect()

    def _build_event_data(self, node_id: int, data: Mapping[str, Any]) -> MutableMapping[str, Any]:
        return {"config_entry_id": self._config_entry_id, "node": node_id, "data": data}

    async def _on_text_message(self, node: MeshNode, packet: Packet) -> None:
        event_data = self._build_event_data(
            node.id,
            {
                "from": getattr(packet.mesh_packet, "from"),
                "to": packet.mesh_packet.to,
                "message": packet.app_payload,
            },
        )

        event_data["message_id"] = packet.mesh_packet.id
        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TEXT_MESSAGE, event_data)

    async def _on_telemetry(self, node: MeshNode, telemetry: dict[str, Any]) -> None:
        device_metrics = telemetry.get("deviceMetrics")
        local_stats = telemetry.get("localStats")
        environment_metrics = telemetry.get("environmentMetrics")
        power_metrics = telemetry.get("powerMetrics")

        node_info = {"name": node.long_name}
        if device_metrics:
            event_data = self._build_event_data(node.id, device_metrics)
            event_data["node_info"] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.DEVICE_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if local_stats:
            event_data = self._build_event_data(node.id, local_stats)
            event_data["node_info"] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.LOCAL_STATS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if environment_metrics:
            event_data = self._build_event_data(node.id, environment_metrics)
            event_data["node_info"] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.ENVIRONMENT_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if power_metrics:
            event_data = self._build_event_data(node.id, power_metrics)
            event_data["node_info"] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.POWER_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

    async def _process_meshtastic_packet(self) -> None:
        async for packet in self._interface.packet_stream():
            try:
                packet_clone = google.protobuf.json_format.MessageToDict(packet)
                node_id = packet_clone["from"]
                self._hass.bus.async_fire(EVENT_MESHTASTIC_API_PACKET, self._build_event_data(node_id, packet_clone))
            except:  # noqa: E722
                self._logger.warning("Failed to process packet %s", packet, exc_info=True)

    def _add_background_task(self, coro: Coroutine[Any, Any, None], name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _message_to_dict(self, message: Message) -> Mapping[str, Any]:
        try:
            return MessageToDict(message, always_print_fields_with_no_presence=True)
        except TypeError:
            # older protobuf version
            return MessageToDict(message, including_default_value_fields=True)
