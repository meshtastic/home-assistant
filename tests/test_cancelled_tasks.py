# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.connection.errors import ClientApiConnectionError
from custom_components.meshtastic.aiomeshtastic.protobuf import mesh_pb2, portnums_pb2

from .test_interface_reboot import FakeRadio


async def _leftover_tasks_after_cancelling(
    coro: Coroutine[Any, Any, object], before: set[asyncio.Task]
) -> set[asyncio.Task]:
    task = asyncio.ensure_future(coro)
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    return {t for t in asyncio.all_tasks() - before if not t.done()} - {asyncio.current_task()}


async def test_send_waiting_for_a_full_queue_leaves_no_tasks_when_cancelled() -> None:
    radio = FakeRadio()
    radio._queue_status = mesh_pb2.QueueStatus(free=0, maxlen=16)
    before = asyncio.all_tasks()

    leftover = await _leftover_tasks_after_cancelling(
        radio.send_packet(mesh_pb2.ToRadio(packet=mesh_pb2.MeshPacket(id=1))), before
    )

    assert leftover == set()


async def test_request_awaiting_a_response_leaves_no_tasks_when_cancelled() -> None:
    radio = FakeRadio()
    interface = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await interface.start()
    try:
        await asyncio.wait_for(interface.connected_node_ready(), timeout=5)
        before = asyncio.all_tasks()

        leftover = await _leftover_tasks_after_cancelling(
            interface._send_message_await_response(
                node=0x1234, message=b"", port_num=portnums_pb2.PortNum.TEXT_MESSAGE_APP, timeout=60
            ),
            before,
        )

        assert leftover == set()
    finally:
        await interface.stop()


async def test_a_config_request_stops_when_cancelled_inside_its_packet_callback() -> None:
    radio = FakeRadio()
    await radio.connect()

    async def never_returns(_packet: mesh_pb2.FromRadio) -> None:
        await asyncio.Event().wait()

    radio.receive(mesh_pb2.FromRadio(my_info=mesh_pb2.MyNodeInfo(my_node_num=1)))
    try:
        async with asyncio.timeout(2):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(radio.request_config(on_packet=never_returns), timeout=0.05)
    finally:
        await radio.disconnect()


async def test_a_reconnect_cancelled_midway_releases_the_callers_waiting_on_it() -> None:
    radio = FakeRadio()
    connecting = asyncio.Event()

    async def hang() -> None:
        connecting.set()
        await asyncio.Event().wait()

    radio._connect = hang  # type: ignore[method-assign]
    reconnect = asyncio.create_task(radio.reconnect())
    await connecting.wait()
    waiter = asyncio.create_task(radio.reconnect())
    await asyncio.sleep(0)

    before = asyncio.all_tasks()

    reconnect.cancel()

    with pytest.raises(ClientApiConnectionError):
        await asyncio.wait_for(waiter, timeout=2)
    await asyncio.gather(reconnect, return_exceptions=True)
    await asyncio.sleep(0)
    assert not radio._reconnect_in_progress.is_set()
    assert {t for t in asyncio.all_tasks() - before if not t.done()} == set()
