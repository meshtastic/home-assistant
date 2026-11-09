# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.text import (
    DOMAIN as TEXT_DOMAIN,
)
from homeassistant.components.text import (
    TextEntity,
    TextEntityDescription,
)

from . import helpers
from .entity import MeshtasticNodeEntity
from .field_metadata import device_role_attributes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import MeshtasticDataUpdateCoordinator
    from .data import MeshtasticConfigEntry, MeshtasticData


def _build_texts(nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData) -> Iterable[MeshtasticText]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    entities = []
    entities += [
        MeshtasticText(
            coordinator=coordinator,
            entity_description=MeshtasticTextEntityDescription(
                key="node_role",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("role", None),
                attributes_fn=lambda device: dict(
                    device_role_attributes(device.coordinator.data[device.node_id].get("user", {}).get("role", None))
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]
    entities += [
        MeshtasticText(
            coordinator=coordinator,
            entity_description=MeshtasticTextEntityDescription(
                key="node_short_name",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("shortName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    entities += [
        MeshtasticText(
            coordinator=coordinator,
            entity_description=MeshtasticTextEntityDescription(
                key="node_long_name",
                icon="mdi:card-account-details",
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("longName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await helpers.setup_platform_entry(hass, entry, async_add_entities, _build_texts)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    return await helpers.async_unload_entry(hass, entry)


@dataclass(kw_only=True)
class MeshtasticTextEntityDescription(TextEntityDescription):
    value_fn: Callable[[MeshtasticText], str]
    attributes_fn: Callable[[MeshtasticText], dict[str, Any]] | None = None


class MeshtasticText(MeshtasticNodeEntity, TextEntity):
    entity_description: MeshtasticTextEntityDescription

    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        entity_description: MeshtasticTextEntityDescription,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
    ) -> None:
        super().__init__(coordinator, gateway, node_id, TEXT_DOMAIN, entity_description)

    def _async_update_attrs(self) -> None:
        self._attr_native_value = self.entity_description.value_fn(self)
        if self.entity_description.attributes_fn is not None:
            self._attr_extra_state_attributes = self.entity_description.attributes_fn(self)
