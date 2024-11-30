from __future__ import annotations

import typing
from collections import defaultdict

from homeassistant.helpers import entity_platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_OPTION_FILTER_NODES, LOGGER

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
    return {
        node_num: node_info
        for node_num, node_info in entry.runtime_data.coordinator.data.items()
        if node_num in filter_node_nums
    }


_remove_listeners = defaultdict(lambda: defaultdict(list))


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
        new_entities = [s for s in entities if s.entity_id not in platform.entities]
        if new_entities:
            async_add_entities(new_entities)

    remove_listener = entry.runtime_data.coordinator.async_add_listener(on_coordinator_data_update)
    _remove_listeners[platform.domain][entry.entry_id].append(remove_listener)


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: MeshtasticConfigEntry,
) -> bool:
    platform = entity_platform.async_get_current_platform()
    for remove_listener in _remove_listeners[platform.domain].pop(entry.entry_id, []):
        remove_listener()

    return True


async def fetch_meshtastic_hardware_names(hass: HomeAssistant) -> typing.Mapping[str, str]:
    try:
        session = async_get_clientsession(hass)
        async with session.get("https://api.meshtastic.org/resource/deviceHardware", raise_for_status=True) as response:
            response_json = await response.json()
            device_hardware_names = {h["hwModelSlug"]: h["displayName"] for h in response_json}
    except Exception:  # noqa: BLE001
        LOGGER.info("Failed to fetch meshtastic hardware infos", exc_info=True)
        device_hardware_names = {}
    return device_hardware_names
