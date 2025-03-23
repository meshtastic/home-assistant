import asyncio
from asyncio import StreamReader, StreamWriter
from types import TracebackType
from typing import Self

from google.protobuf import message

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.connection.streaming import StreamingClientTransport
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2
from custom_components.meshtastic.const import LOGGER

_LOGGER = LOGGER.getChild(__name__)


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

    async def write_from_radio_packet(self, from_radio: mesh_pb2.FromRadio) -> None:
        binary = from_radio.SerializeToString()
        await self._write_bytes(StreamingClientTransport.build_frame(binary))

    async def _read_bytes(self, n: int = -1, *, exactly: int | None = None) -> bytes | None:
        if exactly is not None:
            return await self._reader.readexactly(n=exactly)
        return await self._reader.read(n=n)

    async def _write_bytes(self, data: bytes) -> bool:
        self._writer.write(data)
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
        await self._writer.wait_closed()

    @property
    def is_connected(self) -> bool:
        return True


class MeshtasticTcpProxy:
    def __init__(self, interface: MeshInterface, host: str | None = None, port: int | None = None) -> None:
        self._client_queues: set[asyncio.Queue] = set()
        self._clients = {}
        self._forward_task = None
        self._interface = interface
        self._should_stop = False
        self._host = host
        self._port = port or 4403
        self._server: asyncio.Server | None = None

        self._packet_queue = asyncio.Queue()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    async def start(self) -> None:
        if not self._interface.is_running:
            self._should_stop = True
            await self._interface.start()

        self._server = await asyncio.start_server(self._handle_client, self._host, self._port)
        self._forward_task = asyncio.create_task(self._forward_from_radio(), name="tcp_proxy_read_from_gateway")

    async def stop(self) -> None:
        self._forward_task.cancel()

        if self._server:
            self._server.close()

        if self._should_stop:
            await self._interface.stop()
            self._should_stop = False

    async def _handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        queue = asyncio.Queue()
        self._client_queues.add(queue)
        client_connection = ClientProxyTransport(reader, writer)
        disconnect_event = asyncio.Event()
        try:
            peer = writer.transport.get_extra_info("peername", (None, None, None, None))
            peer_name = "{}:{}".format(*peer[0:2])

            async def forward_to_radio() -> None:
                while True:
                    packet = await client_connection.read_to_radio_packet()
                    if packet is None:
                        continue
                    packet_bytes, to_radio = packet

                    # skip disconnect request
                    if to_radio.disconnect:
                        disconnect_event.set()
                        await client_connection.disconnect()
                        continue

                    _LOGGER.debug(
                        "Forwarding from %s to gateway: %s",
                        peer_name,
                        ClientApiConnection._protobuf_log(packet[1]),  # noqa: SLF001
                    )

                    await self._interface._connection._send_packet(packet[0])  # noqa: SLF001

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
                task_read.cancel()
                task_write.cancel()
                task_wait_disconnect.cancel()
            _LOGGER.debug("Closed proxy connection %s", peer_name)
        except:  # noqa: E722
            _LOGGER.warning("Failed handling proxy connection")
        finally:
            self._client_queues.remove(queue)
            await client_connection.disconnect()

    async def _forward_from_radio(self) -> None:
        try:
            while True:
                async for packet in self._interface.from_radio_stream():
                    for queue in self._client_queues:
                        await queue.put(packet)
        except asyncio.CancelledError:
            pass
        except:  # noqa: E722
            _LOGGER.warning("Failuring during forwarding from gateway", exc_info=True)
