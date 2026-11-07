# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.protobuf import channel_pb2, mesh_pb2


def channel_packet(index: int, name: str) -> mesh_pb2.FromRadio:
    return mesh_pb2.FromRadio(
        channel=channel_pb2.Channel(
            index=index,
            role=channel_pb2.Channel.Role.PRIMARY if index == 0 else channel_pb2.Channel.Role.SECONDARY,
            settings=channel_pb2.ChannelSettings(name=name),
        )
    )


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
