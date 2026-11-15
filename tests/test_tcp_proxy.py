# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

"""Offline tests for the TCP proxy: fake upstream interface, fake client streams, no sockets."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from custom_components.meshtastic.aiomeshtastic.connection.streaming import StreamingClientTransport
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2
from custom_components.meshtastic.meshtastic_tcp import server

FRAME = StreamingClientTransport.build_frame


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[mesh_pb2.ToRadio] = []

    async def forward(self, packet: bytes, to_radio: mesh_pb2.ToRadio) -> bool:
        assert mesh_pb2.ToRadio.FromString(packet) == to_radio
        self.sent.append(to_radio)
        return True


class FakeInterface:
    is_running = True

    def __init__(self) -> None:
        self._connection = FakeConnection()
        self.packets: asyncio.Queue[mesh_pb2.FromRadio] = asyncio.Queue()

    async def from_radio_stream(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        while True:
            packet = await self.packets.get()
            if isinstance(packet, Exception):
                raise packet
            yield packet
            await asyncio.sleep(0)  # a real upstream yields between frames


class FakeServer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, writer: "FakeWriter") -> None:
        self._writer = writer

    def get_extra_info(self, name: str, default: object = None) -> object:
        return ("127.0.0.1", self._writer.port) if name == "peername" else default

    def abort(self) -> None:
        self._writer.aborted = True
        self._writer.closing = True


class FakeWriter:
    _port = 50000

    def __init__(
        self,
        *,
        drain_hangs: bool = False,
        drain_error: Exception | None = None,
        close_error: Exception | None = None,
        close_hangs: bool = False,
    ) -> None:
        FakeWriter._port += 1
        self.port = FakeWriter._port
        self.data = bytearray()
        self.closing = False
        self.aborted = False
        self.drain_hangs = drain_hangs
        self.drain_error = drain_error
        self.close_error = close_error
        self.close_hangs = close_hangs
        self.transport = FakeTransport(self)

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error
        if self.drain_hangs:
            await asyncio.Event().wait()

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    async def wait_closed(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        if self.close_hangs:
            await asyncio.Event().wait()

    def received(self) -> list[mesh_pb2.FromRadio]:
        packets, data = [], bytes(self.data)
        while data:
            assert data[:2] == bytes([StreamingClientTransport.START1, StreamingClientTransport.START2])
            length = (data[2] << 8) + data[3]
            from_radio = mesh_pb2.FromRadio()
            from_radio.ParseFromString(data[4 : 4 + length])
            packets.append(from_radio)
            data = data[4 + length :]
        return packets


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110 - the fakes expose state, not events
            await asyncio.sleep(0.01)


def client(writer: FakeWriter | None = None) -> tuple[asyncio.StreamReader, FakeWriter]:
    return asyncio.StreamReader(), writer or FakeWriter()


def send(reader: asyncio.StreamReader, to_radio: mesh_pb2.ToRadio) -> None:
    reader.feed_data(FRAME(to_radio.SerializeToString()))


@pytest.fixture
async def proxy(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[server.MeshtasticTcpProxy]:
    async def fake_start_server(*_args: object, **_kwargs: object) -> FakeServer:
        return FakeServer()

    monkeypatch.setattr(server.asyncio, "start_server", fake_start_server)
    instance = server.MeshtasticTcpProxy(FakeInterface())
    await instance.start()
    await asyncio.sleep(0)  # let the forwarder subscribe
    yield instance
    await instance.stop()


def node_info(num: int) -> mesh_pb2.FromRadio:
    return mesh_pb2.FromRadio(node_info=mesh_pb2.NodeInfo(num=num))


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(FakeWriter(drain_hangs=True), id="slow peer"),
        pytest.param(FakeWriter(drain_error=ConnectionResetError()), id="dead peer write"),
    ],
)
async def test_unwritable_peer_is_disconnected_alone(
    proxy: server.MeshtasticTcpProxy, monkeypatch: pytest.MonkeyPatch, writer: FakeWriter
) -> None:
    monkeypatch.setattr(server, "_WRITE_TIMEOUT_S", 0.05)
    reader, writer = client(writer)
    healthy_reader, healthy_writer = client()
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    healthy = asyncio.create_task(proxy._handle_client(healthy_reader, healthy_writer))
    await asyncio.sleep(0)

    proxy._interface.packets.put_nowait(node_info(1))
    async with asyncio.timeout(2):
        await handler
    assert writer.closing

    proxy._interface.packets.put_nowait(node_info(2))
    await wait_until(lambda: [p.node_info.num for p in healthy_writer.received()] == [1, 2])
    assert not healthy.done()
    healthy_reader.feed_eof()
    await healthy


async def test_dead_peer_eof_closes_handler(proxy: server.MeshtasticTcpProxy) -> None:
    reader, writer = client()
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    reader.feed_eof()
    async with asyncio.timeout(2):
        await handler
    assert writer.closing


@pytest.mark.parametrize("value", [True, False])
async def test_disconnect_is_never_forwarded(proxy: server.MeshtasticTcpProxy, value: bool) -> None:  # noqa: FBT001
    reader, writer = client()
    other_reader, other_writer = client()
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    other = asyncio.create_task(proxy._handle_client(other_reader, other_writer))

    to_radio = mesh_pb2.ToRadio(disconnect=value)
    assert to_radio.HasField("disconnect")
    send(reader, to_radio)
    async with asyncio.timeout(2):
        await handler

    assert writer.closing
    assert proxy._interface._connection.sent == []
    assert not other.done()
    assert not other_writer.closing
    other_reader.feed_eof()
    await other


def bootstrap(nonce: int) -> list[mesh_pb2.FromRadio]:
    return [
        mesh_pb2.FromRadio(my_info=mesh_pb2.MyNodeInfo(my_node_num=1)),
        *(node_info(num) for num in range(1, 9)),
        mesh_pb2.FromRadio(config_complete_id=nonce),
    ]


async def test_wedged_client_is_evicted_while_healthy_client_bootstraps(
    proxy: server.MeshtasticTcpProxy, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(server, "_CLIENT_QUEUE_SIZE", 4)
    wedged_reader, wedged_writer = client(FakeWriter(drain_hangs=True))
    healthy_reader, healthy_writer = client()
    wedged = asyncio.create_task(proxy._handle_client(wedged_reader, wedged_writer))
    healthy = asyncio.create_task(proxy._handle_client(healthy_reader, healthy_writer))

    send(healthy_reader, mesh_pb2.ToRadio(want_config_id=1234))
    await wait_until(lambda: len(proxy._interface._connection.sent) == 1)
    assert proxy._interface._connection.sent[0].want_config_id == 1234

    expected = bootstrap(1234)
    for packet in expected:
        proxy._interface.packets.put_nowait(packet)

    async with asyncio.timeout(2):
        await wedged
    assert wedged_writer.closing
    assert f"Proxy client 127.0.0.1:{wedged_writer.port} is not keeping up" in caplog.text
    await wait_until(lambda: healthy_writer.received() == expected)
    assert not healthy.done()

    # live traffic still flows to the healthy client after the eviction
    proxy._interface.packets.put_nowait(node_info(42))
    await wait_until(lambda: healthy_writer.received()[-1:] == [node_info(42)])
    healthy_reader.feed_eof()
    await healthy


@pytest.mark.parametrize(
    "writer",
    [
        pytest.param(FakeWriter(close_error=ConnectionResetError()), id="broken close"),
        pytest.param(FakeWriter(close_hangs=True), id="hanging close"),
    ],
)
async def test_failed_close_falls_back_to_abort(
    proxy: server.MeshtasticTcpProxy, monkeypatch: pytest.MonkeyPatch, writer: FakeWriter
) -> None:
    monkeypatch.setattr(server, "_CLOSE_TIMEOUT_S", 0.05)
    reader, writer = client(writer)
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    reader.feed_eof()
    async with asyncio.timeout(2):
        await handler
    assert writer.aborted
    assert proxy._clients == {}


async def test_is_connected_reflects_transport_state() -> None:
    reader, writer = client()
    connection = server.ClientProxyTransport(reader, writer)
    assert connection.is_connected
    await connection.disconnect()
    assert not connection.is_connected


async def test_forwarder_failure_closes_listener_and_clients(
    proxy: server.MeshtasticTcpProxy, caplog: pytest.LogCaptureFixture
) -> None:
    reader, writer = client()
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    await asyncio.sleep(0)

    proxy._interface.packets.put_nowait(RuntimeError("upstream listener failed"))
    async with asyncio.timeout(2):
        await handler
    assert writer.closing
    assert proxy._server.closed
    assert "Forwarding from gateway failed" in caplog.text

    # a connection that still races in is closed instead of being served nothing
    late_reader, late_writer = client()
    async with asyncio.timeout(2):
        await proxy._handle_client(late_reader, late_writer)
    assert late_writer.closing
    assert proxy._clients == {}


async def test_stop_is_bounded_with_wedged_clients(
    proxy: server.MeshtasticTcpProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "_CLOSE_TIMEOUT_S", 0.05)
    writers = [FakeWriter(drain_hangs=True, close_hangs=True) for _ in range(3)]
    handlers = [asyncio.create_task(proxy._handle_client(*client(w))) for w in writers]
    await asyncio.sleep(0)
    proxy._interface.packets.put_nowait(node_info(1))
    await wait_until(lambda: all(w.data for w in writers))

    async with asyncio.timeout(1):
        await proxy.stop()
    assert all(h.done() for h in handlers)
    assert all(w.aborted for w in writers)
    assert proxy._server.closed
    assert proxy._forward_task.done()
    assert proxy._handlers == set()
    assert proxy._clients == {}


async def test_write_deadline_drop_is_logged_with_peer(
    proxy: server.MeshtasticTcpProxy, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(server, "_WRITE_TIMEOUT_S", 0.05)
    reader, writer = client(FakeWriter(drain_hangs=True))
    handler = asyncio.create_task(proxy._handle_client(reader, writer))
    await asyncio.sleep(0)

    proxy._interface.packets.put_nowait(node_info(1))
    async with asyncio.timeout(2):
        await handler
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [f"Proxy client 127.0.0.1:{writer.port} did not take a frame within 0.05 s, disconnecting it"]


async def test_client_arriving_during_stop_is_refused(proxy: server.MeshtasticTcpProxy) -> None:
    reader, writer = client()
    # accepted before stop(), but first runs while stop() waits for the forwarder to cancel
    late = asyncio.create_task(proxy._handle_client(reader, writer))
    await proxy.stop()
    async with asyncio.timeout(1):
        await late
    assert writer.closing
    assert proxy._handlers == set()
    assert proxy._clients == {}


@pytest.mark.usefixtures("socket_enabled")  # pytest-homeassistant-custom-component blocks sockets
async def test_loopback_bootstrap_and_disconnect() -> None:
    """Real asyncio server, socket and framing on loopback; only the upstream interface is faked."""
    proxy = server.MeshtasticTcpProxy(FakeInterface(), host="127.0.0.1")
    proxy._port = 0  # the constructor maps 0 to the default port; let the OS choose one
    await proxy.start()
    try:
        port = proxy._server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(FRAME(mesh_pb2.ToRadio(want_config_id=7).SerializeToString()))
        await writer.drain()
        await wait_until(lambda: len(proxy._interface._connection.sent) == 1)
        assert proxy._interface._connection.sent[0].want_config_id == 7

        # about the size of a real bootstrap, written before the client reads anything
        expected = [
            mesh_pb2.FromRadio(node_info=mesh_pb2.NodeInfo(num=num, user=mesh_pb2.User(long_name=f"node {num:04}" * 4)))
            for num in range(300)
        ]
        for packet in expected:
            proxy._interface.packets.put_nowait(packet)

        received = []
        async with asyncio.timeout(5):
            while len(received) < len(expected):
                header = await reader.readexactly(4)
                assert header[:2] == bytes([StreamingClientTransport.START1, StreamingClientTransport.START2])
                from_radio = mesh_pb2.FromRadio()
                from_radio.ParseFromString(await reader.readexactly((header[2] << 8) + header[3]))
                received.append(from_radio)
        assert received == expected

        writer.write(FRAME(mesh_pb2.ToRadio(disconnect=True).SerializeToString()))
        await writer.drain()
        async with asyncio.timeout(5):
            assert await reader.read() == b""
        assert len(proxy._interface._connection.sent) == 1
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.stop()
