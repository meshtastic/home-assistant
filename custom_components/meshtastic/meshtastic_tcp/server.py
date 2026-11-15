# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

import asyncio
from asyncio import StreamReader, StreamWriter
from types import TracebackType
from typing import Any, Self, cast

from google.protobuf import message

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.connection.streaming import StreamingClientTransport
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2
from custom_components.meshtastic.const import LOGGER

_LOGGER = LOGGER.getChild(__name__)

# a client that cannot take a frame within this time is not reading and gets disconnected
_WRITE_TIMEOUT_S = 30.0
# FromRadio messages buffered per client; a full bootstrap of a large mesh is a few hundred
_CLIENT_QUEUE_SIZE = 1024
# wait_closed() can hang on a peer that does not read, or raise on a broken one
_CLOSE_TIMEOUT_S = 5.0


class ClientProxyTransport(StreamingClientTransport):
    def __init__(self, reader: StreamReader, writer: StreamWriter) -> None:
        super().__init__()
        self._reader = reader
        self._writer = writer

    async def read_to_radio_packet(self) -> tuple[bytes, mesh_pb2.ToRadio] | None:
        packet = await self._read_packet_bytes()
        if packet is None:
            return None

        to_radio = mesh_pb2.ToRadio()
        try:
            to_radio.ParseFromString(packet)
        except message.DecodeError:
            self._logger.debug("Error while parsing ToRadio bytes %s", packet, exc_info=True)
        else:
            return packet, to_radio
        return None

    async def write_from_radio_packet(self, from_radio: mesh_pb2.FromRadio) -> None:
        binary = from_radio.SerializeToString()
        await self._write_bytes(StreamingClientTransport.build_frame(binary))

    async def _read_bytes(self, n: int = -1, *, exactly: int | None = None) -> bytes | None:
        if exactly is not None:
            return await self._reader.readexactly(n=exactly)
        return await self._reader.read(n=n)

    async def _write_bytes(self, data: bytes) -> bool:
        self._writer.write(data)
        await asyncio.wait_for(self._writer.drain(), _WRITE_TIMEOUT_S)
        return True

    def _can_read(self) -> bool:
        return not self._reader.at_eof()

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        await self._disconnect()

    async def _connect(self) -> None:
        pass

    async def _disconnect(self) -> None:
        self._writer.close()
        closed = False
        try:
            await asyncio.wait_for(self._writer.wait_closed(), _CLOSE_TIMEOUT_S)
            closed = True
        except Exception:
            self._logger.debug("Graceful close of proxy client failed, aborting", exc_info=True)
        finally:
            if not closed:
                self._writer.transport.abort()

    @property
    def is_connected(self) -> bool:
        return not self._writer.is_closing()


class MeshtasticTcpProxy:
    def __init__(self, interface: MeshInterface, host: str | None = None, port: int | None = None) -> None:
        # per-client live queue -> that client's disconnect event and peer name
        self._clients: dict[asyncio.Queue, tuple[asyncio.Event, str]] = {}
        self._handlers: set[asyncio.Task] = set()
        self._forward_task: asyncio.Task[None] | None = None
        self._interface = interface
        self._should_stop = False
        self._stopping = False
        self._host = host
        self._port = port or 4403
        self._server: asyncio.Server | None = None

        self._packet_queue: asyncio.Queue[Any] = asyncio.Queue()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        self._stopping = False
        if not self._interface.is_running:
            self._should_stop = True
            await self._interface.start()

        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        self._forward_task = asyncio.create_task(self._forward_from_radio(), name="tcp_proxy_read_from_gateway")

    async def stop(self) -> None:
        # a handler that first runs after the snapshot below would otherwise never be cancelled
        self._stopping = True
        if self._server:
            self._server.close()

        tasks = [task for task in (self._forward_task, *self._handlers) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            # each handler's close is itself bounded, so this only guards against the unexpected
            _, pending = await asyncio.wait(tasks, timeout=2 * _CLOSE_TIMEOUT_S)
            if pending:
                _LOGGER.warning("%d tcp proxy tasks did not stop in time", len(pending))

        if self._should_stop:
            await self._interface.stop()
            self._should_stop = False

    async def _handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        client_connection = ClientProxyTransport(reader, writer)
        if self._stopping or self._forward_task is None or self._forward_task.done():
            # without the forwarder this client would never receive anything
            await client_connection.disconnect()
            return

        handler = asyncio.current_task()
        self._handlers.add(cast("asyncio.Task", handler))  # a handler always runs inside a task
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_CLIENT_QUEUE_SIZE)
        disconnect_event = asyncio.Event()
        try:
            peer_name = "{}:{}".format(*writer.transport.get_extra_info("peername", (None, None))[0:2])
            self._clients[queue] = (disconnect_event, peer_name)

            async def forward_to_radio() -> None:
                while True:
                    packet = await client_connection.read_to_radio_packet()
                    if packet is None:
                        continue
                    packet_bytes, to_radio = packet

                    # never forward disconnect: firmware closes the shared session on presence, whatever the value
                    if to_radio.HasField("disconnect"):
                        disconnect_event.set()
                        return

                    _LOGGER.debug(
                        "Forwarding from %s to gateway: %s",
                        peer_name,
                        ClientApiConnection._protobuf_log(to_radio),  # noqa: SLF001
                    )

                    await self._interface._connection.forward(packet_bytes, to_radio)  # noqa: SLF001

            async def forward_from_radio() -> None:
                while client_connection.is_connected:
                    from_radio = await queue.get()
                    _LOGGER.debug(
                        "Forwarding from gateway to %s: %s",
                        peer_name,
                        ClientApiConnection._protobuf_log(from_radio),  # noqa: SLF001
                    )
                    await client_connection.write_from_radio_packet(from_radio)

            task_read = asyncio.create_task(forward_to_radio(), name=f"tcp_proxy_read_{peer_name}")
            task_write = asyncio.create_task(forward_from_radio(), name=f"tcp_proxy_write_{peer_name}")
            task_wait_disconnect = asyncio.create_task(
                disconnect_event.wait(), name=f"tcp_proxy_disconnect_wait_{peer_name}"
            )

            try:
                await asyncio.wait([task_wait_disconnect, task_read, task_write], return_when=asyncio.FIRST_COMPLETED)
            finally:
                tasks = (task_read, task_write, task_wait_disconnect)
                for task in tasks:
                    task.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            if isinstance(results[1], TimeoutError):
                _LOGGER.warning(
                    "Proxy client %s did not take a frame within %s s, disconnecting it", peer_name, _WRITE_TIMEOUT_S
                )
            _LOGGER.debug(
                "Closed proxy connection %s: %s",
                peer_name,
                [r for r in results if isinstance(r, Exception)],
            )
        except Exception:
            _LOGGER.warning("Failed handling proxy connection", exc_info=True)
        finally:
            self._clients.pop(queue, None)
            try:
                await client_connection.disconnect()
            finally:
                self._handlers.discard(handler)

    async def _forward_from_radio(self) -> None:
        try:
            while True:
                async for packet in self._interface.from_radio_stream():
                    # never await here: one client that does not keep up must not hold up the others
                    for queue, (disconnect_event, peer_name) in self._clients.items():
                        try:
                            queue.put_nowait(packet)
                        except asyncio.QueueFull:
                            if not disconnect_event.is_set():
                                _LOGGER.warning("Proxy client %s is not keeping up, disconnecting it", peer_name)
                                disconnect_event.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            # without this task no client receives anything, so stop pretending to serve
            _LOGGER.exception("Forwarding from gateway failed, closing tcp proxy and its clients")
            if self._server:
                self._server.close()
            for disconnect_event, _ in self._clients.values():
                disconnect_event.set()
