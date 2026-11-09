# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import AsyncIterator

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2


class FakeRadio(ClientApiConnection):
    """In-memory radio that answers a config request with an empty bootstrap."""

    def __init__(self) -> None:
        super().__init__()
        self.connects = 0
        self.reconnected = asyncio.Event()
        self._connected = False
        self._from_radio: asyncio.Queue[mesh_pb2.FromRadio] = asyncio.Queue()

    async def _connect(self) -> None:
        self._connected = True
        self.connects += 1
        if self.connects > 1:
            self.reconnected.set()

    async def _disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def _packet_stream(self) -> AsyncIterator[mesh_pb2.FromRadio]:
        while True:
            yield await self._from_radio.get()

    async def _send_packet(self, packet: bytes) -> bool:
        to_radio = mesh_pb2.ToRadio.FromString(packet)
        if to_radio.HasField("want_config_id"):
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
