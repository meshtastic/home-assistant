# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.helpers.entity_component import EntityComponent
    from homeassistant.loader import Integration

    from .api import MeshtasticApiClient
    from .coordinator import MeshtasticDataUpdateCoordinator
    from .entity import MeshtasticEntity


type MeshtasticConfigEntry = ConfigEntry[MeshtasticData]


@dataclass
class MeshtasticData:
    client: MeshtasticApiClient
    coordinator: MeshtasticDataUpdateCoordinator
    integration: Integration
    gateway_node: Mapping[str, Any]


DATA_COMPONENT: HassKey[EntityComponent[MeshtasticEntity]] = HassKey(DOMAIN)
