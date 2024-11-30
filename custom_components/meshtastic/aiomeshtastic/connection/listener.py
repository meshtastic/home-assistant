import asyncio
from collections.abc import AsyncIterable
from types import TracebackType
from typing import Self

from meshtastic.protobuf import mesh_pb2


class ClientApiConnectionPacketStreamListener:
    def __init__(self, queue_size: int = 16) -> None:
        self._failure: Exception | None = None
        self._queue = asyncio.Queue(maxsize=queue_size)
        self._closed = asyncio.Event()

    async def notify(self, packet: mesh_pb2.FromRadio) -> None:
        await self._queue.put(packet)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> mesh_pb2.FromRadio:
        self._stop_if_needed()

        queue_task = asyncio.create_task(self._queue.get(), name="packet-stream-listener-queue-get")
        closed_task = asyncio.create_task(self._closed.wait(), name="packet-stream-listener-closed")
        try:
            done, pending = await asyncio.wait([queue_task, closed_task], return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            queue_task.cancel()
            closed_task.cancel()
            raise

        for task in pending:
            task.cancel()

        if done == {closed_task}:
            self._stop_if_needed()

        return await queue_task

    def _stop_if_needed(self) -> None:
        if self._closed.is_set():
            if self._failure is not None:
                raise self._failure

            raise StopAsyncIteration

    def packets(self) -> AsyncIterable[mesh_pb2.FromRadio]:
        return self

    def close(self) -> None:
        if self._closed.is_set():
            return

        self._closed.set()

    def set_failure(self, e: Exception) -> None:
        self._failure = e
        self._closed.set()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
