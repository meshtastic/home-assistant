# SPDX-FileCopyrightText: 2026 James Rich
#
# SPDX-License-Identifier: MIT

"""Fields the schema marks sensitive never reach state attributes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.meshtastic.const import DOMAIN
from custom_components.meshtastic.field_metadata import sensitive_json_names, without_sensitive_local_config

from .conftest import GATEWAY_NUM

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

SECRET_VALUES = ("secret", "wifi-password", "mqtt-password", "123456", "c2VjcmV0")


def test_the_schema_marks_secrets_and_leaves_public_keys() -> None:
    names = sensitive_json_names()

    assert {"privateKey", "wifiPsk", "password", "fixedPin", "psk"} <= names
    assert not {"publicKey", "adminKey", "wifiSsid", "address"} & names


def test_redaction_walks_into_submessages_and_keeps_unknown_keys() -> None:
    redacted = without_sensitive_local_config(
        {"security": {"privateKey": "x", "isManaged": True}, "network": {"wifiPsk": "y"}, "extra": 1}
    )

    assert redacted == {"security": {"isManaged": True}, "network": {}, "extra": 1}


@pytest.mark.usefixtures("fake_client")
async def test_gateway_and_channel_attributes_carry_no_secrets(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)

    gateway = hass.states.get(registry.async_get_entity_id(DOMAIN, DOMAIN, f"{entry.entry_id}_gateway_{GATEWAY_NUM}"))
    channel = hass.states.get(registry.async_get_entity_id(DOMAIN, DOMAIN, f"{entry.entry_id}_channel_{GATEWAY_NUM}_0"))

    rendered = json.dumps([dict(gateway.attributes), dict(channel.attributes)])
    for secret in SECRET_VALUES:
        assert secret not in rendered
    assert gateway.attributes["config_network_wifiSsid"] == "home"
    assert gateway.attributes["config_bluetooth_mode"] == "FIXED_PIN"
    assert "config_security_privateKey" not in gateway.attributes
    assert "psk" not in channel.attributes
    assert channel.attributes["uplink_enabled"] is False
