# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Following nodes whose number changed (firmware 2.8: crc32 of the public key)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.meshtastic.const import CONF_OPTION_FILTER_NODES, DOMAIN
from custom_components.meshtastic.renumber import (
    canonical_node_num,
    find_renumbered_nodes,
    is_same_gateway,
    rewrite_unique_id,
)

from .conftest import GATEWAY_KEY, GATEWAY_NUM, PEER_KEY, PEER_NUM, FakeClient, canonical, make_entry, make_node

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

NEW_GATEWAY = canonical(GATEWAY_KEY)
NEW_PEER = canonical(PEER_KEY)


def test_canonical_node_num_is_crc32_of_the_decoded_key() -> None:
    assert canonical_node_num(PEER_KEY) == NEW_PEER
    assert canonical_node_num(None) is None
    assert canonical_node_num("not base64!") is None


def test_peer_follows_only_to_the_crc32_of_its_own_key() -> None:
    nodes = {
        NEW_PEER: make_node(NEW_PEER, "Peer", PEER_KEY),
        # a node squatting on the key at a number that is not its crc32 captures nothing
        0x44444444: make_node(0x44444444, "Squatter", PEER_KEY),
    }

    assert find_renumbered_nodes({PEER_NUM: PEER_KEY}, nodes) == {PEER_NUM: NEW_PEER}
    assert find_renumbered_nodes({PEER_NUM: PEER_KEY}, {0x44444444: nodes[0x44444444]}) == {}
    assert find_renumbered_nodes({NEW_PEER: PEER_KEY}, nodes) == {}


def test_gateway_identity_prefers_the_key_over_the_number() -> None:
    assert is_same_gateway(GATEWAY_NUM, GATEWAY_KEY, make_node(NEW_GATEWAY, "Gateway", GATEWAY_KEY))
    assert not is_same_gateway(GATEWAY_NUM, GATEWAY_KEY, make_node(GATEWAY_NUM, "Gateway", PEER_KEY))
    assert is_same_gateway(GATEWAY_NUM, None, make_node(GATEWAY_NUM, "Gateway"))
    assert not is_same_gateway(GATEWAY_NUM, None, make_node(PEER_NUM, "Peer"))


def test_unique_ids_are_rewritten_by_whole_token_only() -> None:
    renumbered = {5: 9}

    assert rewrite_unique_id("abc_sensor_5_node_snr", renumbered) == "abc_sensor_9_node_snr"
    assert rewrite_unique_id("abc_gateway_5", renumbered) == "abc_gateway_9"
    assert rewrite_unique_id("abc_sensor_55_node_snr", renumbered) == "abc_sensor_55_node_snr"
    assert rewrite_unique_id("5_sensor", renumbered) == "5_sensor"


async def test_setup_moves_devices_entities_and_options_to_the_new_numbers(
    hass: HomeAssistant, fake_client: FakeClient
) -> None:
    entry = make_entry(
        {
            CONF_OPTION_FILTER_NODES: [
                {"id": GATEWAY_NUM, "name": "Gateway"},
                {"id": PEER_NUM, "name": "Peer", "public_key": PEER_KEY},
            ]
        }
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    devices = {
        num: device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, str(num))}, name=name
        )
        for num, name in ((GATEWAY_NUM, "Gateway"), (PEER_NUM, "Peer"))
    }
    seeded = {
        "sensor.old_peer_snr": ("sensor", "meshtastic", f"{entry.entry_id}_sensor_{PEER_NUM}_node_snr", PEER_NUM),
        "meshtastic.old_gateway": ("meshtastic", "meshtastic", f"{entry.entry_id}_gateway_{GATEWAY_NUM}", GATEWAY_NUM),
        "meshtastic.old_channel": (
            "meshtastic",
            "meshtastic",
            f"{entry.entry_id}_channel_{GATEWAY_NUM}_0",
            GATEWAY_NUM,
        ),
        "notify.old_peer": ("notify", "meshtastic", f"meshtastic_node_{PEER_NUM}", PEER_NUM),
    }
    for entity_id, (domain, platform, unique_id, num) in seeded.items():
        entity_registry.async_get_or_create(
            domain,
            platform,
            unique_id,
            suggested_object_id=entity_id.split(".")[1],
            config_entry=entry,
            device_id=devices[num].id,
        )

    # the radio now reports both nodes at crc32(key); the gateway is the connection, the peer is found by key
    fake_client.nodes = {
        NEW_GATEWAY: make_node(NEW_GATEWAY, "Gateway", GATEWAY_KEY),
        NEW_PEER: make_node(NEW_PEER, "Peer", PEER_KEY),
    }
    fake_client.gateway_num = NEW_GATEWAY

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == str(NEW_GATEWAY)
    assert entry.options[CONF_OPTION_FILTER_NODES] == [
        {"id": NEW_GATEWAY, "name": "Gateway", "public_key": GATEWAY_KEY},
        {"id": NEW_PEER, "name": "Peer", "public_key": PEER_KEY},
    ]

    # the same device rows moved: ids kept, identifiers rewritten, nothing left behind
    moved = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert {d.id for d in moved} == {d.id for d in devices.values()}
    assert {ident for d in moved for _, ident in d.identifiers} == {str(NEW_GATEWAY), str(NEW_PEER)}

    # the same entity rows moved, so entity ids survive and the platforms attach to them instead of duplicating
    for entity_id, (domain, platform, unique_id, _) in seeded.items():
        new_unique_id = unique_id.replace(str(GATEWAY_NUM), str(NEW_GATEWAY)).replace(str(PEER_NUM), str(NEW_PEER))
        assert entity_registry.async_get_entity_id(domain, platform, unique_id) is None
        assert entity_registry.async_get_entity_id(domain, platform, new_unique_id) == entity_id

    all_unique_ids = [
        e.unique_id for e in entity_registry.entities.values() if e.platform == DOMAIN or e.config_entry_id
    ]
    assert len(all_unique_ids) == len(set(all_unique_ids))
    assert not any(str(old) in unique_id for unique_id in all_unique_ids for old in (GATEWAY_NUM, PEER_NUM))


async def test_a_renumber_onto_an_existing_device_is_not_merged(hass: HomeAssistant, fake_client: FakeClient) -> None:
    entry = make_entry({CONF_OPTION_FILTER_NODES: [{"id": PEER_NUM, "name": "Peer", "public_key": PEER_KEY}]})
    entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    old = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, str(PEER_NUM))}, name="Peer"
    )
    new = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, str(NEW_PEER))}, name="Peer"
    )
    fake_client.nodes = {
        GATEWAY_NUM: make_node(GATEWAY_NUM, "Gateway", GATEWAY_KEY),
        NEW_PEER: make_node(NEW_PEER, "Peer", PEER_KEY),
    }

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert device_registry.async_get_device_by_identifier((DOMAIN, str(PEER_NUM)), entry.entry_id).id == old.id
    assert device_registry.async_get_device_by_identifier((DOMAIN, str(NEW_PEER)), entry.entry_id).id == new.id
