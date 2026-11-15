# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""The device tracker of a selected node."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers import entity_registry as er

from custom_components.meshtastic.const import DOMAIN

from .conftest import PEER_NUM, make_node

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .conftest import FakeClient


async def test_tracker_has_the_position_as_soon_as_it_is_added(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    """The coordinator already holds the position; the tracker must not wait for the next update to show it."""
    fake_client.nodes[PEER_NUM] = make_node(
        PEER_NUM, "Peer", position={"latitude": 48.137, "longitude": 11.575, "precisionBits": 24}
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "device_tracker", DOMAIN, f"{entry.entry_id}_device_tracker_{PEER_NUM}_node_position"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    assert (state.attributes["latitude"], state.attributes["longitude"]) == (48.137, 11.575)
