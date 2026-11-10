# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Copies of protobuf messages with their secrets removed, for logging."""

from __future__ import annotations

from typing import Any

from google.protobuf.message import Message

from .protobuf import field_metadata_pb2, mesh_pb2, portnums_pb2


def without_secrets[M: Message](message: M) -> M:
    """Return a copy without the fields the schema marks sensitive and without admin payloads, at any depth."""
    copy = type(message)()
    copy.CopyFrom(message)
    _redact(copy)
    return copy


def _redact(message: Message) -> None:
    # an admin payload is a serialized AdminMessage, so its secrets are bytes the field walk cannot see
    if isinstance(message, mesh_pb2.Data) and message.portnum == portnums_pb2.PortNum.ADMIN_APP:
        message.ClearField("payload")
    for field, value in message.ListFields():
        if field.GetOptions().Extensions[field_metadata_pb2.field_metadata].sensitive:
            message.ClearField(field.name)
        elif field.message_type is not None:
            for item in _message_values(field, value):
                _redact(item)


def _message_values(field: Any, value: Any) -> list[Message]:
    if field.message_type.GetOptions().map_entry:
        value_field = field.message_type.fields_by_name["value"]
        return list(value.values()) if value_field.message_type is not None else []
    if isinstance(value, Message):
        return [value]
    return list(value)
