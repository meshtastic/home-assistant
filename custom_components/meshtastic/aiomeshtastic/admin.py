# SPDX-FileCopyrightText: 2026 Marc Warne <marc@kernel-panic.im>
#
# SPDX-License-Identifier: MIT

"""Which writes to the radio are admin for the gateway itself, and which of those change it."""

from google.protobuf.message import DecodeError

from .protobuf import admin_pb2, mesh_pb2, portnums_pb2

# destinations firmware treats as the gateway itself
_LOCAL_DESTINATIONS = {0, 1, 0xFFFFFFFF}
# gateway admin that changes nothing the radio keeps
_ADMIN_NOT_STATE = frozenset({"set_time_only", "begin_edit_settings", "send_input_event"})


def gateway_admin(to_radio: mesh_pb2.ToRadio, my_node_num: int | None) -> admin_pb2.AdminMessage | None:
    """
    Return the admin message of a ToRadio for the gateway itself, or None if it is not one.

    With the gateway's node number unknown, every admin message counts as for the gateway. One that does not
    decode is returned empty, which counts as a change.
    """
    packet = to_radio.packet
    if packet.decoded.portnum != portnums_pb2.PortNum.ADMIN_APP:
        return None
    if my_node_num is not None and packet.to != my_node_num and packet.to not in _LOCAL_DESTINATIONS:
        return None
    try:
        return admin_pb2.AdminMessage.FromString(packet.decoded.payload)
    except DecodeError:
        return admin_pb2.AdminMessage()


def changes_gateway(admin: admin_pb2.AdminMessage) -> bool:
    """Return whether gateway admin changes the gateway; empty or unreadable admin counts."""
    variant = admin.WhichOneof("payload_variant")
    return variant is None or not (variant.startswith("get_") or variant in _ADMIN_NOT_STATE)
