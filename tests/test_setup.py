# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Entry setup and unload with a fake radio client."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.meshtastic.const import DOMAIN

from .conftest import GATEWAY_NUM, PEER_NUM, FakeClient, make_node

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

OTHER_NUM = 0x33333333


async def test_setup_creates_devices_and_entities_for_the_selected_nodes(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.nodes[OTHER_NUM] = make_node(OTHER_NUM, "Unselected")

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    fake_client.connect.assert_awaited_once()
    assert entry.runtime_data.client is fake_client
    assert entry.runtime_data.gateway_node["num"] == GATEWAY_NUM

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert {ident for d in devices for domain, ident in d.identifiers if domain == DOMAIN} == {
        str(GATEWAY_NUM),
        str(PEER_NUM),
    }
    peer = next(d for d in devices if (DOMAIN, str(PEER_NUM)) in d.identifiers)
    gateway = next(d for d in devices if (DOMAIN, str(GATEWAY_NUM)) in d.identifiers)
    assert peer.name == "Peer"
    assert peer.via_device_id == gateway.id

    unique_ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert f"{entry.entry_id}_gateway_{GATEWAY_NUM}" in unique_ids
    assert f"{entry.entry_id}_channel_{GATEWAY_NUM}_0" in unique_ids
    assert f"{entry.entry_id}_sensor_{PEER_NUM}_node_long_name" in unique_ids
    assert f"meshtastic_node_{PEER_NUM}" in unique_ids
    assert not any(f"_sensor_{OTHER_NUM}_" in unique_id for unique_id in unique_ids)


async def test_unload_disconnects_the_client_and_keeps_the_registries(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    fake_client.disconnect.assert_awaited_once()
    assert dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


async def test_connect_failure_retries_setup(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    fake_client.connect.side_effect = ConnectionError

    assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    fake_client.disconnect.assert_not_awaited()


@pytest.mark.parametrize("disconnect_fails", [False, True])
async def test_failure_after_connect_disconnects_and_clears_runtime_data(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient, *, disconnect_fails: bool
) -> None:
    if disconnect_fails:
        fake_client.disconnect.side_effect = RuntimeError
    with patch("custom_components.meshtastic._setup_meshtastic_devices", side_effect=RuntimeError("boom")):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    fake_client.connect.assert_awaited_once()
    fake_client.disconnect.assert_awaited_once()
    assert getattr(entry, "runtime_data", None) is None
