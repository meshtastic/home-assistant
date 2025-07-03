import asyncio  # noqa: D104
import contextlib
import logging
import random
from abc import abstractmethod
from collections.abc import AsyncIterable, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from types import TracebackType

import google
from google.protobuf.message import Message

from ..packet import Packet  # noqa: TID252
from ..protobuf import mesh_pb2, portnums_pb2  # noqa: TID252
from .errors import (
    ClientApiConnectFailedError,
    ClientApiConnectionError,
    ClientApiConnectionInterruptedError,
    ClientApiDisconnectFailedError,
    ClientApiListenInterruptedError,
    ClientApiNotConnectedError,
)
from .listener import ClientApiConnectionPacketStreamListener

LOGGER = logging.getLogger(__package__)


class ClientApiConnection:
    _CONFIG_ID_MINIMAL = 69420

    def __init__(self) -> None:
        self._packet_stream_listeners: list[ClientApiConnectionPacketStreamListener] = []
        self._on_demand_streaming_task: asyncio.Task | None = None
        self._pending_config_requests: dict[int, asyncio.Event] = {}
        self._processing_packets_consumer_count = 0
        self._processing_packets_consumer_lock = asyncio.Lock()
        self._on_demand_streaming_processing_lock = asyncio.Lock()
        self._on_demand_streaming_processing_stop = asyncio.Event()
        self._current_packet_id: int | None = None
        self._queue_status: mesh_pb2.QueueStatus | None = None
        self._queue_status_update = asyncio.Event()
        self._logger = LOGGER.getChild(self.__class__.__name__)
        self._reconnect_lock = asyncio.Lock()
        self._reconnect_in_progress = asyncio.Event()
        self._reconnect_completed = asyncio.Event()
        self._reconnect_failed = asyncio.Event()
        self._reconnect_status_lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            await self._connect()
        except ClientApiConnectionError:
            raise
        except Exception as e:
            raise ClientApiConnectFailedError from e

    async def disconnect(self) -> None:
        for listener in self._packet_stream_listeners:
            listener.close()

        try:
            await self._disconnect()
        except ClientApiConnectionError:
            raise
        except Exception as e:
            raise ClientApiDisconnectFailedError from e

        await self._stop_on_demand_steaming_task()

    async def _stop_on_demand_steaming_task(self) -> None:
        async with self._on_demand_streaming_processing_lock:
            if self._on_demand_streaming_task is not None:
                self._on_demand_streaming_processing_stop.set()
                task = self._on_demand_streaming_task
                task.cancel()
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                finally:
                    if self._on_demand_streaming_task is not None:
                        self._on_demand_streaming_task = None

    @abstractmethod
    async def _connect(self) -> None:
        pass

    @abstractmethod
    async def _disconnect(self) -> None:
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        pass

    async def __aenter__(self) -> "ClientApiConnection":
        if not self.is_connected:
            await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.disconnect()

    async def listen(self, on_start: Coroutine | None = None) -> AsyncIterable[mesh_pb2.FromRadio]:
        if not self.is_connected:
            if on_start is not None:
                on_start.close()
            raise ClientApiNotConnectedError

        async with self._ensure_processing_packets():
            with ClientApiConnectionPacketStreamListener() as listener:
                self._packet_stream_listeners.append(listener)
                try:
                    if on_start is not None:
                        try:
                            await on_start
                        except Exception as e:
                            raise ClientApiConnectionError from e
                    async for packet in listener:
                        yield packet
                except Exception as e:
                    raise ClientApiListenInterruptedError from e
                finally:
                    with contextlib.suppress(ValueError):
                        self._packet_stream_listeners.remove(listener)

    async def nodes(self) -> AsyncIterable[mesh_pb2.NodeInfo]:
        async for packet in self.listen():
            if packet.HasField("node_info"):
                self._logger.debug("Received node info: %s", self._protobuf_log(packet.node_info))
                yield packet.node_info

    async def _process_packet_stream(self) -> None:
        if self._reconnect_in_progress.is_set():
            self._logger.debug("Reconnect in progress, need to wait")
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_completed.wait()
                self._logger.debug("Reconnect completed")

        if not self.is_connected:
            self._logger.debug("Not connected")
            raise ClientApiNotConnectedError

        try:
            async for packet in self._packet_stream():
                await self._update_queue_status(packet)
                await self._notify_packet_stream_listeners(packet)
                # give listener higher change to process packet before continuing ourselves
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self._logger.debug("Packet stream processing cancelled", exc_info=True)
            raise
        except Exception as e:
            await self._notify_packet_stream_listeners_error(e)
            raise
        finally:
            await self._close_packet_stream_listeners()

    async def _update_queue_status(self, packet: mesh_pb2.FromRadio) -> None:
        if packet.HasField("queueStatus"):
            self._queue_status = packet.queueStatus
            self._queue_status_update.set()
            self._queue_status_update.clear()
            self._logger.debug("New Queue Status: %s", repr(self._queue_status).replace("\n", ""))

    async def _notify_packet_stream_listeners(self, packet: mesh_pb2.FromRadio, *, sequential: bool = False) -> None:
        async def notify(listener: ClientApiConnectionPacketStreamListener, new_packet: mesh_pb2.FromRadio) -> None:
            try:
                await listener.notify(new_packet)
            except:  # noqa: E722
                self._logger.warning("Listener notify failed: %s", listener, exc_info=True)

        if sequential:
            for listener in self._packet_stream_listeners:
                await notify(listener, packet)
        else:
            await asyncio.wait(
                [asyncio.create_task(notify(listener, packet)) for listener in self._packet_stream_listeners]
            )

    async def _close_packet_stream_listeners(self) -> None:
        for listener in self._packet_stream_listeners:
            listener.close()

    async def _notify_packet_stream_listeners_error(self, e: Exception) -> None:
        for listener in self._packet_stream_listeners:
            listener.set_failure(e)

    @abstractmethod
    def _packet_stream(self) -> AsyncIterable[mesh_pb2.FromRadio]:
        pass

    @abstractmethod
    async def _send_packet(self, packet: bytes) -> bool:
        pass

    async def send_heartbeat(self) -> None:
        p = mesh_pb2.ToRadio()
        p.heartbeat.CopyFrom(mesh_pb2.Heartbeat())
        if not await self.send_packet(p):
            msg = "Heartbeat failed"
            raise ClientApiConnectionInterruptedError(msg)

    async def request_config(self, minimal: bool = False) -> bool:  # noqa: FBT001, FBT002
        start_config_packet = mesh_pb2.ToRadio()

        if minimal:
            start_config_packet.want_config_id = self._CONFIG_ID_MINIMAL
        else:
            # not using 0 as config id as it is default for config_complete_id
            start_config_packet.want_config_id = random.randint(1, 0xFFFFFFFF)  # noqa: S311
            if start_config_packet.want_config_id == self._CONFIG_ID_MINIMAL:
                start_config_packet.want_config_id += 1

        async for packet in self.listen(on_start=self.send_packet(start_config_packet)):
            try:
                if (
                    packet.HasField("config_complete_id")
                    and packet.config_complete_id == start_config_packet.want_config_id
                ):
                    return True
            except:  # noqa: E722
                self._logger.warning("Failed to check request config", exc_info=True)
        return False

    def extract_mesh_packet_data(self, from_radio: mesh_pb2.FromRadio) -> mesh_pb2.Data | None:
        if not from_radio.HasField("packet"):
            return None

        packet = from_radio.packet
        if not packet.HasField("decoded"):
            self._logger.debug("Packet could not be decoded: %s", self._protobuf_log(packet))
            return None

        return packet.decoded

    async def send_mesh_packet(  # noqa: PLR0913
        self,
        to_node: int,
        message: google.protobuf.message.Message | bytes,
        port_num: portnums_pb2.PortNum.ValueType = portnums_pb2.PortNum.PRIVATE_APP,
        priority: mesh_pb2.MeshPacket.Priority = mesh_pb2.MeshPacket.Priority.DEFAULT,
        channel_index: int | None = None,
        *,
        from_node: int | None = None,
        ack: bool = False,
        reply_id: int | None = None,
        want_response: bool = False,
        ack_callback: Callable[[Packet[mesh_pb2.Routing]], Awaitable[None]] | None = None,
        response_callback: Callable[[Packet], Awaitable[None]] | None = None,
    ) -> None | Packet:
        mesh_packet = mesh_pb2.MeshPacket()
        if channel_index is not None:
            mesh_packet.channel = channel_index
        mesh_packet.decoded.payload = (
            message.SerializeToString() if (isinstance(message, google.protobuf.message.Message)) else message
        )
        mesh_packet.decoded.portnum = port_num
        mesh_packet.decoded.want_response = want_response
        if reply_id is not None:
            mesh_packet.decoded.reply_id = reply_id
        mesh_packet.id = self._generate_packet_id()
        if from_node is not None:
            mesh_packet.__setattr__("from", from_node)
        mesh_packet.to = to_node
        mesh_packet.priority = priority
        mesh_packet.want_ack = ack

        to_radio = mesh_pb2.ToRadio()
        to_radio.packet.CopyFrom(mesh_packet)

        if not ack and not want_response:
            await self.send_packet(to_radio)
            return None

        return await self._send_await_response(
            to_radio, want_response=want_response, ack_callback=ack_callback, response_callback=response_callback
        )

    async def _send_await_response(
        self,
        to_radio: mesh_pb2.ToRadio,
        *,
        want_response: bool = False,
        ack_callback: Callable[[Packet[mesh_pb2.Routing]], Awaitable[None]] | None = None,
        response_callback: Callable[[Packet], Awaitable[None]] | None = None,
    ) -> None | Packet:
        ack_packet = None
        response_packet = None
        async for from_radio in self.listen(on_start=self.send_packet(to_radio)):
            packet = Packet(from_radio)
            if packet.data is None:
                continue

            if packet.data.request_id != to_radio.packet.id:
                continue

            if packet.data.portnum == portnums_pb2.PortNum.ROUTING_APP:
                self._logger.debug("Received routing ACK: %s", packet.app_payload)
                ack_packet = packet
                if ack_callback is not None:
                    try:
                        await ack_callback(ack_packet)
                    except:  # noqa: E722
                        self._logger.debug("Ack callback failed", exc_info=True)
            else:
                self._logger.debug("Received response")
                response_packet = packet
                if response_callback is not None:
                    try:
                        await response_callback(response_packet)
                    except:  # noqa: E722
                        self._logger.debug("Response callback failed", exc_info=True)

            if want_response and response_packet is None:
                continue

            if want_response:
                if response_packet is None:
                    continue
                return response_packet
            return ack_packet

        return None

    def _generate_packet_id(self) -> int:
        if self._current_packet_id is None:
            self._current_packet_id = random.randint(0, 0xFFFFFFFF)  # noqa: S311

        next_packet_id = (self._current_packet_id + 1) & 0xFFFFFFFF
        next_packet_id = next_packet_id & 0x3FF
        random_part = (random.randint(0, 0x3FFFFF) << 10) & 0xFFFFFFFF  # noqa: S311
        self._current_packet_id = next_packet_id | random_part
        return self._current_packet_id

    async def send_packet(self, to_radio: mesh_pb2.ToRadio) -> bool:
        packet: bytes = to_radio.SerializeToString()

        if not to_radio.HasField("packet"):
            return await self._send_packet(packet)
        self._logger.debug(
            "Sending packet (id=0x%x fr=0x%x to=0x%x)",
            to_radio.packet.id,
            getattr(to_radio.packet, "from"),
            to_radio.packet.to,
        )
        if self._queue_status is None:
            self._logger.debug("No queue state available, sending directly")
            return await self._send_packet(packet)
        while self._queue_status.free == 0:
            self._logger.debug("Queue is full, waiting")
            queue_status_update_task = asyncio.create_task(self._queue_status_update.wait())
            sleep_task = asyncio.create_task(asyncio.sleep(1.0))
            await asyncio.wait([queue_status_update_task, sleep_task], return_when=asyncio.FIRST_COMPLETED)

        self._queue_status.free -= 1
        return await self._send_packet(packet)

    @asynccontextmanager
    async def _ensure_processing_packets(self) -> None:
        if not self.is_connected:
            raise ClientApiNotConnectedError

        if self._reconnect_in_progress.is_set():
            self._logger.debug("_ensure_processing_packets but reconnect in progress")
            await self._reconnect_completed.wait()

        async with self._processing_packets_consumer_lock:
            # start consuming
            if self._processing_packets_consumer_count == 0:
                await self._start_on_demand_stream_processor()

            self._processing_packets_consumer_count += 1
        try:
            yield
        finally:
            async with self._processing_packets_consumer_lock:
                self._processing_packets_consumer_count -= 1

                # stop consuming when no consumer left
                if self._processing_packets_consumer_count == 0:
                    await self._stop_on_demand_steaming_task()

    async def _start_on_demand_stream_processor(self) -> None:
        async with self._on_demand_streaming_processing_lock:
            self._on_demand_streaming_processing_stop.clear()

            async def _on_demand_stream_processor() -> None:
                self._logger.debug("On demand packet stream processor started")
                try:
                    while not self._on_demand_streaming_processing_stop.is_set():
                        try:
                            await self._process_packet_stream()
                        except (ClientApiConnectionError, asyncio.exceptions.IncompleteReadError):
                            self._logger.debug("On demand processing failed with connection error", exc_info=True)
                            if self._reconnect_in_progress.is_set():
                                self._logger.debug("On demand processing, waiting for reconnect")
                                await self._reconnect_completed.wait()
                                self._logger.debug("On demand processing, reconnect done")
                            else:
                                with contextlib.suppress(asyncio.CancelledError):
                                    await asyncio.sleep(5)
                        except asyncio.CancelledError:
                            raise
                        except:  # noqa: E722
                            self._logger.warning("On demand processing failed with unexpected error", exc_info=True)
                            await asyncio.sleep(10)

                except asyncio.CancelledError:
                    self._logger.debug("On demand processor cancelled", exc_info=True)
                except Exception:  # noqa: BLE001
                    self._logger.debug("On demand processor failed", exc_info=True)
                finally:
                    self._on_demand_streaming_task = None
                    self._logger.debug("On demand processor ended")

            self._on_demand_streaming_task = asyncio.create_task(
                _on_demand_stream_processor(), name="on_demand_stream_processor"
            )

    async def reconnect(self, *, force: bool = False) -> bool:
        """
        Reconnect connection.

        If reconnect is already in progress, waits until pending reconnect is completed.
        Returns true if new reconnect has been performed
        """
        async with self._reconnect_status_lock:
            reconnect_was_in_progress = self._reconnect_in_progress.is_set()

            if not force and self.is_connected:
                self._logger.debug("Aborting reconnect, already connected")
                return False

            if not reconnect_was_in_progress:
                self._reconnect_in_progress.set()
                self._reconnect_completed.clear()

        if reconnect_was_in_progress:
            self._logger.debug("Reconnect in progress, waiting")
            complete_task = asyncio.create_task(self._reconnect_completed.wait())
            failed_task = asyncio.create_task(self._reconnect_failed.wait())
            done, pending = await asyncio.wait([complete_task, failed_task], return_when=asyncio.FIRST_COMPLETED)
            if done == {failed_task}:
                msg = "Already ongoing reconnect failed"
                raise ClientApiConnectionError(msg)
            return False

        self._logger.debug("Reconnect started")
        try:
            async with self._reconnect_lock:
                if self.is_connected or force:
                    self._logger.debug("Reconnect: disconnecting first")
                    with contextlib.suppress(Exception):
                        await self._disconnect()
                    self._logger.debug("Reconnect: disconnecting complete")
                await self._connect()

                async with self._reconnect_status_lock:
                    self._reconnect_completed.set()
                    self._reconnect_in_progress.clear()
                    self._reconnect_failed.clear()
                self._logger.debug("Reconnect completed")

        except:
            self._logger.debug("Failed to reconnect", exc_info=True)
            async with self._reconnect_status_lock:
                self._reconnect_completed.clear()
                self._reconnect_in_progress.clear()
                self._reconnect_failed.set()
            raise
        return True

    async def send_disconnect(self) -> None:
        m = mesh_pb2.ToRadio()
        m.disconnect = True
        await self.send_packet(m)

    @staticmethod
    def _protobuf_log(message: Message) -> str:
        return repr(message).replace("\n", "")
