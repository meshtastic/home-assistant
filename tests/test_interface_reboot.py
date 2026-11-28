# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import AsyncIterator

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic import interface as interface_module
from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.interface import ConnectionState
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2


class FakeRadio(ClientApiConnection):
    """In-memory radio that answers a config request with an empty bootstrap."""

    def __init__(self) -> None:
        super().__init__()
        self.connects = 0
        self.ignore_config_requests = 0
        self.stall_next_disconnect = False
        self.disconnect_started = asyncio.Event()
        self.reconnected = asyncio.Event()
        self._connected = False
        self._from_radio: asyncio.Queue[mesh_pb2.FromRadio] = asyncio.Queue()

    async def _connect(self) -> None:
        self._connected = True
        self.connects += 1
        if self.connects > 1:
            self.reconnected.set()

    async def _disconnect(self) -> None:
        if self.stall_next_disconnect:
            self.stall_next_disconnect = False
            self.disconnect_started.set()
            await asyncio.Event().wait()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _packet_stream(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        while True:
            yield await self._from_radio.get()

    async def _send_packet(self, packet: bytes) -> bool:
        to_radio = mesh_pb2.ToRadio.FromString(packet)
        if to_radio.HasField("want_config_id") and self.ignore_config_requests:
            self.ignore_config_requests -= 1
        elif to_radio.HasField("want_config_id"):
            self._from_radio.put_nowait(mesh_pb2.FromRadio(config_complete_id=to_radio.want_config_id))
        return True

    def receive(self, packet: mesh_pb2.FromRadio) -> None:
        self._from_radio.put_nowait(packet)


async def test_reconnects_after_radio_reboot() -> None:
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)

        radio.receive(mesh_pb2.FromRadio(rebooted=True))

        await asyncio.wait_for(radio.reconnected.wait(), timeout=5)
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        assert interface.is_running
        assert radio.connects == 2
    finally:
        await interface.stop()


async def test_a_packet_stream_subscriber_keeps_receiving_after_a_reboot() -> None:
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    received = asyncio.Event()

    async def subscribe() -> None:
        async for packet in interface.packet_stream():
            if packet.id == 7:
                received.set()

    subscriber = asyncio.create_task(subscribe())
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio.receive(mesh_pb2.FromRadio(rebooted=True))
        await asyncio.wait_for(radio.reconnected.wait(), timeout=5)
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)

        radio.receive(mesh_pb2.FromRadio(packet=mesh_pb2.MeshPacket(id=7)))

        await asyncio.wait_for(received.wait(), timeout=5)
        assert not subscriber.done()
    finally:
        subscriber.cancel()
        await interface.stop()


async def test_a_failed_restart_is_retried_and_the_connection_state_follows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interface_module, "_REBOOT_RESTART_RETRY_S", 0.01)
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    states: list[ConnectionState] = []
    interface.add_connection_state_listener(states.append)
    await interface.start()
    refusals = 2
    connect = radio._connect

    async def flaky() -> None:
        nonlocal refusals
        if refusals:
            refusals -= 1
            raise OSError
        await connect()

    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio._connect = flaky  # type: ignore[method-assign]
        radio.receive(mesh_pb2.FromRadio(rebooted=True))

        await asyncio.wait_for(radio.reconnected.wait(), timeout=5)
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        for _ in range(100):
            if states and states[-1] == ConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.01)

        assert refusals == 0
        assert interface.is_running
        assert states[0] == ConnectionState.RECONNECTING
        assert ConnectionState.FAILED in states
        assert states[-1] == ConnectionState.CONNECTED
    finally:
        await interface.stop()


async def test_stop_ends_a_restart_that_keeps_failing_and_its_subscriber(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_module, "_REBOOT_RESTART_RETRY_S", 0.01)
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    attempts = 0
    retried = asyncio.Event()

    async def subscribe() -> None:
        async for _ in interface.packet_stream():
            pass

    async def refuse() -> None:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            retried.set()
        raise OSError

    subscriber = asyncio.create_task(subscribe())
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio._connect = refuse  # type: ignore[method-assign]
        radio.receive(mesh_pb2.FromRadio(rebooted=True))
        await asyncio.wait_for(retried.wait(), timeout=5)
        assert not interface.is_running

        await interface.stop()
        settled = attempts
        await asyncio.sleep(0.1)

        assert attempts == settled
        await asyncio.wait_for(subscriber, timeout=5)
    finally:
        subscriber.cancel()


async def test_a_restart_whose_config_never_completes_is_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface_module, "_REBOOT_RESTART_RETRY_S", 0.01)
    monkeypatch.setattr(interface_module, "_REBOOT_CONFIG_TIMEOUT_S", 0.05)
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    states: list[ConnectionState] = []
    interface.add_connection_state_listener(states.append)
    await interface.start()
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio.ignore_config_requests = 1
        radio.receive(mesh_pb2.FromRadio(rebooted=True))

        for _ in range(300):
            if radio.connects >= 3 and states and states[-1] == ConnectionState.CONNECTED:
                break
            await asyncio.sleep(0.01)

        assert radio.connects == 3
        assert interface.is_running
        assert await interface.connected_node_ready()
        assert states.count(ConnectionState.CONNECTED) == 1
        assert states[-1] == ConnectionState.CONNECTED
    finally:
        await interface.stop()


async def test_stop_releases_the_transport_when_it_cancels_a_restart_mid_teardown() -> None:
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio.stall_next_disconnect = True
        radio.receive(mesh_pb2.FromRadio(rebooted=True))
        await asyncio.wait_for(radio.disconnect_started.wait(), timeout=5)
        assert not interface.is_running

        await interface.stop()

        assert not radio.is_connected
        assert not interface.is_running
    finally:
        await interface.stop()


async def test_a_reboot_during_stop_does_not_start_a_replacement_restart() -> None:
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        radio.stall_next_disconnect = True
        radio.receive(mesh_pb2.FromRadio(rebooted=True))
        await asyncio.wait_for(radio.disconnect_started.wait(), timeout=5)
        restart = interface._reboot_restart
        cancel = interface._cancel_task

        async def cancel_with_a_reboot_arriving(task: asyncio.Task) -> None:
            await cancel(task)
            if task is restart:
                # a rebooted packet processed while stop() waits for the old restart to end
                await interface._process_connected_node_packets(mesh_pb2.FromRadio(rebooted=True))
                await asyncio.sleep(0.05)

        interface._cancel_task = cancel_with_a_reboot_arriving  # type: ignore[method-assign]

        await interface.stop()
        await asyncio.sleep(0.1)

        assert not interface.is_running
        assert not radio.is_connected
        assert radio.connects == 1
    finally:
        interface._cancel_task = type(interface)._cancel_task.__get__(interface)  # type: ignore[method-assign]
        await interface.stop()
