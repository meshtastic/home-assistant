# SPDX-FileCopyrightText: 2026 ozcet
#
# SPDX-License-Identifier: MIT

import pytest

from custom_components.meshtastic.aiomeshtastic import MeshInterface


@pytest.mark.parametrize("user", [None, {}, {"id": "!12345678"}, {"id": ""}, {"longName": "Peer"}])
def test_find_node_with_partial_metadata(user: dict[str, str] | None) -> None:
    """Keep node lookups safe and user IDs stable when user metadata is absent or partial."""
    interface = MeshInterface(connection=None, enable_mqtt_proxy=False)
    node = {"num": 1}
    if user is not None:
        node["user"] = user
    interface._node_database = {1: node}

    found = interface.find_node(node_id=1)

    assert found is not None
    assert found.user_id == ((user or {}).get("id") or "!00000001")
    assert isinstance(found.user_id, str)
    assert found.long_name == (user or {}).get("longName", "")
    assert found.short_name == ""
    assert interface.find_node(user_id="missing") is None
    assert interface.find_node(short_name="missing") is None
    assert interface.find_node(long_name="missing") is None
