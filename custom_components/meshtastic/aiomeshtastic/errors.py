# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from .protobuf import mesh_pb2


class MeshtasticError(Exception):
    pass


class MeshInterfaceError(MeshtasticError):
    pass


class MeshInterfaceRequestError(MeshtasticError):
    pass


# what the routing errors of the firmware mean for the person who sees them
ROUTING_ERROR_HINTS = {
    "NO_ROUTE": "no route to the node is known",
    "GOT_NAK": "a node on the way refused the packet",
    "TIMEOUT": "no answer arrived in time",
    "NO_INTERFACE": "the node has no way to send the packet",
    "MAX_RETRANSMIT": "no acknowledgement after the maximum number of retries; the node is probably out of range, "
    "offline or asleep",
    "NO_CHANNEL": "the node does not have the channel the packet was sent on",
    "TOO_LARGE": "the packet is too large",
    "NO_RESPONSE": "the node did not answer the request",
    "DUTY_CYCLE_LIMIT": "the radio reached its legal airtime limit, try again later",
    "BAD_REQUEST": "the node rejected the request",
    "NOT_AUTHORIZED": "the node does not allow this request from here",
    "PKI_FAILED": "the end-to-end encryption between the nodes failed",
    "PKI_UNKNOWN_PUBKEY": "the node does not know the public key of the sender yet",
    "ADMIN_BAD_SESSION_KEY": "the admin session of the request is not valid",
    "ADMIN_PUBLIC_KEY_UNAUTHORIZED": "the node does not accept admin requests from this key",
}


class MeshRoutingError(MeshtasticError):
    def __init__(self, error: mesh_pb2.Routing.Error.ValueType) -> None:
        self._error = error
        try:
            name = mesh_pb2.Routing.Error.Name(error)
        except ValueError:
            name = str(error)
        hint = ROUTING_ERROR_HINTS.get(name)
        super().__init__(f"Routing error: {name} ({hint})" if hint else f"Routing error: {name}")
