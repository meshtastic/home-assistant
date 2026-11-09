# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Node DB reads, coordinator updates and text message events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er

from custom_components.meshtastic import api, helpers
from custom_components.meshtastic.api import MeshtasticApiClient, MeshtasticApiClientError
from custom_components.meshtastic.const import DOMAIN

from .conftest import GATEWAY_NUM, PEER_NUM, FakeClient

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry


def bare_client(hass: HomeAssistant | None = None) -> MeshtasticApiClient:
    client = object.__new__(MeshtasticApiClient)
    client._hass = hass
    client._config_entry_id = "entry"
    client._interface = MagicMock()
    return client


async def test_coordinator_updates_add_only_entities_the_platform_lacks(hass: HomeAssistant) -> None:
    def entity(unique_id: str) -> SimpleNamespace:
        # entity_id stays None until a platform adds the entity, so only unique_id can match
        return SimpleNamespace(unique_id=unique_id, entity_id=None)

    listeners = []
    entry = SimpleNamespace(
        entry_id="entry",
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(async_add_listener=listeners.append)),
    )
    platform = SimpleNamespace(domain="sensor", entities={"sensor.a": entity("a")})
    added = []

    with (
        patch.object(helpers, "get_nodes", return_value={}),
        patch.object(helpers.entity_platform, "async_get_current_platform", return_value=platform),
    ):
        await helpers.setup_platform_entry(
            hass,
            entry,
            lambda entities: added.append([e.unique_id for e in entities]),
            lambda *_: [entity("a"), entity("b")],
        )
        listeners[0]()

    assert added == [["a", "b"], ["b"]]


async def test_a_zero_last_heard_reads_as_unknown(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake_client.nodes[PEER_NUM]["lastHeard"] = 0
    entry.runtime_data.coordinator.async_set_updated_data(dict(fake_client.nodes))
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_sensor_{PEER_NUM}_node_last_heard"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_reading_the_node_db_gives_up_when_the_node_never_gets_ready() -> None:
    client = bare_client()
    client._interface.connected_node_ready.side_effect = lambda: asyncio.Event().wait()

    with patch.object(api, "NODE_READY_TIMEOUT", 0.01), pytest.raises(MeshtasticApiClientError):
        await client.async_get_all_nodes()


@pytest.mark.parametrize(("hop_start", "hops_away"), [(7, 2), (0, None)])
async def test_text_message_events_carry_the_sender_id_and_hop_count(
    hass: HomeAssistant, hop_start: int, hops_away: int | None
) -> None:
    client = bare_client(hass)
    client.get_own_node = lambda: {"num": GATEWAY_NUM}
    mesh_packet = SimpleNamespace(
        id=42,
        hop_start=hop_start,
        hop_limit=5,
        rx_snr=6.25,
        rx_rssi=-90,
        via_mqtt=False,
        xeddsa_signed=False,
        decoded=SimpleNamespace(reply_id=0, emoji=0),
    )
    packet = SimpleNamespace(
        from_id=PEER_NUM, to_id=GATEWAY_NUM, channel_index=0, app_payload="hi", mesh_packet=mesh_packet
    )
    events = []
    hass.bus.async_listen(api.EVENT_MESHTASTIC_API_TEXT_MESSAGE, events.append)

    await client._on_text_message(SimpleNamespace(id=PEER_NUM), packet)
    await hass.async_block_till_done()

    data = events[0].data["data"]
    assert data["from_id"] == f"!{PEER_NUM:08x}"
    assert data["hops_away"] == hops_away
    assert events[0].data["message_id"] == 42
