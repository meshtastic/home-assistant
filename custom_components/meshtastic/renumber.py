# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Follow nodes whose number changed: firmware 2.8 derives it from the public key."""

from __future__ import annotations

import base64
import binascii
import re
import zlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def canonical_node_num(public_key: str | None) -> int | None:
    if not public_key:
        return None
    try:
        key = base64.b64decode(public_key, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not key:
        return None
    return zlib.crc32(key) & 0xFFFFFFFF


def node_public_key(node: Mapping[str, Any] | None) -> str | None:
    if not node:
        return None
    return node.get("user", {}).get("publicKey") or None


def find_renumbered_nodes(
    known_keys: Mapping[int, str | None], nodes: Mapping[int, Mapping[str, Any]]
) -> dict[int, int]:
    # a peer is only followed to the crc32 of its own key, so a node reusing a key cannot capture a device
    canonical_by_key = {}
    for num, node in nodes.items():
        key = node_public_key(node)
        if key and canonical_node_num(key) == num:
            canonical_by_key[key] = num

    renumbered = {}
    for old_num, stored_key in known_keys.items():
        key = stored_key or node_public_key(nodes.get(old_num))
        new_num = canonical_by_key.get(key) if key else None
        if new_num is not None and new_num != old_num:
            renumbered[old_num] = new_num
    return renumbered


def rewrite_unique_id(unique_id: str, renumbered: Mapping[int, int]) -> str:
    for old_num, new_num in renumbered.items():
        unique_id = re.sub(rf"(?<=_){old_num}(?=_|$)", str(new_num), unique_id)
    return unique_id
