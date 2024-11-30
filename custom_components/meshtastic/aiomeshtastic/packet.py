from functools import cached_property
from typing import Optional, TypeVar

from meshtastic.protobuf import admin_pb2, mesh_pb2, portnums_pb2, telemetry_pb2

from .const import LOGGER

T = TypeVar("T", None, mesh_pb2.Routing, telemetry_pb2.Telemetry, admin_pb2.AdminMessage, str)


class Packet[T]:
    def __init__(self, packet: mesh_pb2.FromRadio) -> None:
        self._packet = packet
        self._logger = LOGGER.getChild(self.__class__.__name__)

    @property
    def from_id(self) -> str | None:
        return getattr(self.mesh_packet, "from") if self.mesh_packet is not None else None

    @property
    def to_id(self) -> str | None:
        return self.mesh_packet.to if self.mesh_packet is not None else None

    @property
    def mesh_packet(self) -> mesh_pb2.MeshPacket | None:
        return self._packet.packet if self._packet.HasField("packet") else None

    @property
    def data(self) -> mesh_pb2.Data | None:
        return self.mesh_packet.decoded if self.mesh_packet and self.mesh_packet.HasField("decoded") else None

    @property
    def port_num(self) -> Optional[portnums_pb2.PortNum]:  # noqa: UP007
        return self.data.portnum if self.data is not None else None

    @cached_property
    def app_payload(self) -> T:
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
        self._logger.debug("Unhandled portnum %s", port_num)
        return None
