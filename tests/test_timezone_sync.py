# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""The radio's time zone is filled in when it has none, and otherwise left alone."""

from __future__ import annotations

from unittest.mock import AsyncMock

from custom_components.meshtastic.aiomeshtastic import MeshInterface
from custom_components.meshtastic.aiomeshtastic.interface import posix_tz_string
from custom_components.meshtastic.aiomeshtastic.protobuf import config_pb2


def _interface_with_tzdef(tzdef: str) -> MeshInterface:
    interface = MeshInterface(connection=None, enable_mqtt_proxy=False)
    interface.request_device_config = AsyncMock(return_value=config_pb2.Config.DeviceConfig(tzdef=tzdef))
    interface.write_device_config = AsyncMock()
    return interface


async def test_a_zone_set_on_the_radio_is_never_overwritten() -> None:
    interface = _interface_with_tzdef("CET-1CEST,M3.5.0,M10.5.0/3")

    assert not await interface.write_timezone_if_unset("America/Chicago")

    interface.write_device_config.assert_not_awaited()


async def test_a_radio_without_a_zone_gets_the_given_one() -> None:
    interface = _interface_with_tzdef("")

    assert await interface.write_timezone_if_unset("America/Chicago")

    written = interface.write_device_config.await_args.args[0]
    assert written.tzdef == "CST6CDT,M3.2.0,M11.1.0"


async def test_an_unknown_zone_writes_nothing() -> None:
    interface = _interface_with_tzdef("")

    assert not await interface.write_timezone_if_unset("Not/AZone")

    interface.write_device_config.assert_not_awaited()


def test_the_posix_string_comes_from_the_named_zone() -> None:
    assert posix_tz_string("Europe/Amsterdam") == "CET-1CEST,M3.5.0,M10.5.0/3"
    assert posix_tz_string("UTC") == "UTC0"


def test_a_path_is_not_a_zone() -> None:
    assert posix_tz_string("../../etc/passwd") is None
