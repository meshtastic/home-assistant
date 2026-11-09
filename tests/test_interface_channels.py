# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import AsyncIterator

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.connection import ClientApiConnection
from custom_components.meshtastic.aiomeshtastic.protobuf import channel_pb2, mesh_pb2


def channel_packet(index: int, name: str) -> mesh_pb2.FromRadio:
    return mesh_pb2.FromRadio(
        channel=channel_pb2.Channel(
            index=index,
            role=channel_pb2.Channel.Role.PRIMARY if index == 0 else channel_pb2.Channel.Role.SECONDARY,
            settings=channel_pb2.ChannelSettings(name=name),
        )
    )


class BootstrapRadio(ClientApiConnection):
    """In-memory radio that answers a config request with its current channels."""

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names
        self._connected = True
        self._from_radio: asyncio.Queue[mesh_pb2.FromRadio] = asyncio.Queue()

    async def _connect(self) -> None:
        self._connected = True

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
            for index, name in enumerate(self.names):
                self._from_radio.put_nowait(channel_packet(index, name))
            self._from_radio.put_nowait(mesh_pb2.FromRadio(config_complete_id=to_radio.want_config_id))
        return True


async def test_channels_stay_stable_across_repeated_bootstraps() -> None:
    interface = MeshInterface(connection=None, enable_mqtt_proxy=False)
    interface._connected_node_channels = []  # as _start_config() does

    for generation in range(3):
        # out of order on purpose; positions must still match channel indices
        for index in (1, 0, 2):
            await interface._process_connected_node_packets(channel_packet(index, f"ch{index}-g{generation}"))

    channels = interface._connected_node_channels
    assert [c.index for c in channels] == [0, 1, 2]
    assert [c.settings.name for c in channels] == ["ch0-g2", "ch1-g2", "ch2-g2"]


async def test_channel_index_past_the_end_is_unavailable() -> None:
    interface = MeshInterface(connection=None, enable_mqtt_proxy=False)
    interface._connected_node_info = mesh_pb2.MyNodeInfo(my_node_num=1)
    interface._connected_node_channels = []
    for index in (0, 1):
        await interface._process_connected_node_packets(channel_packet(index, f"ch{index}"))

    assert interface.find_channel(index=1).index == 1
    assert interface.find_channel(index=2) is None
    with pytest.raises(ValueError, match="Unavailable channel index"):
        await interface.send_text_message("hello", channel_index=2)


async def test_reconnect_refreshes_channels() -> None:
    radio = BootstrapRadio(["old"])
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    interface._is_running.set()  # as start() does, without the packet loop, which is busy during a reconnect
    interface._connected_node_channels = [channel_packet(0, "old").channel]

    radio.names = ["new0", "new1"]  # changed while we were disconnected
    await asyncio.wait_for(interface._reconnect_while_running(force=True), timeout=5)

    assert [c.settings.name for c in interface._connected_node_channels] == ["new0", "new1"]
