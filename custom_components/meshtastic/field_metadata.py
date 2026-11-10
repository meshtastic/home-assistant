# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""Read the protobuf FieldMetadata options from the generated descriptors."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from .aiomeshtastic.protobuf import channel_pb2, config_pb2, field_metadata_pb2, localonly_pb2

if TYPE_CHECKING:
    from collections.abc import Mapping

    from google.protobuf.descriptor import Descriptor, FieldDescriptor

_ROLE_ENUM = config_pb2.Config.DeviceConfig.Role.DESCRIPTOR


def is_sensitive(field: FieldDescriptor) -> bool:
    """Whether the schema marks the field as a secret (a password, PIN or private or shared key)."""
    return field.GetOptions().Extensions[field_metadata_pb2.field_metadata].sensitive


def without_sensitive_fields(descriptor: Descriptor, data: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the schema's sensitive fields from `MessageToDict` output of `descriptor`, at any depth."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        field = descriptor.fields_by_camelcase_name.get(key) or descriptor.fields_by_name.get(key)
        if field is None:
            result[key] = value
        elif is_sensitive(field):
            continue
        elif field.message_type is not None and isinstance(value, dict):
            result[key] = without_sensitive_fields(field.message_type, value)
        elif field.message_type is not None and isinstance(value, list):
            result[key] = [
                without_sensitive_fields(field.message_type, item) if isinstance(item, dict) else item for item in value
            ]
        else:
            result[key] = value
    return result


def without_sensitive_local_config(data: Mapping[str, Any]) -> dict[str, Any]:
    return without_sensitive_fields(localonly_pb2.LocalConfig.DESCRIPTOR, data)


def without_sensitive_module_config(data: Mapping[str, Any]) -> dict[str, Any]:
    return without_sensitive_fields(localonly_pb2.LocalModuleConfig.DESCRIPTOR, data)


def without_sensitive_channel(data: Mapping[str, Any]) -> dict[str, Any]:
    return without_sensitive_fields(channel_pb2.Channel.DESCRIPTOR, data)


def _sensitive_json_names(descriptor: Descriptor, seen: set[str]) -> set[str]:
    if descriptor.full_name in seen:
        return set()
    seen.add(descriptor.full_name)
    names: set[str] = set()
    for field in descriptor.fields:
        if is_sensitive(field):
            names.add(field.json_name)
        elif field.message_type is not None:
            names |= _sensitive_json_names(field.message_type, seen)
    return names


@cache
def sensitive_json_names() -> frozenset[str]:
    """JSON names of every sensitive field reachable from the config and channel messages."""
    seen: set[str] = set()
    return frozenset(
        name
        for descriptor in (
            localonly_pb2.LocalConfig.DESCRIPTOR,
            localonly_pb2.LocalModuleConfig.DESCRIPTOR,
            channel_pb2.Channel.DESCRIPTOR,
        )
        for name in _sensitive_json_names(descriptor, seen)
    )


@cache
def device_role_attributes(role: str | None) -> dict[str, Any]:
    """Label and description of a device role given by its enum name, as `MessageToDict` yields it."""
    value = _ROLE_ENUM.values_by_name.get(role) if role else None
    if value is None:
        return {}
    metadata = value.GetOptions().Extensions[field_metadata_pb2.enum_value_metadata]
    return {
        f"role_{key}": text for key, text in (("label", metadata.label), ("description", metadata.description)) if text
    }
