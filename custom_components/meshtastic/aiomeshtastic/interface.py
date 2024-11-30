import asyncio
import contextlib
import datetime
import enum
import functools
import itertools
import random
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType, TracebackType
from typing import (
    Any,
    Self,
)

import google
from google.protobuf.message import Message

from meshtastic.protobuf import (
    admin_pb2,
    channel_pb2,
    config_pb2,
    connection_status_pb2,
    localonly_pb2,
    mesh_pb2,
    module_config_pb2,
    portnums_pb2,
    telemetry_pb2,
)
from meshtastic.protobuf.mesh_pb2 import MeshPacket

from .connection import (
    ClientApiConnection,
    ClientApiConnectionPacketStreamListener,
    ClientApiNotConnectedError,
)
from .const import LOGGER, UNDEFINED
from .errors import MeshInterfaceRequestError, MeshRoutingError, MeshtasticError
from .packet import Packet


class MeshInterfaceError(MeshtasticError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass
class MeshNode:
    id: int
    user_id: str
    short_name: str
    long_name: str

    @staticmethod
    def stub_node(node_id: int) -> "MeshNode":
        user_id = f"!{node_id:08x}"
        return MeshNode(
            id=node_id, user_id=user_id, short_name=f"{user_id[-4:]}", long_name=f"Meshtastic {user_id[-4:]}"
        )


@dataclass
class MeshChannel:
    index: int
    name: str


def process_while_running(f):  # noqa: ANN001, ANN201
    @functools.wraps(f)
    async def wrapper(self: "MeshInterface") -> None:
        while self.is_running:
            try:
                self._logger.debug("Processing while running starting, function: %s", f.__name__)
                await f(self)
            except asyncio.CancelledError:
                self._logger.debug("Processing while running, aborting, function: %s", f.__name__)
                raise
            except:  # noqa: E722
                self._logger.debug(
                    "Processing while running failed, restarting soon, function: %s", f.__name__, exc_info=True
                )
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    self._logger.debug("Processing cancelled, aborting, function: %s", f.__name__)
                    break
                self._logger.debug("Resuming processing while running, function: %s", f.__name__)
        self._logger.debug("Processing while running ended, function: %s", f.__name__)

    return wrapper


class TelemetryType(enum.StrEnum):
    DEVICE_METRICS = "device_metrics"
    ENVIRONMENT_METRICS = "environment_metrics"
    POWER_METRICS = "power_metrics"
    AIR_QUALITY_METRICS = "air_quality_metrics"


class MeshInterface:
    PKC_CHANNEL_INDEX = 8

    BROADCAST_NUM: int = 0xFFFFFFFF
    BROADCAST_ADDR = "^all"

    def __init__(  # noqa: PLR0913
        self,
        connection: "ClientApiConnection",
        *,
        debug_out: bool = False,
        no_proto: bool = False,
        no_nodes: bool = False,
        heartbeat_interval: datetime.timedelta | None = None,
        acknowledgement_timeout: datetime.timedelta | None = None,
        response_timeout: datetime.timedelta | None = None,
    ) -> None:
        self._logger = LOGGER.getChild(self.__class__.__name__)
        self._connection = connection
        self._is_running = asyncio.Event()

        self.debug_out = debug_out
        self.no_nodes = no_nodes
        self.no_proto = no_proto

        self._connected_node_config_lock = asyncio.Lock()
        self._connected_node_info: mesh_pb2.MyNodeInfo | None = None
        self._connected_node_metadata: mesh_pb2.DeviceMetadata | None = None
        self._connected_node_channels: list[channel_pb2.Channel] | None = None
        self._connected_node_queue_status: mesh_pb2.QueueStatus | None = None
        self._connected_node_local_config = localonly_pb2.LocalConfig()
        self._connected_node_module_config = localonly_pb2.LocalModuleConfig()

        self._connected_node_ready = asyncio.Event()

        self._heartbeat_interval_s = 600 if heartbeat_interval is None else heartbeat_interval.total_seconds()

        self._ack_timeout = 30.0 if acknowledgement_timeout is None else acknowledgement_timeout.total_seconds()
        self._response_timeout = 60.0 if response_timeout is None else response_timeout.total_seconds()

        self._node_database: dict[int, dict[str, Any]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()

        self._processing_tasks: set[asyncio.Task] = set()
        self._background_tasks: set[asyncio.Task] = set()

        self._reconnect_lock = asyncio.Lock()
        self._listen_lock = asyncio.Lock()

        self._packet_stream_listeners: list[ClientApiConnectionPacketStreamListener] = []
        self._app_listeners: dict[portnums_pb2.PortNum, list[Callable[[MeshNode, Packet], Awaitable[None]]]] = (
            defaultdict(list)
        )

    def add_packet_app_listener(
        self,
        packet_type: portnums_pb2.PortNum,
        callback: Callable[[MeshNode, Message | Packet | dict], Awaitable[None]],
        *,
        as_dict: bool = False,
        as_packet: bool = False,
    ) -> Callable[[], None]:
        if as_dict and as_packet:
            msg = "as_dict and as_packet are mutually exclusive"
            raise ValueError(msg)

        async def wrapper(node: MeshNode, source: Packet) -> None:
            if as_dict:
                await callback(node, google.protobuf.json_format.MessageToDict(source.app_payload))
            elif as_packet:
                await callback(node, source)
            else:
                await callback(node, source.app_payload)

        self._app_listeners[packet_type].append(wrapper)
        return lambda: self._app_listeners[packet_type].remove(wrapper)

    def nodes(self) -> Mapping[int, Mapping[str, Any]]:
        return MappingProxyType(self._node_database)

    def connected_node(self) -> Mapping[str, Any] | None:
        if not self._connected_node_ready.is_set():
            return None

        node = self._node_database.get(self._connected_node_info.my_node_num)
        if node is None:
            return node

        return MappingProxyType(node)

    def connected_node_metadata(self) -> mesh_pb2.DeviceMetadata | None:
        if not self._connected_node_ready.is_set():
            return None

        return self._connected_node_metadata

    def connected_node_channels(self) -> list[channel_pb2.Channel] | None:
        if not self._connected_node_ready.is_set():
            return None

        return self._connected_node_channels

    def connected_node_local_config(self) -> localonly_pb2.LocalConfig | None:
        if not self._connected_node_ready.is_set():
            return None
        return self._connected_node_local_config

    def connected_node_module_config(self) -> localonly_pb2.LocalModuleConfig | None:
        if not self._connected_node_ready.is_set():
            return None
        return self._connected_node_module_config

    def find_node(
        self,
        node_id: int | None = None,
        user_id: str | None = None,
        short_name: str | None = None,
        long_name: str | None = None,
    ) -> MeshNode | None:
        if node_id is None and user_id is None and short_name is None and long_name is None:
            msg = "Most provide node_id, user_id, short_name or long_name"
            raise ValueError(msg)

        if node_id is not None:
            node_info = self._node_database.get(node_id)
        else:

            def matches(node_info: Mapping[str, Any]) -> bool:
                if user_id is not None and node_info["user"]["id"] == user_id:
                    return True
                if short_name is not None and node_info["user"]["shortName"] == short_name:
                    return True

                return bool(long_name is not None and node_info["user"]["longName"] == long_name)

            node_info = next((node_info for node_info in self._node_database.values() if matches(node_info)), None)

        if node_info is None:
            return None

        return MeshNode(
            id=node_info["num"],
            user_id=node_info["user"]["id"],
            short_name=node_info["user"]["shortName"],
            long_name=node_info["user"]["longName"],
        )

    def find_channel(self, index: int | None = None, name: str | None = None) -> MeshChannel | None:
        if index is None and name is None:
            msg = "Most provide index or name"
            raise ValueError(msg)

        if self._connected_node_info is None:
            return None

        if index is not None:
            if len(self._connected_node_channels) < index:
                return None

            channel = self._connected_node_channels[index]
            if channel.role == channel_pb2.Channel.Role.DISABLED:
                return None

            if name is not None and channel.settings.name != name:
                return None
        else:
            channel = next(
                (
                    channel_info
                    for channel_info in self._connected_node_channels
                    if channel_info.settings.name == name and channel_info.role != channel_pb2.Channel.Role.DISABLED
                ),
                None,
            )
            if channel is None:
                return None

        return MeshChannel(index=index, name=channel.settings.name)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        await self._connection.connect()
        self._is_running.set()

        self._processing_tasks.clear()
        self._processing_tasks.add(asyncio.create_task(self._heartbeat_loop(), name="heartbeat"))
        self._processing_tasks.add(
            asyncio.create_task(self._process_from_radio_packets_loop(), name="process_from_radio_packets")
        )

        async def get_config() -> None:
            await self._start_config()

        self._add_background_task(get_config(), name="get_config")

    async def stop(self) -> None:
        if not self._is_running.is_set():
            return

        self._is_running.clear()
        self._connected_node_ready.clear()

        await self._close_packet_streams()
        await self._cancel_processing_tasks()
        await self._cancel_background_tasks()

        with contextlib.suppress(Exception):
            await self._connection.send_disconnect()
        await self._connection.disconnect()

    async def _close_packet_streams(self) -> None:
        if not self._packet_stream_listeners:
            return

        for listener in self._packet_stream_listeners:
            listener.close()
        self._packet_stream_listeners.clear()

    async def _cancel_processing_tasks(self) -> None:
        if not self._processing_tasks:
            return

        await asyncio.wait(
            [
                asyncio.create_task(self._cancel_task(t), name=f"cancel-{t.get_name()}")
                for t in itertools.chain(self._processing_tasks, self._background_tasks)
            ]
        )
        self._processing_tasks.clear()

    async def _cancel_background_tasks(self) -> None:
        if not self._background_tasks:
            return

        await asyncio.wait(
            [asyncio.create_task(self._cancel_task(t), name=f"cancel-{t.get_name()}") for t in self._background_tasks]
        )

    async def _cancel_task(self, t: asyncio.Task) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            t.cancel()
            await t

    @property
    def is_running(self) -> bool:
        return self._is_running.is_set()

    async def connected_node_ready(self) -> bool:
        await self._connected_node_ready.wait()
        return self._connected_node_ready.is_set()

    @process_while_running
    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_s)
            try:
                self._logger.debug("Sending heartbeat")
                if self._connected_node_ready.is_set():
                    # perform request with an actual response from node, self._connection.send_heartbeat() does not
                    # reliably work to detect broken connections as there is no response
                    await self.request_connection_status()
                else:
                    # use as fallback when we did not suceed to connect and we don't
                    await self._connection.send_heartbeat()
            except Exception:  # noqa: BLE001
                self._logger.info("Heartbeat failed, reconnecting", exc_info=True)
                await self._reconnect_while_running(force=True)
            else:
                self._logger.debug("Heartbeat success")

    async def _process_connected_node_packets(self, packet: mesh_pb2.FromRadio) -> None:
        if packet.HasField("rebooted") and packet.rebooted:

            async def reconnect() -> None:
                await self.stop()
                await self.start()

            self._add_background_task(reconnect(), name="reconnect")
        elif packet.HasField("my_info"):
            self._connected_node_info = packet.my_info
        elif packet.HasField("metadata"):
            self._connected_node_metadata = packet.metadata
        elif packet.HasField("channel"):
            self._connected_node_channels.append(packet.channel)
        elif packet.HasField("queueStatus"):
            self._connected_node_queue_status = packet.queueStatus
        elif packet.HasField("log_record"):
            pass
        elif packet.HasField("config"):
            self._process_connected_node_config(packet.config)
        elif packet.HasField("moduleConfig"):
            self._process_connected_node_module_config(packet.moduleConfig)

    def _process_connected_node_config(self, config: config_pb2.Config) -> None:
        if config.HasField("device"):
            self._connected_node_local_config.device.CopyFrom(config.device)
        if config.HasField("position"):
            self._connected_node_local_config.position.CopyFrom(config.position)
        if config.HasField("power"):
            self._connected_node_local_config.power.CopyFrom(config.power)
        if config.HasField("network"):
            self._connected_node_local_config.network.CopyFrom(config.network)
        if config.HasField("display"):
            self._connected_node_local_config.display.CopyFrom(config.display)
        if config.HasField("lora"):
            self._connected_node_local_config.lora.CopyFrom(config.lora)
        if config.HasField("bluetooth"):
            self._connected_node_local_config.bluetooth.CopyFrom(config.bluetooth)
        if config.HasField("security"):
            self._connected_node_local_config.security.CopyFrom(config.security)

    def _process_connected_node_module_config(self, module_config: module_config_pb2.ModuleConfig) -> None:  # noqa: PLR0912
        if module_config.HasField("mqtt"):
            self._connected_node_module_config.mqtt.CopyFrom(module_config.mqtt)
        if module_config.HasField("serial"):
            self._connected_node_module_config.serial.CopyFrom(module_config.serial)
        if module_config.HasField("external_notification"):
            self._connected_node_module_config.external_notification.CopyFrom(module_config.external_notification)
        if module_config.HasField("store_forward"):
            self._connected_node_module_config.store_forward.CopyFrom(module_config.store_forward)
        if module_config.HasField("range_test"):
            self._connected_node_module_config.range_test.CopyFrom(module_config.range_test)
        if module_config.HasField("telemetry"):
            self._connected_node_module_config.telemetry.CopyFrom(module_config.telemetry)
        if module_config.HasField("canned_message"):
            self._connected_node_module_config.canned_message.CopyFrom(module_config.canned_message)
        if module_config.HasField("audio"):
            self._connected_node_module_config.audio.CopyFrom(module_config.audio)
        if module_config.HasField("remote_hardware"):
            self._connected_node_module_config.remote_hardware.CopyFrom(module_config.remote_hardware)
        if module_config.HasField("neighbor_info"):
            self._connected_node_module_config.neighbor_info.CopyFrom(module_config.neighbor_info)
        if module_config.HasField("detection_sensor"):
            self._connected_node_module_config.detection_sensor.CopyFrom(module_config.detection_sensor)
        if module_config.HasField("ambient_lighting"):
            self._connected_node_module_config.ambient_lighting.CopyFrom(module_config.ambient_lighting)
        if module_config.HasField("paxcounter"):
            self._connected_node_module_config.paxcounter.CopyFrom(module_config.paxcounter)

    @process_while_running
    async def _process_from_radio_packets_loop(self) -> None:
        async for from_radio in self._listen_while_running():
            await self._process_connected_node_packets(from_radio)
            await self._process_node_info(from_radio)

            for listener in self._packet_stream_listeners:
                await listener.notify(from_radio)

            await self._process_packet_for_app_listener(from_radio)

    async def _process_packet_for_app_listener(self, from_radio: mesh_pb2.FromRadio) -> None:
        packet = Packet(from_radio)
        if packet.mesh_packet is None:
            return

        if packet.data is None:
            self._logger.debug("Packet could not be decoded: %s", repr(packet.mesh_packet).replace("\n", ""))
            return

        if packet.port_num is None:
            return

        if packet.app_payload is None:
            self._logger.debug("Packet has no payload: %s", repr(packet.data).replace("\n", ""))
            return

        node_id = int(packet.from_id)
        node = self.find_node(node_id) or MeshNode.stub_node(node_id)

        if packet.port_num == portnums_pb2.PortNum.TELEMETRY_APP:
            telemetry = packet.app_payload
            telemetry_info = google.protobuf.json_format.MessageToDict(telemetry)
            if node_id in self._node_database:
                self._node_database[node_id].update(telemetry_info)

        for listener in self._app_listeners[packet.port_num]:
            self._add_background_task(listener(node, packet), name=f"app-listener-{packet.port_num}")

    async def _process_node_info(self, packet: mesh_pb2.FromRadio) -> None:
        if not packet.HasField("node_info"):
            return

        node_info = packet.node_info
        try:
            node_info_dict = google.protobuf.json_format.MessageToDict(node_info)
            node = self._get_or_create_node(node_info.num)
            node.update(node_info_dict)

            if "position" in node:
                node["position"] = self._fixup_position(node["position"])
        except:  # noqa: E722
            self._logger.warning("Failed to process node", exc_info=True)

    def _fixup_position(self, position: dict) -> dict:
        if "latitudeI" in position:
            position["latitude"] = float(position["latitudeI"] * Decimal("1e-7"))
        if "longitudeI" in position:
            position["longitude"] = float(position["longitudeI"] * Decimal("1e-7"))
        return position

    def _get_or_create_node(self, node_num: int) -> MutableMapping[str, Any]:
        if node_num == self.BROADCAST_NUM:
            msg = "Broadcast Num is no valid node num"
            raise ValueError(msg)

        if node_num in self._node_database:
            return self._node_database[node_num]
        presumptive_id = f"!{node_num:08x}"
        n = {
            "num": node_num,
            "user": {
                "id": presumptive_id,
                "longName": f"Meshtastic {presumptive_id[-4:]}",
                "shortName": f"{presumptive_id[-4:]}",
                "hwModel": "UNSET",
            },
        }  # Create a minimal node db entry
        self._node_database[node_num] = n
        return n

    async def node_info_stream(self) -> AsyncIterator[mesh_pb2.NodeInfo]:
        async for packet in self._listen():
            if packet.HasField("node_info"):
                yield packet.node_info

    async def packet_stream(self) -> AsyncIterator[mesh_pb2.MeshPacket]:
        async for packet in self._listen():
            if packet.HasField("packet"):
                yield packet.packet

    async def from_radio_stream(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        async for packet in self._listen():
            yield packet

    async def _listen(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        with ClientApiConnectionPacketStreamListener() as listener:
            self._packet_stream_listeners.append(listener)
            try:
                async for packet in listener.packets():
                    yield packet
            finally:
                self._packet_stream_listeners.remove(listener)

    async def _listen_while_running(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        # only allow one listener that performs reconnects
        async with self._listen_lock:
            if not self.is_running:
                raise ClientApiNotConnectedError

            while self.is_running:
                try:
                    async for packet in self._connection.listen():
                        yield packet
                        if not self.is_running:
                            return
                except Exception:  # noqa: BLE001
                    await self._reconnect_while_running()

    async def _reconnect_while_running(self, *, force: bool = False) -> None:
        force_reconnect = force
        while self.is_running:
            try:
                async with self._reconnect_lock:
                    self._logger.debug("Starting to reconnect")
                    try:
                        did_reconnect = await asyncio.wait_for(
                            self._connection.reconnect(force=force_reconnect), timeout=30
                        )
                    except TimeoutError:
                        self._logger.debug("Reconnect connection did timeout, retrying")
                        continue
                    else:
                        self._logger.debug("Reconnect connection succeeded, requesting config")

                    if did_reconnect:
                        try:
                            await asyncio.wait_for(self._connection.request_config(minimal=self.no_nodes), timeout=60)
                            if not self._connected_node_ready.is_set():
                                self._logger.debug("Completed first request config as part of reconnect")
                                self._connected_node_ready.set()
                        except TimeoutError:
                            self._logger.debug("Reconnect requesting config did timeout, forcing next reconnect")
                            force_reconnect = True
                            continue
                        else:
                            force_reconnect = False
                            self._logger.debug("Reconnect finished")
                    return
            except asyncio.CancelledError:
                self._logger.debug("Reconnecting cancelled", exc_info=True)
                break
            except:  # noqa: E722
                reconnect_delay = float(random.randint(5, 30))  # noqa: S311
                self._logger.debug("Reconnecting failed, retrying in %.0f seconds", reconnect_delay)
                await asyncio.sleep(reconnect_delay)

    async def _start_config(self) -> None:
        async with self._connected_node_config_lock:
            self._connected_node_ready.clear()
            self._connected_node_info: mesh_pb2.MyNodeInfo | None = None
            self._connected_node_metadata: mesh_pb2.DeviceMetadata | None = None
            self._connected_node_channels: list[channel_pb2.Channel] | None = []
            self._connected_node_queue_status: mesh_pb2.QueueStatus | None = None
            self._node_database = {}

            await self._connection.request_config(minimal=self.no_nodes)
            self._connected_node_ready.set()

    def _add_background_task(self, coro: Awaitable[None], name: str | None = None) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def send_time(self, node: int | None = None) -> None:
        local_now = datetime.datetime.now().astimezone()
        tz_offset = local_now.tzinfo.utcoffset(local_now)
        time_secs = int(local_now.timestamp() + tz_offset.total_seconds())
        admin_message = admin_pb2.AdminMessage()
        admin_message.set_time_only = time_secs

        await self.send_admin_message_await_response(node=node, message=admin_message, expect_response=False)

    async def request_connection_status(self, node: int | None = None) -> connection_status_pb2.DeviceConnectionStatus:
        admin_message = admin_pb2.AdminMessage()
        admin_message.get_device_connection_status_request = True

        response = await self.send_admin_message_await_response(node=node, message=admin_message, expect_response=True)
        return response.app_payload.get_device_connection_status_response

    async def request_lora_config(self, node: int | None = None) -> config_pb2.Config.LoRaConfig:
        admin_message = admin_pb2.AdminMessage()
        admin_message.get_config_request = admin_pb2.AdminMessage.ConfigType.LORA_CONFIG

        response = await self.send_admin_message_await_response(node=node, message=admin_message)

        return response.app_payload.get_config_response.lora

    async def write_lora_config(self, lora: config_pb2.Config.LoRaConfig, node: int | None = None) -> None:
        admin_message = admin_pb2.AdminMessage()
        config = config_pb2.Config()
        config.lora.CopyFrom(lora)
        admin_message.set_config.CopyFrom(config)

        await self.send_admin_message_await_response(node=node, message=admin_message, expect_response=False)

    async def request_telemetry(
        self,
        node: int | MeshNode,
        telemetry_type: TelemetryType,
        timeout: float = UNDEFINED,  # noqa: ASYNC109
    ) -> telemetry_pb2.Telemetry:
        telemetry = telemetry_pb2.Telemetry()

        if telemetry_type == TelemetryType.DEVICE_METRICS:
            telemetry.device_metrics.CopyFrom(telemetry_pb2.DeviceMetrics())
        elif telemetry_type == TelemetryType.ENVIRONMENT_METRICS:
            telemetry.environment_metrics.CopyFrom(telemetry_pb2.EnvironmentMetrics())
        elif telemetry_type == TelemetryType.AIR_QUALITY_METRICS:
            telemetry.air_quality_metrics.CopyFrom(telemetry_pb2.AirQualityMetrics())
        elif telemetry_type == TelemetryType.POWER_METRICS:
            telemetry.power_metrics.CopyFrom(telemetry_pb2.PowerMetrics())
        else:
            msg = "Invalid telemetry type"
            raise ValueError(msg)

        response = await self._send_message_await_response(
            node=node.id if isinstance(node, MeshNode) else node,
            message=telemetry,
            port_num=portnums_pb2.PortNum.TELEMETRY_APP,
            want_response=True,
            timeout=timeout,
        )

        return response.app_payload

    async def _send_message_await_response(  # noqa: PLR0913
        self,
        node: int,
        message: google.protobuf.message.Message | bytes,
        port_num: portnums_pb2.PortNum.ValueType,
        channel_index: int | None = None,
        from_node: int | None = None,
        *,
        want_response: bool = False,
        timeout: float = UNDEFINED,  # noqa: ASYNC109
    ) -> Packet:
        actual_timeout = (
            timeout if timeout is not UNDEFINED else (self._response_timeout if want_response else self._ack_timeout)
        )
        ack_received = asyncio.Event()
        error_queue = asyncio.Queue()

        async def on_ack(packet: Packet[mesh_pb2.Routing]) -> None:
            ack_received.set()
            if packet.app_payload.error_reason != mesh_pb2.Routing.Error.NONE:
                error_queue.put_nowait(MeshRoutingError(packet.app_payload.error_reason))

        abort_on_error_task = asyncio.create_task(error_queue.get())
        send_packet_task = asyncio.create_task(
            self._connection.send_mesh_packet(
                to_node=node,
                message=message,
                port_num=port_num,
                priority=mesh_pb2.MeshPacket.Priority.RELIABLE,
                ack=True,
                want_response=want_response,
                channel_index=channel_index,
                from_node=from_node,
                ack_callback=on_ack,
            )
        )

        done, pending = await asyncio.wait(
            [abort_on_error_task, send_packet_task], timeout=actual_timeout, return_when=asyncio.FIRST_COMPLETED
        )

        try:
            if done == {abort_on_error_task}:
                raise next(iter(done)).result()
            if done == {send_packet_task}:
                return next(iter(done)).result()
            if not ack_received.is_set():
                msg = f"No acknowledgement received within {actual_timeout} seconds"
                raise MeshInterfaceRequestError(msg)
            msg = f"No response received within {actual_timeout} seconds"
            raise MeshInterfaceRequestError(msg)
        finally:
            for p in pending:
                p.cancel()

    async def send_admin_message(
        self, node: int, message: admin_pb2.AdminMessage, *, ack: bool = True
    ) -> None | tuple[mesh_pb2.Data, mesh_pb2.FromRadio]:
        return await self._connection.send_mesh_packet(
            channel_index=self._get_admin_channel_index(node=node),
            to_node=node,
            message=message,
            port_num=portnums_pb2.PortNum.ADMIN_APP,
            priority=MeshPacket.Priority.RELIABLE,
            want_response=ack,
            ack=ack,
        )

    async def send_admin_message_await_response(
        self,
        node: int | None,
        message: admin_pb2.AdminMessage,
        *,
        timeout: float = UNDEFINED,  # noqa: ASYNC109
        expect_response: bool = True,
    ) -> Packet[admin_pb2.AdminMessage]:
        if node is None:
            node = self._connected_node_info.my_node_num
        return await self._send_message_await_response(
            node=node,
            message=message,
            port_num=portnums_pb2.PortNum.ADMIN_APP,
            channel_index=self._get_admin_channel_index(node=node),
            want_response=expect_response,
            timeout=timeout,
        )

    def _get_admin_channel_index(self, node: int) -> int:
        if node == self._connected_node_info.my_node_num:
            return 0

        if self._node_database.get(self._connected_node_info.my_node_num, {}).get(
            "hasPKC", False
        ) and self._node_database.get(node, {}).get("hasPKC", False):
            return self.PKC_CHANNEL_INDEX

        for c in self._connected_node_channels or []:
            if c.settings and c.settings.name.lower() == "admin":
                return c.index
        return 0

    async def send_text_message(
        self,
        text: str,
        destination: MeshNode | MeshChannel | int | str = None,
        *,
        want_ack: bool = False,
        channel_index: int | None = None,
    ) -> None:
        if isinstance(destination, MeshNode):
            to_node = destination.id
            channel_index = None
        elif isinstance(destination, MeshChannel):
            to_node = self.BROADCAST_NUM
            channel_index = destination.index
        elif isinstance(destination, str):
            if destination == self.BROADCAST_ADDR:
                to_node = self.BROADCAST_NUM
            else:
                if not destination.startswith("!"):
                    msg = "Not a valid user id"
                    raise ValueError(msg)
                to_node = int(destination[1:], 16)
        elif isinstance(destination, int):
            to_node = destination
        else:
            to_node = self.BROADCAST_NUM

        if channel_index is not None:
            if len(self._connected_node_channels) < channel_index:
                msg = "Unavailable channel index"
                raise ValueError(msg)

            channel = self._connected_node_channels[channel_index]
            if channel.role == channel_pb2.Channel.Role.DISABLED:
                msg = f"Channel {channel.settings.name} ({channel.settings.id}) is disabled"
                raise ValueError(msg)

        return await self._connection.send_mesh_packet(
            channel_index=channel_index,
            to_node=to_node,
            message=text.encode("utf-8"),
            port_num=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            priority=MeshPacket.Priority.RELIABLE if want_ack else MeshPacket.Priority.DEFAULT,
            want_response=False,
            ack=want_ack,
        )
