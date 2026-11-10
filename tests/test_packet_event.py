# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The packet event republishes packets, but never an admin payload."""

from __future__ import annotations

from custom_components.meshtastic.aiomeshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2
from custom_components.meshtastic.api import _without_admin_payload


def test_an_admin_reply_loses_its_payload_and_keeps_the_rest() -> None:
    reply = admin_pb2.AdminMessage()
    reply.get_device_connection_status_response.bluetooth.pin = 123456
    packet = mesh_pb2.MeshPacket(id=7, to=1, decoded=mesh_pb2.Data(portnum=portnums_pb2.ADMIN_APP))
    setattr(packet, "from", 2)
    packet.decoded.payload = reply.SerializeToString()

    published = _without_admin_payload(packet)

    assert not published.decoded.payload
    assert published.decoded.portnum == portnums_pb2.ADMIN_APP
    assert published.id == 7
    assert getattr(published, "from") == 2
    assert packet.decoded.payload, "the packet the listeners receive is left whole"


def test_other_packets_pass_unchanged() -> None:
    packet = mesh_pb2.MeshPacket(decoded=mesh_pb2.Data(portnum=portnums_pb2.TEXT_MESSAGE_APP, payload=b"hi"))

    assert _without_admin_payload(packet) is packet
