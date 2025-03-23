import asyncio
import contextlib
import re
from asyncio import StreamReader, StreamReaderProtocol, StreamWriter
from typing import cast

import serial
import serial_asyncio

from custom_components.meshtastic.aiomeshtastic.connection import (
    ClientApiConnectionError,
)
from custom_components.meshtastic.aiomeshtastic.connection.streaming import (
    StreamingClientTransport,
)


class SerialConnectionError(ClientApiConnectionError):
    pass


class SerialConnection(StreamingClientTransport, asyncio.Protocol):
    def __init__(self, device: str, baud_rate: int = 115200, *, debug_logs: bool = False) -> None:
        super().__init__()
        self._log_reader_task: asyncio.Task | None = None
        self._log_reader: StreamReader | None = None
        self._writer: StreamWriter | None = None
        self._reader: StreamReader | None = None
        self._device = device
        self._baud_rate = baud_rate
        self._debug_logs = debug_logs

    async def _connect(self) -> None:
        loop = asyncio.get_running_loop()
        reader = StreamReader(loop=loop)
        protocol = StreamReaderProtocol(reader, loop=loop)
        transport, _ = await serial_asyncio.create_serial_connection(
            loop,
            lambda: protocol,
            self._device,
            baudrate=self._baud_rate,
            exclusive=True,
        )
        writer = StreamWriter(transport, protocol, reader, loop)
        self._reader = reader
        self._writer = writer

        if self._debug_logs:
            self._log_reader = StreamReader(loop=loop)

            async def read_log(reader: StreamReader) -> None:
                logger = self._logger.getChild("log")
                ansi_regex = (
                    r"\x1b("
                    r"(\[\??\d+[hl])|"
                    r"([=<>a-kzNM78])|"
                    r"([\(\)][a-b0-2])|"
                    r"(\[\d{0,2}[ma-dgkjqi])|"
                    r"(\[\d+;\d+[hfy]?)|"
                    r"(\[;?[hf])|"
                    r"(#[3-68])|"
                    r"([01356]n)|"
                    r"(O[mlnp-z]?)|"
                    r"(/Z)|"
                    r"(\d+)|"
                    r"(\[\?\d;\d0c)|"
                    r"(\d;\dR))"
                )

                ansi_escape = re.compile(ansi_regex, flags=re.IGNORECASE)
                while not reader.at_eof():
                    try:
                        line = await reader.readline()
                        line_str = line.decode(errors="replace")
                        log_mes = ansi_escape.sub("", line_str)
                        logger.debug(log_mes)
                    except asyncio.CancelledError:
                        break

            self._log_reader_task = asyncio.create_task(read_log(self._log_reader), name="log")

    async def _disconnect(self) -> None:
        if self._writer:
            self._writer.close()
            cast("serial_asyncio.SerialTransport", self._writer.transport).serial.close()
            self._writer = None
            self._reader = None

        if self._log_reader:
            self._log_reader.at_eof()
            self._log_reader = None

        if self._log_reader_task is not None:
            try:
                with contextlib.suppress(asyncio.CancelledError):
                    self._log_reader_task.cancel()
                    await self._log_reader_task
            finally:
                self._log_reader_task = None

    async def _on_other_data(self, data: bytes) -> None:
        if self.START1 in data or self.START2 in data:
            pass

        if self._log_reader:
            self._log_reader.feed_data(data)

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and self._reader is not None and not self._reader.at_eof()

    def _can_read(self) -> bool:
        return self._reader is not None and not self._reader.at_eof()

    async def _read_bytes(self, n: int = -1, *, exactly: int | None = None) -> bytes | None:
        if not self._can_read():
            msg = "Can not read bytes"
            raise ValueError(msg)
        try:
            if exactly is not None:
                return await self._reader.readexactly(exactly)
            return await self._reader.read(n=n)
        except serial.serialutil.SerialException as e:
            raise SerialConnectionError from e

    async def _write_bytes(self, data: bytes) -> bool:
        if self._writer is None:
            return False
        self._writer.write(data)
        return True
