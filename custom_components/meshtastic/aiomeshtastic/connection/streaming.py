import asyncio
from abc import abstractmethod
from collections.abc import AsyncIterable

from google.protobuf import message

from ..protobuf import mesh_pb2  # noqa: TID252
from . import (
    ClientApiConnection,
    ClientApiConnectionError,
    ClientApiConnectionInterruptedError,
    ClientApiNotConnectedError,
)


class StreamingClientTransport(ClientApiConnection):
    START1 = 0x94
    START2 = 0xC3
    MAGIC_LEN = 2
    HEADER_LEN = 4
    MAX_TO_FROM_RADIO_SIZE = 512

    def __init__(self) -> None:
        super().__init__()
        self._read_lock = asyncio.Lock()

    async def _send_packet(self, packet: bytes) -> bool:
        if not self.is_connected:
            raise ClientApiNotConnectedError
        try:
            return await self._write_bytes(StreamingClientTransport.build_frame(packet))
        except ClientApiConnectionError:
            raise
        except BaseException as e:
            raise ClientApiConnectionInterruptedError from e

    @staticmethod
    def build_frame(payload: bytes) -> bytes:
        payload_length: int = len(payload)
        header: bytes = bytes(
            [
                StreamingClientTransport.START1,
                StreamingClientTransport.START2,
                (payload_length >> 8) & 0xFF,
                payload_length & 0xFF,
            ]
        )
        return header + payload

    async def _on_other_data(self, data: bytes) -> None:
        pass

    async def _read_packet_bytes(self) -> bytes | None:
        header = await self._read_header()
        if header is None:
            return None

        packet_len = (header[2] << 8) + header[3]
        if packet_len > StreamingClientTransport.MAX_TO_FROM_RADIO_SIZE:
            # corrupted packet
            await self._on_other_data(header)
            return None

        return await self._read_bytes(exactly=packet_len)

    async def _packet_stream(self) -> AsyncIterable[mesh_pb2.FromRadio]:
        if not self._can_read():
            raise ClientApiNotConnectedError

        try:
            async with self._read_lock:
                while self._can_read():
                    packet = await self._read_packet_bytes()
                    if packet is None:
                        continue

                    from_radio = mesh_pb2.FromRadio()
                    try:
                        from_radio.ParseFromString(packet)
                        self._logger.debug("Parsed packet: %s", self._protobuf_log(from_radio))
                        yield from_radio
                    except message.DecodeError:
                        self._logger.warning("Error while parsing FromRadio bytes %s", packet, exc_info=True)
        except asyncio.exceptions.IncompleteReadError as e:
            raise ClientApiConnectionInterruptedError from e

    @abstractmethod
    async def _read_bytes(self, n: int = -1, *, exactly: int | None = None) -> bytes | None:
        pass

    @abstractmethod
    async def _write_bytes(self, data: bytes) -> bool:
        pass

    @abstractmethod
    def _can_read(self) -> bool:
        pass

    async def _read_header(self) -> bytes | None:
        header = await self._read_bytes(exactly=1)
        if header[0] != StreamingClientTransport.START1:
            await self._on_other_data(header)
            return None

        header += await self._read_bytes(exactly=1)
        if header[1] != StreamingClientTransport.START2:
            await self._on_other_data(header)
            return None

        header += await self._read_bytes(exactly=2)
        return header

        header = await self._read_bytes(StreamingClientTransport.MAGIC_LEN)
        if header is None:
            return None

        while (missing_magic_bytes := StreamingClientTransport.MAGIC_LEN - len(header)) > 0:
            b = await self._read_bytes(missing_magic_bytes)
            if b is not None:
                header += b

        while not (header[0] == StreamingClientTransport.START1 and header[1] == StreamingClientTransport.START2):
            # check if second byte might be start of header
            if header[1] == StreamingClientTransport.START1:
                header = header[1:]
                # get second byte of header
                while len(header) < StreamingClientTransport.MAGIC_LEN:
                    b = await self._read_bytes(StreamingClientTransport.MAGIC_LEN - len(header))
                    if b is not None:
                        header += b
            else:
                await self._on_other_data(header)
                return None

        # get full header length
        while (missing_header_bytes := StreamingClientTransport.HEADER_LEN - len(header)) > 0:
            b = await self._read_bytes(missing_header_bytes)
            if b is not None:
                header += b

        return header
