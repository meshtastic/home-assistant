# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from collections.abc import Mapping
from functools import cached_property
from typing import Any, TypeVar

import google

from .const import LOGGER
from .protobuf import admin_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

T = TypeVar("T", None, mesh_pb2.Routing, telemetry_pb2.Telemetry, admin_pb2.AdminMessage, str)


class Packet[T]:
    def __init__(self, packet: mesh_pb2.FromRadio) -> None:
        self._packet = packet
        self._logger = LOGGER.getChild(self.__class__.__name__)

    @property
    def from_id(self) -> int | None:
        return getattr(self.mesh_packet, "from") if self.mesh_packet is not None else None

    @property
    def rx_time(self) -> int | None:
        return self.mesh_packet.rx_time if self.mesh_packet is not None else None

    @property
    def rx_snr(self) -> float | None:
        return self.mesh_packet.rx_snr if self.mesh_packet is not None else None

    @property
    def rx_rssi(self) -> int | None:
        return self.mesh_packet.rx_rssi if self.mesh_packet is not None else None

    @property
    def to_id(self) -> int | None:
        return self.mesh_packet.to if self.mesh_packet is not None else None

    @property
    def mesh_packet(self) -> mesh_pb2.MeshPacket | None:
        return self._packet.packet if self._packet.HasField("packet") else None

    @property
    def data(self) -> mesh_pb2.Data | None:
        return self.mesh_packet.decoded if self.mesh_packet and self.mesh_packet.HasField("decoded") else None

    @property
    def port_num(self) -> portnums_pb2.PortNum | None:
        return self.data.portnum if self.data is not None else None

    @cached_property
    def app_payload(self) -> T:  # noqa: PLR0911
        data = self.data
        if data is None or data.portnum is None:
            return None

        port_num = data.portnum
        payload = data.payload

        if port_num == portnums_pb2.PortNum.ROUTING_APP:
            routing = mesh_pb2.Routing()
            routing.ParseFromString(payload)
            return routing
        if port_num == portnums_pb2.PortNum.TEXT_MESSAGE_APP:
            return payload.decode()
        if port_num == portnums_pb2.PortNum.TELEMETRY_APP:
            telemetry = telemetry_pb2.Telemetry()
            telemetry.ParseFromString(payload)
            return telemetry
        if port_num == portnums_pb2.PortNum.ADMIN_APP:
            admin_message = admin_pb2.AdminMessage()
            admin_message.ParseFromString(payload)
            return admin_message
        if port_num == portnums_pb2.PortNum.POSITION_APP:
            position_message = mesh_pb2.Position()
            position_message.ParseFromString(payload)
            return position_message
        if port_num == portnums_pb2.PortNum.NODEINFO_APP:
            node_info = mesh_pb2.NodeInfo()
            node_info.user.ParseFromString(payload)
            node_info.num = self.from_id
            return node_info
        if port_num == portnums_pb2.PortNum.TRACEROUTE_APP:
            route_discovery = mesh_pb2.RouteDiscovery()
            route_discovery.ParseFromString(payload)
            return route_discovery
        self._logger.debug("Unhandled portnum %s", port_num)
        return None

    @property
    def pki_encrypted(self) -> bool:
        return self.mesh_packet.pki_encrypted if self.mesh_packet is not None else False

    @property
    def channel_index(self) -> int | None:
        return self.mesh_packet.channel if self.mesh_packet is not None else None


class FullNodeInfoPacket(Packet[mesh_pb2.NodeInfo]):
    def __init__(self, packet: mesh_pb2.FromRadio) -> None:
        if not packet.HasField("node_info"):
            msg = "Not a node_info packet"
            raise ValueError(msg)
        super().__init__(packet)

    @property
    def port_num(self) -> portnums_pb2.PortNum | None:
        return portnums_pb2.PortNum.NODEINFO_APP

    @cached_property
    def app_payload(self) -> mesh_pb2.NodeInfo:
        return self._packet.node_info


class DatabaseNodeInfoPacket(FullNodeInfoPacket):
    def __init__(self, database: Mapping[str, Any]) -> None:
        from_radio = mesh_pb2.FromRadio()
        try:
            google.protobuf.json_format.ParseDict(database, from_radio.node_info, ignore_unknown_fields=True)
        except:  # noqa: E722
            from_radio.node_info.num = database["num"]
            google.protobuf.json_format.ParseDict(database["user"], from_radio.node_info.user)

        super().__init__(from_radio)
