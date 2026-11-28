# SPDX-FileCopyrightText: 2026 Meshtastic Home Assistant contributors
#
# SPDX-License-Identifier: MIT

"""Channel notify entities: one per channel (key and name), and the move off the key-only unique id."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_component import DATA_INSTANCES

from custom_components.meshtastic.const import (
    CONF_OPTION_FILTER_NODES,
    CONF_OPTION_NOTIFY_PLATFORM,
    CONF_OPTION_NOTIFY_PLATFORM_CHANNELS,
    CONF_OPTION_NOTIFY_PLATFORM_NODES,
    DOMAIN,
)
from custom_components.meshtastic.notify import (
    CHANNEL_UNIQUE_ID_PREFIX,
    MeshtasticChannelNotify,
    _channel_global_id,
    _legacy_channel_global_id,
)

from .conftest import GATEWAY_NUM, FakeClient, make_entry, make_node, make_nodes

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall
    from pytest_homeassistant_custom_component.common import MockConfigEntry

DEFAULT_KEY = "AQ=="
OTHER_KEY = "Ag=="


def channel(index: int, name: str, psk: str, role: str | None = None) -> dict[str, Any]:
    return {
        "index": index,
        "role": role or ("PRIMARY" if index == 0 else "SECONDARY"),
        "settings": {"name": name, "psk": psk, "uplinkEnabled": False, "downlinkEnabled": False},
    }


B_NUM = 0x33333333

MESH = [
    channel(0, "Alpha", DEFAULT_KEY),
    channel(1, "Bravo", DEFAULT_KEY),
    channel(2, "Charlie", DEFAULT_KEY),
    channel(3, "Delta", OTHER_KEY),
    channel(4, "Echo", OTHER_KEY),
]


def channels_entry() -> MockConfigEntry:
    return make_entry(
        {
            CONF_OPTION_FILTER_NODES: [{"id": GATEWAY_NUM, "name": "Gateway"}],
            CONF_OPTION_NOTIFY_PLATFORM: {
                CONF_OPTION_NOTIFY_PLATFORM_CHANNELS: True,
                CONF_OPTION_NOTIFY_PLATFORM_NODES: "none",
            },
        }
    )


def firmware_id(ch: dict[str, Any]) -> str:
    """Return what the client reports for a channel: its name, else the modem preset (LongFast here)."""
    return ch["settings"]["name"] or "LongFast"


def channel_unique_id(ch: dict[str, Any]) -> str:
    return f"{CHANNEL_UNIQUE_ID_PREFIX}{_channel_global_id(ch, firmware_id(ch))}"


def legacy_unique_id(ch: dict[str, Any]) -> str:
    return f"{CHANNEL_UNIQUE_ID_PREFIX}{_legacy_channel_global_id(ch)}"


def serve_channels(client: FakeClient, chs: list) -> None:
    client.async_get_channels = AsyncMock(return_value=chs)
    client.async_get_channel_ids = AsyncMock(return_value={c["index"]: firmware_id(c) for c in chs})


async def setup_channels(hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient, chs: list) -> None:
    serve_channels(fake_client, chs)
    if entry.entry_id not in {e.entry_id for e in hass.config_entries.async_entries(DOMAIN)}:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def channel_entities(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    registry = er.async_get(hass)
    return {
        e.unique_id: e
        for e in registry.entities.values()
        if e.domain == "notify" and e.unique_id.startswith(CHANNEL_UNIQUE_ID_PREFIX)
    }


def legacy_entry(
    hass: HomeAssistant, entry: MockConfigEntry, ch: dict[str, Any], original_name: str, object_id: str
) -> er.RegistryEntry:
    return er.async_get(hass).async_get_or_create(
        "notify",
        DOMAIN,
        legacy_unique_id(ch),
        config_entry=entry,
        original_name=original_name,
        suggested_object_id=object_id,
    )


def test_channels_sharing_a_key_get_distinct_ids_and_the_same_channel_the_same_id() -> None:
    ids = {_channel_global_id(c, firmware_id(c)) for c in MESH}
    assert len(ids) == len(MESH)
    alpha = channel(0, "Alpha", DEFAULT_KEY)
    assert _channel_global_id(alpha, "Alpha") == _channel_global_id(channel(7, "Alpha", DEFAULT_KEY), "Alpha")
    assert _channel_global_id(alpha, "Alpha") != _channel_global_id(channel(0, "Alpha", OTHER_KEY), "Alpha")


def test_the_new_id_never_equals_the_key_only_id() -> None:
    assert {_channel_global_id(c, firmware_id(c)) for c in MESH}.isdisjoint(
        {_legacy_channel_global_id(c) for c in MESH}
    )
    assert _legacy_channel_global_id(MESH[0]) == _legacy_channel_global_id(MESH[1])


def test_unnamed_channels_are_identified_by_key_and_modem_preset_whatever_their_role() -> None:
    primary = channel(0, "", OTHER_KEY, "PRIMARY")
    secondary = channel(1, "", OTHER_KEY, "SECONDARY")
    assert _channel_global_id(primary, "LongFast") == _channel_global_id(secondary, "LongFast")
    assert _channel_global_id(primary, "LongFast") != _channel_global_id(primary, "MediumFast")
    assert _channel_global_id(primary, "LongFast") != _channel_global_id(channel(0, "", DEFAULT_KEY), "LongFast")


async def test_unnamed_channels_are_named_after_their_key_and_role(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = channels_entry()
    unnamed = [channel(0, "", DEFAULT_KEY), channel(1, "", OTHER_KEY)]
    await setup_channels(hass, entry, fake_client, unnamed)

    entities = channel_entities(hass)
    names = {hass.states.get(e.entity_id).attributes["friendly_name"] for e in entities.values()}
    assert names == {"Channel Primary Default", "Channel Secondary"}


async def test_every_channel_gets_its_own_entity(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = channels_entry()
    await setup_channels(hass, entry, fake_client, MESH)

    entities = channel_entities(hass)
    assert set(entities) == {channel_unique_id(c) for c in MESH}
    for ch in MESH:
        state = hass.states.get(entities[channel_unique_id(ch)].entity_id)
        assert state is not None
        assert state.attributes["gateways"] == {GATEWAY_NUM: ch["index"]}
        assert state.attributes["friendly_name"] == f"Channel {ch['settings']['name']}"


async def test_the_entry_for_a_key_moves_to_the_channel_it_was_named_for(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = channels_entry()
    entry.add_to_hass(hass)
    old = legacy_entry(hass, entry, MESH[0], "Channel Alpha", "mesh_channel_longfast")
    other = legacy_entry(hass, entry, MESH[3], "Channel Echo", "mesh_channel_delta")

    await setup_channels(hass, entry, fake_client, MESH)

    entities = channel_entities(hass)
    assert entities[channel_unique_id(MESH[0])].entity_id == old.entity_id
    assert entities[channel_unique_id(MESH[4])].entity_id == other.entity_id
    assert legacy_unique_id(MESH[0]) not in entities
    assert legacy_unique_id(MESH[3]) not in entities
    assert set(entities) == {channel_unique_id(c) for c in MESH}
    assert hass.states.get(old.entity_id).attributes["gateways"] == {GATEWAY_NUM: 0}


@pytest.mark.parametrize("order", [list(range(5)), [4, 3, 2, 1, 0], [2, 0, 4, 1, 3]])
async def test_the_order_of_the_channels_does_not_change_which_entity_a_channel_keeps(
    hass: HomeAssistant, fake_client: FakeClient, order: list[int]
) -> None:
    entry = channels_entry()
    entry.add_to_hass(hass)
    old = legacy_entry(hass, entry, MESH[1], "Channel Bravo", "mesh_channel_bravo")

    await setup_channels(hass, entry, fake_client, [MESH[i] for i in order])

    assert channel_entities(hass)[channel_unique_id(MESH[1])].entity_id == old.entity_id


async def test_an_entry_whose_name_matches_no_channel_is_left_alone_like_any_renamed_channel(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = channels_entry()
    entry.add_to_hass(hass)
    old = legacy_entry(hass, entry, MESH[0], "Channel Gone", "mesh_channel_gone")

    await setup_channels(hass, entry, fake_client, MESH[:3])

    entities = channel_entities(hass)
    assert entities[legacy_unique_id(MESH[0])].entity_id == old.entity_id
    assert {channel_unique_id(c) for c in MESH[:3]} <= set(entities)


async def test_an_entry_of_another_gateway_moves_to_the_channel_with_the_same_name(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = channels_entry()
    entry.add_to_hass(hass)
    other_gateway = make_entry()
    other_gateway.add_to_hass(hass)
    old = legacy_entry(hass, other_gateway, MESH[0], "Channel Alpha", "mesh_channel_alpha")

    await setup_channels(hass, entry, fake_client, MESH[:3])

    assert channel_entities(hass)[channel_unique_id(MESH[0])].entity_id == old.entity_id


async def test_loading_again_keeps_the_same_entities(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = channels_entry()
    entry.add_to_hass(hass)
    legacy_entry(hass, entry, MESH[0], "Channel Alpha", "mesh_channel_longfast")
    await setup_channels(hass, entry, fake_client, MESH)
    before = {uid: e.entity_id for uid, e in channel_entities(hass).items()}

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert {uid: e.entity_id for uid, e in channel_entities(hass).items()} == before


def routes_of(hass: HomeAssistant, unique_id: str) -> dict[int, int]:
    entity_id = er.async_get(hass).async_get_entity_id("notify", DOMAIN, unique_id)
    return dict(hass.data[DATA_INSTANCES]["notify"].get_entity(entity_id).gateways)


async def test_a_gateway_that_renames_a_channel_it_shares_leaves_no_stale_route(hass: HomeAssistant) -> None:
    on_a = channel(1, "Alpha", DEFAULT_KEY)
    on_b = channel(0, "Alpha", DEFAULT_KEY)
    client_a = FakeClient(make_nodes(), GATEWAY_NUM)
    client_b = FakeClient({B_NUM: make_node(B_NUM, "Gateway B")}, B_NUM)
    serve_channels(client_a, [on_a])
    serve_channels(client_b, [on_b])
    entry_a = channels_entry()
    entry_b = make_entry(
        {
            CONF_OPTION_FILTER_NODES: [{"id": B_NUM, "name": "Gateway B"}],
            CONF_OPTION_NOTIFY_PLATFORM: {
                CONF_OPTION_NOTIFY_PLATFORM_CHANNELS: True,
                CONF_OPTION_NOTIFY_PLATFORM_NODES: "none",
            },
        }
    )
    clients = {entry_a.entry_id: client_a, entry_b.entry_id: client_b}
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with (
        patch(
            "custom_components.meshtastic.MeshtasticApiClient",
            side_effect=lambda *_, config_entry_id, **__: clients[config_entry_id],
        ),
        patch("custom_components.meshtastic.fetch_meshtastic_hardware_names", AsyncMock(return_value={})),
    ):
        assert await hass.config_entries.async_setup(entry_a.entry_id)
        await hass.async_block_till_done()
        assert routes_of(hass, channel_unique_id(on_a)) == {GATEWAY_NUM: 1, B_NUM: 0}

        renamed = channel(0, "Beta", DEFAULT_KEY)
        serve_channels(client_b, [renamed])
        assert await hass.config_entries.async_reload(entry_b.entry_id)
        await hass.async_block_till_done()

    assert routes_of(hass, channel_unique_id(on_a)) == {GATEWAY_NUM: 1}
    assert routes_of(hass, channel_unique_id(renamed)) == {B_NUM: 0}


async def test_a_send_still_fails_over_when_a_gateway_loses_its_route_meanwhile(hass: HomeAssistant) -> None:
    entity = MeshtasticChannelNotify(channel(0, "Alpha", DEFAULT_KEY), "Alpha", gateway_node_id=1)
    entity.add_gateway(2, 0)
    entity.add_gateway(3, 0)
    entity.hass = hass
    tried: list[int] = []

    async def fail_and_drop_gateway_2(call: ServiceCall) -> None:
        tried.append(call.data["from"])
        entity.remove_gateway(2)
        raise HomeAssistantError

    hass.services.async_register(DOMAIN, "send_text", fail_and_drop_gateway_2)

    with pytest.raises(ServiceValidationError):
        await entity.async_send_message("hi")

    assert tried == [1, 3]
