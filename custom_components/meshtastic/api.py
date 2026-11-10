# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import asyncio
import base64
import contextlib
from copy import deepcopy
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self

import google
from google.protobuf.json_format import MessageToDict
from homeassistant.exceptions import IntegrationError

from .aiomeshtastic import (
    BluetoothConnection as AioBluetoothConnection,
)
from .aiomeshtastic import (
    MeshInterface,
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
from .aiomeshtastic.errors import MeshRoutingError, MeshtasticError
from .aiomeshtastic.protobuf import apponly_pb2, channel_pb2, mesh_pb2, portnums_pb2
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
    from collections.abc import Coroutine, Iterable, Mapping, MutableMapping
    from types import MappingProxyType, TracebackType

    from google.protobuf.message import Message
    from homeassistant.core import HomeAssistant

    from .aiomeshtastic.interface import MeshNode, TelemetryType
    from .aiomeshtastic.packet import Packet

_LOGGER = LOGGER.getChild(__name__)


EVENT_MESHTASTIC_API_BASE = f"{DOMAIN}_api"
EVENT_MESHTASTIC_API_NODE_UPDATED = EVENT_MESHTASTIC_API_BASE + "_node_updated"
EVENT_MESHTASTIC_API_TELEMETRY = EVENT_MESHTASTIC_API_BASE + "_telemetry"
EVENT_MESHTASTIC_API_PACKET = EVENT_MESHTASTIC_API_BASE + "_packet"
EVENT_MESHTASTIC_API_TEXT_MESSAGE = EVENT_MESHTASTIC_API_BASE + "_text_message"
EVENT_MESHTASTIC_API_POSITION = EVENT_MESHTASTIC_API_BASE + "_position"

ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_EVENT_MESHTASTIC_API_NODE = "node"
ATTR_EVENT_MESHTASTIC_API_DATA = "data"
ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE = "telemetry_type"
ATTR_EVENT_MESHTASTIC_API_NODE_INFO = "node_info"


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
            ble_address = data[CONF_CONNECTION_BLUETOOTH_ADDRESS]
            ble_device = None
            if hass:
                from homeassistant.components.bluetooth import async_ble_device_from_address

                ble_device = async_ble_device_from_address(hass, ble_address, connectable=True)
            connection = AioBluetoothConnection(ble_address=ble_address, ble_device=ble_device)
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
            packet_type=portnums_pb2.PortNum.NODEINFO_APP, callback=self._on_node_info, as_dict=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TEXT_MESSAGE_APP, callback=self._on_text_message, as_packet=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.TELEMETRY_APP, callback=self._on_telemetry, as_dict=True
        )
        self._interface.add_packet_app_listener(
            packet_type=portnums_pb2.PortNum.POSITION_APP, callback=self._on_position, as_dict=True
        )

    async def connect(self) -> None:
        try:
            await asyncio.wait_for(self._interface.start(), timeout=30)
        except Exception as e:
            raise MeshtasticApiClientCommunicationError from e

        try:
            ready = await asyncio.wait_for(self._interface.connected_node_ready(), timeout=60)
            exception = None
        except Exception as e:  # noqa: BLE001
            ready = False
            exception = e

        if not ready:
            with contextlib.suppress(Exception):
                await self._interface.stop()
            if exception:
                raise MeshtasticApiClientCommunicationError from exception
            raise MeshtasticApiClientCommunicationError

        self._packet_processor = asyncio.create_task(self._process_meshtastic_packet())

        async def send_time() -> None:
            await asyncio.sleep(1)
            try:
                await self._interface.send_time()
                await self._interface.write_timezone_if_needed()
            except:  # noqa: E722
                self._logger.debug("Send time failed", exc_info=True)

        self._add_background_task(send_time())

    async def disconnect(self) -> None:
        try:
            self._packet_processor.cancel()
            await self._interface.stop()
        except Exception as e:
            raise MeshtasticApiClientCommunicationError from e

    async def async_get_channels(self) -> list[Mapping[str, Any]]:
        if not await self._interface.connected_node_ready():
            return []
        return [self._message_to_dict(c) for c in self._interface.connected_node_channels()]

    async def async_get_node_local_config(self) -> dict:
        if not await self._interface.connected_node_ready():
            return {}
        return self._message_to_dict(self._interface.connected_node_local_config())

    async def async_get_node_module_config(self) -> dict:
        if not await self._interface.connected_node_ready():
            return {}
        return self._message_to_dict(self._interface.connected_node_module_config())

    async def async_get_own_node(self) -> Mapping[str, Any]:
        if not await self._interface.connected_node_ready():
            return {}
        return self.get_own_node()

    def get_own_node(self) -> Mapping[str, Any]:
        return self._interface.connected_node() or {}

    def get_node_info(self, node_id: int) -> MeshNode | None:
        return self._interface.find_node(node_id=node_id)

    async def async_get_all_nodes(self) -> Mapping[int, Mapping[str, Any]]:
        await self._interface.connected_node_ready()
        return {node_id: self._transform_node_info(node_info) for node_id, node_info in self._interface.nodes().items()}

    def _transform_node_info(self, node_info: Mapping[str, Any]) -> Mapping[str, Any]:
        transformed = deepcopy(node_info)
        if "position" in transformed:
            self._modify_position(transformed["position"])

        return transformed

    async def send_text(  # noqa: PLR0913
        self,
        text: str,
        destination_id: int | str = MeshInterface.BROADCAST_ADDR,
        *,
        want_ack: bool = False,
        channel_index: int | None = None,
        packet_id: int | None = None,
        reply_id: int | None = None,
    ) -> bool:
        """
        Send a text message without waiting for an acknowledgement.

        ``reply_id`` marks it as the answer to the packet with that id, ``packet_id`` (see ``new_packet_id``)
        fixes the id of the new packet.
        """
        try:
            await asyncio.wait_for(
                self._interface.send_text_message(
                    text,
                    destination=destination_id,
                    want_ack=want_ack,
                    channel_index=channel_index,
                    packet_id=packet_id,
                    reply_id=reply_id,
                ),
                timeout=30,
            )
        except TimeoutError:
            return False
        except Exception as e:
            raise MeshtasticApiClientError from e
        else:
            return True

    async def send_text_with_ack(  # noqa: PLR0913
        self,
        text: str,
        destination_id: int = MeshInterface.BROADCAST_NUM,
        *,
        channel_index: int | None = None,
        ack_timeout: float = 60,
        packet_id: int | None = None,
        reply_id: int | None = None,
    ) -> tuple[int | None, str | None]:
        """
        Send a text message requesting an acknowledgement.

        ``reply_id`` marks it as the answer to the packet with that id, ``packet_id`` (see ``new_packet_id``)
        fixes the id of the new packet.

        Returns (node id that sent the routing ack, routing error name). Both are None if no acknowledgement
        arrived before the timeout. An error name of "NONE" means the packet was acknowledged without error.
        """
        try:
            ack_packet = await asyncio.wait_for(
                self._interface.send_text_message(
                    text,
                    destination=destination_id,
                    want_ack=True,
                    channel_index=channel_index,
                    packet_id=packet_id,
                    reply_id=reply_id,
                ),
                timeout=ack_timeout,
            )
        except TimeoutError:
            return None, None
        except Exception as e:
            raise MeshtasticApiClientError(str(e)) from e

        if ack_packet is None:
            return None, None

        routing = ack_packet.app_payload
        error_name = "NONE"
        if isinstance(routing, mesh_pb2.Routing):
            error_name = mesh_pb2.Routing.Error.Name(routing.error_reason)
        return ack_packet.from_id, error_name

    def new_packet_id(self) -> int:
        return self._interface.generate_packet_id()

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
        return {
            ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID: self._config_entry_id,
            ATTR_EVENT_MESHTASTIC_API_NODE: node_id,
            ATTR_EVENT_MESHTASTIC_API_DATA: data,
        }

    async def _on_node_info(self, node: MeshNode, info: dict[str, Any]) -> None:
        event_data = self._build_event_data(node.id, info)
        position = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, {}).get("position", {})
        if position:
            self._modify_position(position)

        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_NODE_UPDATED, event_data)

    async def _on_text_message(self, node: MeshNode, packet: Packet) -> None:
        if packet.to_id == MeshInterface.BROADCAST_NUM:
            to_channel = packet.channel_index
            to_node = None
        else:
            to_channel = None
            to_node = packet.to_id

        mesh_packet = packet.mesh_packet
        reply_id = packet.data.reply_id if packet.data is not None else 0
        hops = None
        if mesh_packet is not None and mesh_packet.hop_start:
            hops = max(0, mesh_packet.hop_start - mesh_packet.hop_limit)
        event_data = self._build_event_data(
            node.id,
            {
                "from": packet.from_id,
                "to": {"node": to_node, "channel": to_channel},
                "gateway": self.get_own_node()["num"],
                "message": packet.app_payload,
                # how the gateway heard the packet; None when the firmware did not report it
                "rx_snr": mesh_packet.rx_snr if mesh_packet is not None and mesh_packet.rx_snr else None,
                "rx_rssi": mesh_packet.rx_rssi if mesh_packet is not None and mesh_packet.rx_rssi else None,
                "hops": hops,
                # the packet this message answers, when it is a reply
                "reply_id": reply_id or None,
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
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.DEVICE_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if local_stats:
            event_data = self._build_event_data(node.id, local_stats)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.LOCAL_STATS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if environment_metrics:
            event_data = self._build_event_data(node.id, environment_metrics)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.ENVIRONMENT_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

        if power_metrics:
            event_data = self._build_event_data(node.id, power_metrics)
            event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
            event_data[ATTR_EVENT_MESHTASTIC_API_TELEMETRY_TYPE] = EventMeshtasticApiTelemetryType.POWER_METRICS
            self._hass.bus.async_fire(EVENT_MESHTASTIC_API_TELEMETRY, event_data)

    async def _on_position(self, node: MeshNode, position: dict[str, Any]) -> None:
        self._modify_position(position)

        event_data = self._build_event_data(node.id, position)
        node_info = {"name": node.long_name}
        event_data[ATTR_EVENT_MESHTASTIC_API_NODE_INFO] = node_info
        self._hass.bus.async_fire(EVENT_MESHTASTIC_API_POSITION, event_data)

    def _modify_position(self, position: dict[str, Any]) -> None:
        if "latitudeI" in position:
            position["latitude"] = float(position["latitudeI"] * 10**-7)
        if "longitudeI" in position:
            position["longitude"] = float(position["longitudeI"] * 10**-7)

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

    async def request_telemetry(self, node: int, telemetry_type: TelemetryType) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_telemetry(node, telemetry_type=telemetry_type)
            return self._message_to_dict(response)
        except MeshRoutingError as e:
            msg = f"No response for {telemetry_type}: {e}"
            raise MeshtasticApiClientError(msg) from e
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def request_position(self, node: int) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_position(node)
            return self._message_to_dict(response)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def request_traceroute(self, node: int) -> Mapping[str, Any]:
        try:
            response = await self._interface.request_traceroute(node)
            return self._message_to_dict(response)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def async_set_favorite(self, node: int, *, favorite: bool) -> None:
        """Star or unstar a node in the gateway's node database."""
        try:
            await self._interface.set_favorite_node(node, favorite=favorite)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e

    async def async_write_channel(  # noqa: PLR0913
        self,
        index: int,
        *,
        role: str,
        name: str,
        psk: bytes,
        uplink_enabled: bool,
        downlink_enabled: bool,
        position_precision: int,
    ) -> Mapping[str, Any]:
        """Write a channel to the gateway and return the channel as the gateway stored it."""
        channel = channel_pb2.Channel(index=index, role=channel_pb2.Channel.Role.Value(role))
        channel.settings.name = name
        channel.settings.psk = psk
        channel.settings.uplink_enabled = uplink_enabled
        channel.settings.downlink_enabled = downlink_enabled
        channel.settings.module_settings.position_precision = position_precision
        try:
            await self._interface.write_channel(channel)
        except MeshtasticError as e:
            raise MeshtasticApiClientError(str(e)) from e
        try:
            stored = await self._interface.request_channel(index)
        except MeshtasticError:
            self._logger.debug("Could not read channel %s back after writing it", index, exc_info=True)
            stored = channel
        return self._message_to_dict(stored)

    def channel_url(self, indexes: Iterable[int] | None = None, *, add: bool = False) -> str | None:
        """
        Build a meshtastic.org channel URL (the one the apps show as QR code).

        ``indexes`` limits the URL to those channels; ``add`` marks it as "add these channels" instead of
        "replace all channels" for the app that scans it. None if no enabled channel matches.
        """
        channel_set = apponly_pb2.ChannelSet()
        for channel in self._interface.connected_node_channels() or []:
            if channel.role == channel_pb2.Channel.Role.DISABLED:
                continue
            if indexes is not None and channel.index not in indexes:
                continue
            channel_set.settings.append(channel.settings)
        if not channel_set.settings:
            return None
        local_config = self._interface.connected_node_local_config()
        if local_config is not None:
            channel_set.lora_config.CopyFrom(local_config.lora)
        encoded = base64.urlsafe_b64encode(channel_set.SerializeToString()).decode("ascii").rstrip("=")
        return f"https://meshtastic.org/e/{'?add=true' if add else ''}#{encoded}"
