import asyncio
from asyncio import StreamReader, StreamWriter

from . import ClientApiConnectionError
from .streaming import StreamingClientTransport


class TcpConnectionError(ClientApiConnectionError):
    pass


class TcpConnection(StreamingClientTransport):
    DEFAULT_TCP_PORT = 4403

    def __init__(self, host: str, port: int = DEFAULT_TCP_PORT) -> None:
        super().__init__()
        self._reader: StreamReader | None = None
        self._writer: StreamWriter | None = None
        self._host = host
        self._port = port

    def _can_read(self) -> bool:
        return self._reader is not None and not self._reader.at_eof()

    async def _read_bytes(self, n: int = -1, *, exactly: int | None = None) -> bytes | None:
        if not self._can_read():
            await self._disconnect()
            msg = "Can not read bytes"
            raise TcpConnectionError(msg)
        try:
            if exactly is not None:
                return await self._reader.readexactly(n=exactly)
            return await self._reader.read(n=n)
        except (ConnectionError, TimeoutError) as e:
            await self._disconnect()
            raise TcpConnectionError from e

    async def _write_bytes(self, data: bytes) -> bool:
        if self._writer is None:
            return False

        try:
            self._writer.write(data)
        except ConnectionError:
            await self._disconnect()
        return True

    async def _connect(self) -> None:
        self._logger.debug("Connecting to %s:%d", self._host, self._port)
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        self._logger.debug("Connection successful")

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and self._reader is not None and not self._reader.at_eof()

    async def _disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            self._writer = None
            self._reader = None
