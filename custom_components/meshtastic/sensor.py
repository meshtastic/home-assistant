# SPDX-FileCopyrightText: 2024-2025 Pascal Brogle @broglep
#
# SPDX-License-Identifier: MIT

from __future__ import annotations

import datetime
import typing
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    DOMAIN as SENSOR_DOMAIN,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config import callback
from homeassistant.const import (
    DEGREE,
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfConductivity,
    UnitOfDensity,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfInformation,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPressure,
    UnitOfRatio,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumetricFlux,
)

from . import LOGGER, helpers
from .entity import MeshtasticNodeEntity
from .field_metadata import device_role_attributes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback
    from homeassistant.helpers.typing import StateType

    from .coordinator import MeshtasticDataUpdateCoordinator
    from .data import MeshtasticConfigEntry, MeshtasticData


PARALLEL_UPDATES = 0


def _build_sensors(nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData) -> Iterable[MeshtasticSensor]:
    entities = []
    entities += _build_node_sensors(nodes, runtime_data)
    entities += _build_device_sensors(nodes, runtime_data)
    entities += _build_local_stats_sensors(nodes, runtime_data)
    entities += _build_power_metrics_sensors(nodes, runtime_data)
    entities += _build_environment_metrics_sensors(nodes, runtime_data)
    entities += _build_air_quality_metrics_sensors(nodes, runtime_data)
    entities += _build_soil_water_metrics_sensors(nodes, runtime_data)
    entities += _build_host_metrics_sensors(nodes, runtime_data)
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await helpers.setup_platform_entry(hass, entry, async_add_entities, _build_sensors)


async def async_unload_entry(
    hass: HomeAssistant,
    entry: MeshtasticConfigEntry,
) -> bool:
    return await helpers.async_unload_entry(hass, entry)


@dataclass(kw_only=True)
class MeshtasticSensorEntityDescription(SensorEntityDescription):
    exists_fn: Callable[[MeshtasticSensor], bool] = lambda _: True
    value_fn: Callable[[MeshtasticSensor], StateType]
    attributes_fn: Callable[[MeshtasticSensor], dict[str, Any]] | None = None


class MeshtasticSensor(MeshtasticNodeEntity, SensorEntity):
    entity_description: MeshtasticSensorEntityDescription

    def __init__(
        self,
        coordinator: MeshtasticDataUpdateCoordinator,
        entity_description: MeshtasticSensorEntityDescription,
        gateway: typing.Mapping[str, typing.Any],
        node_id: int,
    ) -> None:
        super().__init__(coordinator, gateway, node_id, SENSOR_DOMAIN, entity_description)

    @callback
    def _async_update_attrs(self) -> None:
        LOGGER.debug("Updating sensor attributes: %s", self)
        self._attr_native_value = self.entity_description.value_fn(self)
        self._attr_available = self._attr_native_value is not None
        if self.entity_description.attributes_fn is not None:
            self._attr_extra_state_attributes = self.entity_description.attributes_fn(self)


def _build_node_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    entities = []
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()

    def last_heard(device: MeshtasticNodeEntity) -> datetime.datetime | None:
        last_heard_int = device.coordinator.data[device.node_id].get("lastHeard")
        if not last_heard_int:
            return None
        return datetime.datetime.fromtimestamp(last_heard_int, tz=datetime.UTC)

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_last_heard",
                entity_category=EntityCategory.DIAGNOSTIC,
                device_class=SensorDeviceClass.TIMESTAMP,
                value_fn=last_heard,
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_snr",
                entity_category=EntityCategory.DIAGNOSTIC,
                native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS,
                device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("snr", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_hops_away",
                entity_category=EntityCategory.DIAGNOSTIC,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("hopsAway", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_role",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("role", None),
                attributes_fn=lambda device: dict(
                    device_role_attributes(device.coordinator.data[device.node_id].get("user", {}).get("role", None))
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
        if node_id != runtime_data.gateway_node["num"]
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_short_name",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("shortName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="node_long_name",
                entity_category=EntityCategory.DIAGNOSTIC,
                value_fn=lambda device: device.coordinator.data[device.node_id].get("user", {}).get("longName", None),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


def _build_device_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    entities = []

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_uptime",
                entity_category=EntityCategory.DIAGNOSTIC,
                native_unit_of_measurement=UnitOfTime.SECONDS,
                device_class=SensorDeviceClass.DURATION,
                state_class=SensorStateClass.TOTAL_INCREASING,
                value_fn=lambda device: (
                    device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("uptimeSeconds", None)
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    def battery_level(device: MeshtasticSensor) -> int | None:
        level = device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("batteryLevel", None)
        if level is not None:
            return max(0, min(100, level))
        return level

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_battery_level",
                native_unit_of_measurement=PERCENTAGE,
                device_class=SensorDeviceClass.BATTERY,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=battery_level,
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_voltage",
                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                device_class=SensorDeviceClass.VOLTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda device: (
                    device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("voltage", None)
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_channel_utilization",
                entity_category=EntityCategory.DIAGNOSTIC,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda device: (
                    device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("channelUtilization", None)
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]
    entities += [
        MeshtasticSensor(
            coordinator=coordinator,
            entity_description=MeshtasticSensorEntityDescription(
                key="device_airtime",
                entity_category=EntityCategory.DIAGNOSTIC,
                native_unit_of_measurement=PERCENTAGE,
                state_class=SensorStateClass.MEASUREMENT,
                suggested_display_precision=2,
                value_fn=lambda device: (
                    device.coordinator.data[device.node_id].get("deviceMetrics", {}).get("airUtilTx", None)
                ),
            ),
            gateway=gateway,
            node_id=node_id,
        )
        for node_id, node_info in nodes.items()
    ]

    return entities


def _build_local_stats_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_local_stats = {node_id: node_info for node_id, node_info in nodes.items() if "localStats" in node_info}

    entities = []
    try:
        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numPacketsTx", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numPacketsRx", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx_bad",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numPacketsRxBad", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_rx_duplicate",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numRxDupe", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx_relayed",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numTxRelay", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_packets_tx_relay_cancelled",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    state_class=SensorStateClass.TOTAL_INCREASING,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numTxRelayCanceled", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_nodes_online",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numOnlineNodes", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_nodes_total",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    state_class=SensorStateClass.TOTAL,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("numTotalNodes", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_heap_total_bytes",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    native_unit_of_measurement=UnitOfInformation.BYTES,
                    device_class=SensorDeviceClass.DATA_SIZE,
                    state_class=SensorStateClass.MEASUREMENT,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("heapTotalBytes", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_heap_free_bytes",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    entity_registry_enabled_default=False,
                    native_unit_of_measurement=UnitOfInformation.BYTES,
                    device_class=SensorDeviceClass.DATA_SIZE,
                    state_class=SensorStateClass.MEASUREMENT,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("heapFreeBytes", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]

        entities += [
            MeshtasticSensor(
                coordinator=coordinator,
                entity_description=MeshtasticSensorEntityDescription(
                    key="stats_noise_floor",
                    entity_category=EntityCategory.DIAGNOSTIC,
                    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
                    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
                    state_class=SensorStateClass.MEASUREMENT,
                    value_fn=lambda device: (
                        device.coordinator.data[device.node_id].get("localStats", {}).get("noiseFloor", None)
                    ),
                ),
                gateway=gateway,
                node_id=node_id,
            )
            for node_id, node_info in nodes_with_local_stats.items()
        ]
    except:  # noqa: E722
        LOGGER.warning("Failed to create local stats entities", exc_info=True)

    return entities


def _build_power_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_power_metrics = {
        node_id: node_info for node_id, node_info in nodes.items() if "powerMetrics" in node_info
    }
    if not nodes_with_power_metrics:
        return []

    entities = []
    try:
        for node_id, node_info in nodes_with_power_metrics.items():
            power_metrics = node_info["powerMetrics"]
            for channel in range(1, 4):
                voltage_key = f"ch{channel}Voltage"
                current_key = f"ch{channel}Current"

                def power_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
                    return lambda device: device.coordinator.data[device.node_id].get("powerMetrics", {}).get(key, None)

                if voltage_key in power_metrics:
                    entities.append(
                        MeshtasticSensor(
                            coordinator=coordinator,
                            entity_description=MeshtasticSensorEntityDescription(
                                key=f"power_ch{channel}_voltage",
                                native_unit_of_measurement=UnitOfElectricPotential.VOLT,
                                device_class=SensorDeviceClass.VOLTAGE,
                                state_class=SensorStateClass.MEASUREMENT,
                                suggested_display_precision=2,
                                value_fn=power_metrics_value_fn(voltage_key),
                            ),
                            gateway=gateway,
                            node_id=node_id,
                        )
                    )
                if current_key in power_metrics:
                    entities.append(
                        MeshtasticSensor(
                            coordinator=coordinator,
                            entity_description=MeshtasticSensorEntityDescription(
                                key=f"power_ch{channel}_current",
                                native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
                                device_class=SensorDeviceClass.CURRENT,
                                state_class=SensorStateClass.MEASUREMENT,
                                suggested_display_precision=2,
                                value_fn=power_metrics_value_fn(current_key),
                            ),
                            gateway=gateway,
                            node_id=node_id,
                        )
                    )

    except:  # noqa: E722
        LOGGER.warning("Failed to create power metrics entities", exc_info=True)

    return entities


def _build_environment_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_environment_metrics = {
        node_id: node_info for node_id, node_info in nodes.items() if "environmentMetrics" in node_info
    }
    if not nodes_with_environment_metrics:
        return []

    entities = []

    def environment_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
        return lambda device: device.coordinator.data[device.node_id].get("environmentMetrics", {}).get(key, None)

    def add_sensor_base(  # noqa: PLR0913
        node_id: int,
        node_info: dict[str, Any],
        value_key: str,
        device_class: SensorDeviceClass | None,
        unit_of_measurement: str | None = None,
        state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
    ) -> None:
        key = "".join(["_" + c.lower() if c.isupper() else c for c in value_key]).lstrip("_")
        if value_key in node_info["environmentMetrics"]:
            entities.append(
                MeshtasticSensor(
                    coordinator=coordinator,
                    entity_description=MeshtasticSensorEntityDescription(
                        key="environment_" + key,
                        translation_key="environment_" + key,
                        entity_registry_enabled_default=not value_key.startswith(
                            ("adcVoltageCh", "oneWireTemperatureCh")
                        ),
                        native_unit_of_measurement=unit_of_measurement,
                        device_class=device_class,
                        state_class=state_class,
                        value_fn=environment_metrics_value_fn(value_key),
                    ),
                    gateway=gateway,
                    node_id=node_id,
                )
            )

    try:
        for node_id, node_info in nodes_with_environment_metrics.items():
            add_sensor = partial(add_sensor_base, node_id, node_info)

            add_sensor("temperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS)
            add_sensor("relativeHumidity", SensorDeviceClass.HUMIDITY, PERCENTAGE)
            add_sensor("barometricPressure", SensorDeviceClass.ATMOSPHERIC_PRESSURE, UnitOfPressure.HPA)
            add_sensor("gasResistance", None, "kΩ")
            add_sensor("iaq", SensorDeviceClass.AQI, None)

            add_sensor("distance", SensorDeviceClass.DISTANCE, UnitOfLength.MILLIMETERS)

            add_sensor("lux", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX)
            add_sensor("whiteLux", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX)
            add_sensor("irLux", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX)
            add_sensor("uvLux", SensorDeviceClass.ILLUMINANCE, LIGHT_LUX)

            add_sensor("windDirection", SensorDeviceClass.WIND_SPEED, DEGREE)
            add_sensor("windSpeed", SensorDeviceClass.WIND_SPEED, UnitOfSpeed.METERS_PER_SECOND)
            add_sensor("windGust", SensorDeviceClass.WIND_SPEED, UnitOfSpeed.METERS_PER_SECOND)
            add_sensor("windLull", SensorDeviceClass.WIND_SPEED, UnitOfSpeed.METERS_PER_SECOND)

            # deprecated in favor of power metrics
            add_sensor("voltage", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT)
            add_sensor("current", SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE)

            add_sensor(
                "rainfall1h", SensorDeviceClass.PRECIPITATION_INTENSITY, UnitOfVolumetricFlux.MILLIMETERS_PER_HOUR
            )
            add_sensor(
                "rainfall24h", SensorDeviceClass.PRECIPITATION_INTENSITY, UnitOfVolumetricFlux.MILLIMETERS_PER_DAY
            )

            add_sensor("soilMoisture", SensorDeviceClass.MOISTURE, PERCENTAGE)
            add_sensor("soilTemperature", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS)

            add_sensor("lightningDistanceKm", SensorDeviceClass.DISTANCE, UnitOfLength.KILOMETERS)
            add_sensor("lightningStrikeCount1h", None, None)

            for channel in range(8):
                add_sensor(f"adcVoltageCh{channel}", SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT)
                add_sensor(f"oneWireTemperatureCh{channel}", SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS)

    except:  # noqa: E722
        LOGGER.warning("Failed to create environment metric entities", exc_info=True)

    return entities


def _build_host_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_host_metrics = {node_id: node_info for node_id, node_info in nodes.items() if "hostMetrics" in node_info}
    if not nodes_with_host_metrics:
        return []

    entities = []

    def host_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
        return lambda device: device.coordinator.data[device.node_id].get("hostMetrics", {}).get(key, None)

    def add_sensor_base(  # noqa: PLR0913
        node_id: int,
        node_info: dict[str, Any],
        value_key: str,
        device_class: SensorDeviceClass | None,
        unit_of_measurement: str | None = None,
        state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
    ) -> None:
        key = "".join(["_" + c.lower() if c.isupper() else c for c in value_key]).lstrip("_")
        if value_key in node_info["hostMetrics"]:
            entities.append(
                MeshtasticSensor(
                    coordinator=coordinator,
                    entity_description=MeshtasticSensorEntityDescription(
                        key="host_" + key,
                        translation_key="host_" + key,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        native_unit_of_measurement=unit_of_measurement,
                        device_class=device_class,
                        state_class=state_class,
                        value_fn=host_metrics_value_fn(value_key),
                    ),
                    gateway=gateway,
                    node_id=node_id,
                )
            )

    try:
        for node_id, node_info in nodes_with_host_metrics.items():
            add_sensor = partial(add_sensor_base, node_id, node_info)

            add_sensor(
                "uptimeSeconds",
                SensorDeviceClass.DURATION,
                UnitOfTime.SECONDS,
                SensorStateClass.TOTAL_INCREASING,
            )
            add_sensor("freememBytes", SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES)
            add_sensor("diskfree1Bytes", SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES)
            add_sensor("diskfree2Bytes", SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES)
            add_sensor("diskfree3Bytes", SensorDeviceClass.DATA_SIZE, UnitOfInformation.BYTES)
            add_sensor("load1", None, PERCENTAGE)
            add_sensor("load5", None, PERCENTAGE)
            add_sensor("load15", None, PERCENTAGE)
            add_sensor("userString", None, None)

    except:  # noqa: E722
        LOGGER.warning("Failed to create host metric entities", exc_info=True)

    return entities


def _build_air_quality_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_environment_metrics = {
        node_id: node_info for node_id, node_info in nodes.items() if "airQualityMetrics" in node_info
    }
    if not nodes_with_environment_metrics:
        return []

    entities = []

    def air_quality_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
        return lambda device: device.coordinator.data[device.node_id].get("airQualityMetrics", {}).get(key, None)

    def add_sensor_base(  # noqa: PLR0913
        node_id: int,
        node_info: dict[str, Any],
        value_key: str,
        device_class: SensorDeviceClass | None,
        unit_of_measurement: str | None = None,
        state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
    ) -> None:
        key = "".join(["_" + c.lower() if c.isupper() else c for c in value_key]).lstrip("_")
        if value_key in node_info["airQualityMetrics"]:
            entities.append(
                MeshtasticSensor(
                    coordinator=coordinator,
                    entity_description=MeshtasticSensorEntityDescription(
                        key="airquality_" + key,
                        translation_key="airquality_" + key,
                        native_unit_of_measurement=unit_of_measurement,
                        device_class=device_class,
                        state_class=state_class,
                        value_fn=air_quality_metrics_value_fn(value_key),
                    ),
                    gateway=gateway,
                    node_id=node_id,
                )
            )

    try:
        for node_id, node_info in nodes_with_environment_metrics.items():
            add_sensor = partial(add_sensor_base, node_id, node_info)

            add_sensor("pm10Standard", SensorDeviceClass.PM10, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("pm25Standard", SensorDeviceClass.PM25, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("pm100Standard", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)

            add_sensor("pm10Environmental", SensorDeviceClass.PM10, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("pm25Environmental", SensorDeviceClass.PM25, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("pm100Environmental", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)

            add_sensor("particles03um", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("particles05um", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("particles10um", SensorDeviceClass.PM10, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("particles25um", SensorDeviceClass.PM25, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("particles50um", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
            add_sensor("particles100um", None, UnitOfDensity.MICROGRAMS_PER_CUBIC_METER)
    except:  # noqa: E722
        LOGGER.warning("Failed to create air quality metric entities", exc_info=True)

    return entities


def _build_soil_water_metrics_sensors(
    nodes: Mapping[int, Mapping[str, Any]], runtime_data: MeshtasticData
) -> Iterable[MeshtasticSensor]:
    coordinator = runtime_data.coordinator
    gateway = runtime_data.client.get_own_node()
    nodes_with_soil_water_metrics = {
        node_id: node_info for node_id, node_info in nodes.items() if "soilWaterMetrics" in node_info
    }
    if not nodes_with_soil_water_metrics:
        return []

    entities = []

    def soil_water_metrics_value_fn(key: str) -> Callable[[MeshtasticSensor], str | None]:
        return lambda device: device.coordinator.data[device.node_id].get("soilWaterMetrics", {}).get(key, None)

    def add_sensor_base(  # noqa: PLR0913
        node_id: int,
        node_info: dict[str, Any],
        value_key: str,
        device_class: SensorDeviceClass | None,
        unit_of_measurement: str | None = None,
        state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
    ) -> None:
        key = "".join(["_" + c.lower() if c.isupper() else c for c in value_key]).lstrip("_")
        if value_key in node_info["soilWaterMetrics"]:
            entities.append(
                MeshtasticSensor(
                    coordinator=coordinator,
                    entity_description=MeshtasticSensorEntityDescription(
                        key="soil_water_" + key,
                        translation_key="soil_water_" + key,
                        native_unit_of_measurement=unit_of_measurement,
                        device_class=device_class,
                        state_class=state_class,
                        value_fn=soil_water_metrics_value_fn(value_key),
                    ),
                    gateway=gateway,
                    node_id=node_id,
                )
            )

    try:
        for node_id, node_info in nodes_with_soil_water_metrics.items():
            add_sensor = partial(add_sensor_base, node_id, node_info)

            add_sensor("soilPh", SensorDeviceClass.PH)
            add_sensor("ph", SensorDeviceClass.PH)
            add_sensor(
                "electricalConductivity",
                SensorDeviceClass.CONDUCTIVITY,
                UnitOfConductivity.MILLISIEMENS_PER_CM,
            )
            add_sensor("salinity", None, "mg/L")
            add_sensor("nitrogen", None, "mg/kg")
            add_sensor("phosphorus", None, "mg/kg")
            add_sensor("potassium", None, "mg/kg")
            add_sensor("dissolvedOxygen", None, "mg/L")
            add_sensor("orp", None, UnitOfElectricPotential.MILLIVOLT)
            add_sensor("chemicalOxygenDemand", None, "mg/L")
            add_sensor("turbidity", None, "NTU")
            add_sensor("nitrate", None, UnitOfRatio.PARTS_PER_MILLION)
            add_sensor("ammonium", None, UnitOfRatio.PARTS_PER_MILLION)
            add_sensor("biochemicalOxygenDemand", None, "mg/L")
            add_sensor(
                "solarIrradiance",
                SensorDeviceClass.IRRADIANCE,
                UnitOfIrradiance.WATTS_PER_SQUARE_METER,
            )
    except:  # noqa: E722
        LOGGER.warning("Failed to create soil water metric entities", exc_info=True)

    return entities
