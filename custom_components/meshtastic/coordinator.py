# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from functools import wraps
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID,
    ATTR_EVENT_MESHTASTIC_API_DATA,
    ATTR_EVENT_MESHTASTIC_API_NODE,
    EVENT_MESHTASTIC_API_NODE_UPDATED,
    EVENT_MESHTASTIC_API_POSITION,
    EVENT_MESHTASTIC_API_TELEMETRY,
    EventMeshtasticApiTelemetryType,
    MeshtasticApiClientError,
)
from .const import CONF_OPTION_FILTER_NODES, DOMAIN, LOGGER

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import Event, HomeAssistant, _DataT

    from .data import MeshtasticConfigEntry


def meshtastic_api_event_callback(f):  # noqa: ANN001, ANN201
    @wraps(f)
    async def wrapper(self: MeshtasticDataUpdateCoordinator, event: Event[_DataT]):  # noqa: ANN202
        try:
            if self.config_entry is None:
                return None

            event_data = deepcopy(event.data)
            config_entry_id = event_data.pop(ATTR_EVENT_MESHTASTIC_API_CONFIG_ENTRY_ID, None)
            if config_entry_id != self.config_entry.entry_id:
                return None

            if not self.data:
                self._logger.debug("Received event but coordinator is not yet initialized")
                return None

            node_id = event_data.get(ATTR_EVENT_MESHTASTIC_API_NODE, None)
            if node_id is None or node_id not in self.data:
                self._logger.debug("Node %d not in coordinator data", node_id)
                return None

            data = event_data.get(ATTR_EVENT_MESHTASTIC_API_DATA, None)
            if data is None:
                self._logger.debug("Event did not contain data")
                return None

            additional_event_data = {
                k: v
                for k, v in event_data.items()
                if k not in [ATTR_EVENT_MESHTASTIC_API_NODE, ATTR_EVENT_MESHTASTIC_API_DATA]
            }

            return await f(self, node_id, data, **additional_event_data)
        except:  # noqa: E722
            self._logger.warning("Failed to handle meshtastic api event", exc_info=True)

    return wrapper


class MeshtasticDataUpdateCoordinator(DataUpdateCoordinator):
    config_entry: MeshtasticConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
    ) -> None:
        super().__init__(
            hass=hass,
            logger=LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
        )
        self._logger = LOGGER.getChild(self.__class__.__name__)
        self._remove_event_listeners = []
        self._remove_event_listeners.append(
            hass.bus.async_listen(EVENT_MESHTASTIC_API_NODE_UPDATED, self._api_node_updated)
        )
        self._remove_event_listeners.append(hass.bus.async_listen(EVENT_MESHTASTIC_API_TELEMETRY, self._api_telemetry))
        self._remove_event_listeners.append(hass.bus.async_listen(EVENT_MESHTASTIC_API_POSITION, self._api_position))

    async def async_shutdown(self) -> None:
        await super().async_shutdown()

        for remove_listener in self._remove_event_listeners:
            try:
                remove_listener()
            except:  # noqa: E722
                self._logger.debug("Could not remove event listeners", exc_info=True)

    @meshtastic_api_event_callback
    async def _api_node_updated(self, node_id: int, node_data: Mapping[str, Any], **kwargs) -> None:  # noqa: ANN003, ARG002
        if self.data[node_id] != node_data:
            data = deepcopy(self.data)
            data[node_id].update(node_data)
            self.async_set_updated_data(data)

    @meshtastic_api_event_callback
    async def _api_telemetry(
        self,
        node_id: int,
        data: Mapping[str, Any],
        *,
        telemetry_type: EventMeshtasticApiTelemetryType,
        **kwargs,  # noqa: ANN003, ARG002
    ) -> None:
        if telemetry_type == EventMeshtasticApiTelemetryType.DEVICE_METRICS:
            metric_type = "deviceMetrics"
        elif telemetry_type == EventMeshtasticApiTelemetryType.LOCAL_STATS:
            metric_type = "localStats"
        elif telemetry_type == EventMeshtasticApiTelemetryType.POWER_METRICS:
            metric_type = "powerMetrics"
        elif telemetry_type == EventMeshtasticApiTelemetryType.ENVIRONMENT_METRICS:
            metric_type = "environmentMetrics"
        elif telemetry_type == EventMeshtasticApiTelemetryType.HOST_METRICS:
            metric_type = "hostMetrics"
        else:
            self._logger.warning("Unsupported telemetry type %s", telemetry_type)
            return

        new_metrics = data
        existing_metrics = self.data[node_id].get(metric_type, None)
        if existing_metrics == new_metrics:
            self._logger.debug("Received telemetry identical to existing metrics, ignoring event")
            return

        data = deepcopy(self.data)
        data[node_id][metric_type] = new_metrics
        self.async_set_updated_data(data)

    @meshtastic_api_event_callback
    async def _api_position(
        self,
        node_id: int,
        data: Mapping[str, Any],
        **kwargs,  # noqa: ANN003, ARG002
    ) -> None:
        new_position = data
        existing_position = self.data[node_id].get("position", {})
        if existing_position == new_position:
            self._logger.debug("Received position identical to existing position, ignoring event")
            return

        data = deepcopy(self.data)
        data[node_id]["position"] = new_position
        self.async_set_updated_data(data)

    async def _node_updated(self, event: Event) -> None:
        if self.config_entry is None:
            return

        event_data = deepcopy(event.data)
        config_entry_id = event_data.pop("config_entry_id", None)
        if config_entry_id != self.config_entry.entry_id:
            return

        if not self.data:
            self._logger.debug("Received updated metrics but coordinator data is empty")
            return

        node_id = event_data.get("num", None)
        if node_id is None or node_id not in self.data:
            self._logger.debug("Node %d not in coordinator data", node_id)
            return

        if self.data[node_id] != event_data:
            data = deepcopy(self.data)
            data[node_id] = event_data
            self.async_set_updated_data(data)

    async def _async_update_data(self) -> Any:
        if self.config_entry is None or self.config_entry.runtime_data is None:
            self._logger.warning("Update data requested but config entry is empty")
            return None

        try:
            node_infos = await self.config_entry.runtime_data.client.async_get_all_nodes()

            filter_nodes = self.config_entry.options.get(CONF_OPTION_FILTER_NODES, [])
            filter_node_nums = [el["id"] for el in filter_nodes]
            return {
                node_num: deepcopy(node_info)
                for node_num, node_info in node_infos.items()
                if node_num in filter_node_nums
            }
        except MeshtasticApiClientError as exception:
            raise UpdateFailed(exception) from exception
