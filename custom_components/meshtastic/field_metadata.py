# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

"""Read the protobuf FieldMetadata options from the generated descriptors."""

from __future__ import annotations

from functools import cache
from typing import Any

from .aiomeshtastic.protobuf import config_pb2, field_metadata_pb2

_ROLE_ENUM = config_pb2.Config.DeviceConfig.Role.DESCRIPTOR


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
