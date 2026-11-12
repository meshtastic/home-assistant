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

from custom_components.meshtastic.api import MeshtasticApiClientError
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


@pytest.mark.usefixtures("fake_client")
async def test_entities_attach_to_the_config_entry_and_device_without_a_warning(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert "attach a device to an entity" not in caplog.text
    gateway_device = dr.async_get(hass).async_get_device({(DOMAIN, str(GATEWAY_NUM))})
    registry = er.async_get(hass)
    for unique_id in (
        f"{entry.entry_id}_gateway_{GATEWAY_NUM}",
        f"{entry.entry_id}_channel_{GATEWAY_NUM}_0",
        f"{entry.entry_id}_messages_{GATEWAY_NUM}_dm",
    ):
        entity_id = registry.async_get_entity_id(DOMAIN, DOMAIN, unique_id)
        assert entity_id is not None
        registry_entry = registry.async_get(entity_id)
        assert registry_entry.config_entry_id == entry.entry_id
        assert registry_entry.device_id == gateway_device.id
    assert registry.async_get_entity_id(DOMAIN, DOMAIN, f"{entry.entry_id}_gateway_{GATEWAY_NUM}") == (
        "meshtastic.gateway_gate"
    )


@pytest.mark.usefixtures("fake_client")
async def test_unload_removes_the_entities_and_setup_can_run_again(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    gateway_entity_id = registry.async_get_entity_id(DOMAIN, DOMAIN, f"{entry.entry_id}_gateway_{GATEWAY_NUM}")
    assert hass.states.get(gateway_entity_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get(gateway_entity_id).state == "unavailable"
    assert registry.async_get(gateway_entity_id) is not None

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(gateway_entity_id).state != "unavailable"


@pytest.mark.usefixtures("fake_client")
async def test_failure_after_the_platform_is_set_up_leaves_no_platform_behind(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with patch("custom_components.meshtastic.services.async_register_gateway", side_effect=RuntimeError("boom")):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def test_a_radio_that_stops_answering_mid_setup_retries(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    with patch.object(fake_client, "async_get_all_nodes", side_effect=MeshtasticApiClientError("Node not ready")):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    fake_client.disconnect.assert_awaited_once()


async def test_wind_direction_is_an_angle(hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient) -> None:
    fake_client.nodes[PEER_NUM]["environmentMetrics"] = {"windDirection": 270, "windSpeed": 3.5}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.coordinator.async_set_updated_data(dict(fake_client.nodes))
    await hass.async_block_till_done()

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_sensor_{PEER_NUM}_environment_wind_direction"
    )
    state = hass.states.get(entity_id)
    assert state.attributes["device_class"] == "wind_direction"
    assert state.attributes["state_class"] == "measurement_angle"
    assert state.state == "270"


async def test_selected_node_without_a_long_name_gets_a_device_named_by_its_id(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient
) -> None:
    del fake_client.nodes[PEER_NUM]["user"]["longName"]

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    device = dr.async_get(hass).async_get_device({(DOMAIN, str(PEER_NUM))})
    assert device is not None
    assert device.name == f"!{PEER_NUM:08x}"


async def test_node_without_a_user_record_does_not_break_the_notify_platform(
    hass: HomeAssistant, entry: MockConfigEntry, fake_client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    fake_client.nodes[OTHER_NUM] = {"num": OTHER_NUM, "position": {"latitude": 47.0, "longitude": 8.0}}

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert "Error while setting up meshtastic platform" not in caplog.text
