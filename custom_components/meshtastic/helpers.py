# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import typing

import aiohttp
from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.hass_dict import HassKey

from .const import CONF_OPTION_FILTER_NODES, DOMAIN, LOGGER

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from typing import Any

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import MeshtasticConfigEntry, MeshtasticData


def get_nodes(entry: MeshtasticConfigEntry) -> typing.Mapping[int, typing.Mapping[str, Any]]:
    filter_nodes = entry.options.get(CONF_OPTION_FILTER_NODES, [])
    filter_node_nums = [el["id"] for el in filter_nodes]
    if not entry.runtime_data.coordinator.data:
        return {}

    return {
        node_num: node_info
        for node_num, node_info in entry.runtime_data.coordinator.data.items()
        if node_num in filter_node_nums
    }


DATA_HARDWARE_NAMES: HassKey[typing.Mapping[str, str]] = HassKey(f"{DOMAIN}_hardware_names")

# setup waits on this fetch, and without it devices only lack a model name
_HARDWARE_NAMES_TIMEOUT_SECONDS = 10


async def setup_platform_entry(
    hass: HomeAssistant,  # noqa: ARG001 function argument: `hass`
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
    entity_factory: Callable[[typing.Mapping[int, typing.Mapping[str, Any]], MeshtasticData], Iterable[Entity]],
) -> None:
    async_add_entities(entity_factory(get_nodes(entry), entry.runtime_data))
    platform = entity_platform.async_get_current_platform()

    def on_coordinator_data_update() -> None:
        entities = entity_factory(get_nodes(entry), entry.runtime_data)
        existing_unique_ids = {entity.unique_id for entity in platform.entities.values()}
        new_entities = [s for s in entities if s.unique_id not in existing_unique_ids]
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(entry.runtime_data.coordinator.async_add_listener(on_coordinator_data_update))


async def fetch_meshtastic_hardware_names(hass: HomeAssistant) -> typing.Mapping[str, str]:
    # one successful fetch serves every entry and reload until Home Assistant restarts
    if DATA_HARDWARE_NAMES in hass.data:
        return hass.data[DATA_HARDWARE_NAMES]
    try:
        session = async_get_clientsession(hass)
        async with session.get(
            "https://api.meshtastic.org/resource/deviceHardware",
            raise_for_status=True,
            timeout=aiohttp.ClientTimeout(total=_HARDWARE_NAMES_TIMEOUT_SECONDS),
        ) as response:
            response_json = await response.json()
            device_hardware_names = {h["hwModelSlug"]: h["displayName"] for h in response_json}
            hass.data[DATA_HARDWARE_NAMES] = device_hardware_names
    except Exception:  # noqa: BLE001
        LOGGER.info("Failed to fetch meshtastic hardware infos", exc_info=True)
        device_hardware_names = {}
    return device_hardware_names
