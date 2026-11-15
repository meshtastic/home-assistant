# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

import asyncio
from collections.abc import AsyncIterator

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface, interface
from custom_components.meshtastic.aiomeshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2

from .test_interface_reboot import FakeRadio

GATEWAY = 0x1234


class Gateway(FakeRadio):
    """A TCP gateway that tells its node number and never answers a connection check, as when restarting."""

    async def _send_packet(self, packet: bytes) -> bool:
        to_radio = mesh_pb2.ToRadio.FromString(packet)
        if to_radio.HasField("want_config_id"):
            self.receive(mesh_pb2.FromRadio(my_info=mesh_pb2.MyNodeInfo(my_node_num=GATEWAY)))
        return await super()._send_packet(packet)

    async def client_admin(self, **fields: object) -> None:
        """Send admin to the gateway as a proxy client would."""
        to_radio = mesh_pb2.ToRadio(
            packet=mesh_pb2.MeshPacket(
                to=GATEWAY,
                decoded=mesh_pb2.Data(
                    portnum=portnums_pb2.PortNum.ADMIN_APP,
                    payload=admin_pb2.AdminMessage(**fields).SerializeToString(),  # type: ignore[arg-type]
                ),
            )
        )
        await self.forward(to_radio.SerializeToString(), to_radio)


class HealthyGateway(Gateway):
    """A gateway that answers the connection check, so has not restarted."""

    def __init__(self) -> None:
        super().__init__()
        self.status_requests = 0

    async def _send_packet(self, packet: bytes) -> bool:
        to_radio = mesh_pb2.ToRadio.FromString(packet)
        if to_radio.packet.decoded.portnum == portnums_pb2.PortNum.ADMIN_APP:
            admin = admin_pb2.AdminMessage.FromString(to_radio.packet.decoded.payload)
            if admin.get_device_connection_status_request:
                self.status_requests += 1
                response = admin_pb2.AdminMessage()
                response.get_device_connection_status_response.SetInParent()
                self.receive(
                    mesh_pb2.FromRadio(
                        packet=mesh_pb2.MeshPacket(
                            to=to_radio.packet.to,
                            **{"from": GATEWAY},
                            decoded=mesh_pb2.Data(
                                portnum=portnums_pb2.PortNum.ADMIN_APP,
                                payload=response.SerializeToString(),
                                request_id=to_radio.packet.id,
                            ),
                        )
                    )
                )
        return await super()._send_packet(packet)


@pytest.fixture(params=[Gateway])
async def gateway(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> AsyncIterator[Gateway]:
    monkeypatch.setattr(interface, "_RESTART_DELAY_S", 0.05)
    monkeypatch.setattr(interface, "_RESTART_PROBE_GRACE_S", 0.05)
    monkeypatch.setattr(interface, "_RESTART_PROBE_TIMEOUT_S", 0.2)
    radio = request.param()
    mesh = MeshInterface(connection=radio, no_nodes=True, enable_mqtt_proxy=False)
    await mesh.start()
    try:
        await asyncio.wait_for(mesh.connected_node_ready(), timeout=5)
        yield radio
    finally:
        await mesh.stop()


async def test_a_restart_from_a_client_is_noticed_without_waiting_for_the_heartbeat(gateway: Gateway) -> None:
    await gateway.client_admin(reboot_seconds=0)

    await asyncio.wait_for(gateway.reconnected.wait(), timeout=2)


async def test_a_read_is_not_followed_by_a_check(gateway: Gateway) -> None:
    await gateway.client_admin(get_owner_request=True)

    await asyncio.sleep(0.5)
    assert not gateway.reconnected.is_set()


async def test_a_cancelled_restart_is_not_checked(gateway: Gateway) -> None:
    await gateway.client_admin(reboot_seconds=1)
    await gateway.client_admin(reboot_seconds=-1)

    await asyncio.sleep(1.5)
    assert not gateway.reconnected.is_set()


async def test_cancelling_a_reboot_keeps_the_check_for_a_shutdown(gateway: Gateway) -> None:
    await gateway.client_admin(shutdown_seconds=0)
    await gateway.client_admin(reboot_seconds=-1)

    await asyncio.wait_for(gateway.reconnected.wait(), timeout=2)


@pytest.mark.parametrize("gateway", [HealthyGateway], indirect=True)
async def test_a_radio_that_answers_the_check_is_left_alone(gateway: HealthyGateway) -> None:
    await gateway.client_admin(reboot_seconds=0)

    await asyncio.sleep(0.5)
    assert gateway.status_requests == 1
    assert not gateway.reconnected.is_set()
