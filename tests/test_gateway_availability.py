# SPDX-FileCopyrightText: 2026 Meshtastic contributors
#
# SPDX-License-Identifier: MIT

"""Offline tests for gateway link state: fake connection and fake API client, no sockets."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.meshtastic.aiomeshtastic import interface as interface_module
from custom_components.meshtastic.aiomeshtastic.interface import ConnectionState, MeshInterface
from custom_components.meshtastic.const import CONF_OPTION_FILTER_NODES
from custom_components.meshtastic.coordinator import GATEWAY_LINK_GRACE_PERIOD, MeshtasticDataUpdateCoordinator
from custom_components.meshtastic.entity import MeshtasticNodeEntity

NODE = 1234


class FakeConnection:
    def __init__(self, *, reconnect_failures: int = 0) -> None:
        self.reconnect_failures = reconnect_failures
        self.reconnect_calls = 0

    async def reconnect(self, *, force: bool = False) -> bool:  # noqa: ARG002
        self.reconnect_calls += 1
        if self.reconnect_calls <= self.reconnect_failures:
            raise ConnectionError
        return True

    async def request_config(self, minimal: bool = False, on_packet: object = None) -> bool:  # noqa: ARG002, FBT001, FBT002
        return True


def running_interface(connection: FakeConnection, monkeypatch: pytest.MonkeyPatch) -> MeshInterface:
    monkeypatch.setattr(interface_module.random, "randint", lambda _a, _b: 0)
    interface = MeshInterface(connection=connection, enable_mqtt_proxy=False)
    interface._is_running.set()
    return interface


async def test_interface_reports_reconnect_then_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    interface = running_interface(FakeConnection(), monkeypatch)
    states: list[ConnectionState] = []
    interface.add_connection_state_listener(states.append)

    await interface._reconnect_while_running()

    assert states == [ConnectionState.RECONNECTING, ConnectionState.CONNECTED]
    assert interface.connection_state == ConnectionState.CONNECTED


async def test_interface_reports_each_state_once_across_failed_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = FakeConnection(reconnect_failures=9)
    interface = running_interface(connection, monkeypatch)
    states: list[ConnectionState] = []
    interface.add_connection_state_listener(states.append)

    await interface._reconnect_while_running()

    assert connection.reconnect_calls == 10
    assert states == [ConnectionState.RECONNECTING, ConnectionState.FAILED, ConnectionState.CONNECTED]


async def test_interface_listener_can_be_removed_and_cannot_break_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    interface = running_interface(FakeConnection(), monkeypatch)
    states: list[ConnectionState] = []

    def broken(_state: ConnectionState) -> None:
        raise RuntimeError

    interface.add_connection_state_listener(broken)
    remove = interface.add_connection_state_listener(states.append)
    remove()

    await interface._reconnect_while_running()

    assert states == []
    assert interface.connection_state == ConnectionState.CONNECTED


class FakeClient:
    def __init__(self) -> None:
        self.nodes: dict[int, dict[str, Any]] = {NODE: {"num": NODE, "user": {"longName": "n"}}}
        self.calls = 0
        self.fail = False

    async def async_get_all_nodes(self) -> dict[int, dict[str, Any]]:
        self.calls += 1
        return self.nodes


class Sensor(MeshtasticNodeEntity):
    def _async_update_attrs(self) -> None:
        pass


@pytest.fixture
async def coordinator(hass: HomeAssistant) -> AsyncIterator[tuple[MeshtasticDataUpdateCoordinator, FakeClient]]:
    entry = MockConfigEntry(domain="meshtastic", options={CONF_OPTION_FILTER_NODES: [{"id": NODE}]})
    client = FakeClient()
    entry.runtime_data = SimpleNamespace(client=client)
    coordinator = MeshtasticDataUpdateCoordinator(hass)
    coordinator.config_entry = entry
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    yield coordinator, client
    await coordinator.async_shutdown()


async def advance(hass: HomeAssistant, delta: timedelta) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + delta)
    await hass.async_block_till_done()


async def test_short_drop_within_grace_keeps_entities_available(
    hass: HomeAssistant,
    coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, _client = coordinator
    caplog.set_level(logging.DEBUG)

    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD / 2)
    coordinator.async_connection_state_changed(ConnectionState.CONNECTED)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD * 2)

    assert coordinator.last_update_success
    assert not [r for r in caplog.records if r.levelno >= logging.INFO and "gateway" in r.getMessage().lower()]


async def test_long_drop_marks_unavailable_and_logs_once(
    hass: HomeAssistant,
    coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, _client = coordinator
    entity = Sensor(coordinator, {}, NODE, "sensor", EntityDescription(key="k"))
    assert entity.available

    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    coordinator.async_connection_state_changed(ConnectionState.FAILED)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD + timedelta(seconds=1))
    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    coordinator.async_connection_state_changed(ConnectionState.FAILED)

    assert not coordinator.last_update_success
    assert not entity.available
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "gateway" in r.getMessage().lower()]
    assert len(warnings) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_poll_does_not_revive_entities_from_cached_nodes_while_down(
    hass: HomeAssistant, coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient]
) -> None:
    coordinator, client = coordinator
    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD + timedelta(seconds=1))
    calls = client.calls

    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert client.calls == calls


async def test_poll_during_grace_neither_flips_nor_reads_the_cache(
    coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient],
) -> None:
    coordinator, client = coordinator
    calls = client.calls
    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert client.calls == calls


async def test_events_are_ignored_while_unavailable(
    hass: HomeAssistant, coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient]
) -> None:
    coordinator, _client = coordinator
    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD + timedelta(seconds=1))

    hass.bus.async_fire(
        "meshtastic_api_position",
        {"config_entry_id": coordinator.config_entry.entry_id, "node": NODE, "data": {"latitude": 1.0}},
    )
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert "position" not in coordinator.data[NODE]


async def test_reconnect_refreshes_data_and_logs_recovery_once(
    hass: HomeAssistant,
    coordinator: tuple[MeshtasticDataUpdateCoordinator, FakeClient],
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator, client = coordinator
    entity = Sensor(coordinator, {}, NODE, "sensor", EntityDescription(key="k"))
    coordinator.async_connection_state_changed(ConnectionState.RECONNECTING)
    await advance(hass, GATEWAY_LINK_GRACE_PERIOD + timedelta(seconds=1))
    assert not entity.available
    client.nodes = {NODE: {"num": NODE, "user": {"longName": "renamed"}}}
    caplog.set_level(logging.INFO)
    caplog.clear()

    coordinator.async_connection_state_changed(ConnectionState.CONNECTED)
    await hass.async_block_till_done()
    await asyncio.sleep(0)

    assert coordinator.last_update_success
    assert entity.available
    assert coordinator.data[NODE]["user"]["longName"] == "renamed"
    recoveries = [r for r in caplog.records if r.levelno == logging.INFO and "restored" in r.getMessage()]
    assert len(recoveries) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    coordinator.async_connection_state_changed(ConnectionState.CONNECTED)
    await hass.async_block_till_done()
    assert len([r for r in caplog.records if "restored" in r.getMessage()]) == 1
