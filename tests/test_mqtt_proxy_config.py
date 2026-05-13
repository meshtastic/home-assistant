# SPDX-License-Identifier: MIT

"""Tests for MQTT proxy configuration options."""

from __future__ import annotations

import pytest

from custom_components.meshtastic.const import (
    CONF_OPTION_MQTT_PROXY,
    CONF_OPTION_MQTT_PROXY_DOWNLINK,
    CONF_OPTION_MQTT_PROXY_DOWNLINK_DEFAULT,
    CONF_OPTION_MQTT_PROXY_UPLINK_RELAY,
    CONF_OPTION_MQTT_PROXY_UPLINK_RELAY_DEFAULT,
)


class TestMqttProxyConfigConstants:
    """Test that MQTT proxy config constants are defined correctly."""

    def test_mqtt_proxy_section_key(self):
        assert CONF_OPTION_MQTT_PROXY == "mqtt_proxy"

    def test_downlink_key(self):
        assert CONF_OPTION_MQTT_PROXY_DOWNLINK == "downlink"

    def test_uplink_relay_key(self):
        assert CONF_OPTION_MQTT_PROXY_UPLINK_RELAY == "uplink_relay"

    def test_downlink_default_is_false(self):
        """New installs should not have downlink enabled by default."""
        assert CONF_OPTION_MQTT_PROXY_DOWNLINK_DEFAULT is False

    def test_uplink_relay_default_is_false(self):
        """New installs should not have uplink relay enabled by default."""
        assert CONF_OPTION_MQTT_PROXY_UPLINK_RELAY_DEFAULT is False


class TestMqttProxyOptionAccess:
    """Test reading MQTT proxy options from config entry data."""

    def test_options_missing_returns_defaults(self):
        """When mqtt_proxy section is absent, defaults should apply."""
        options: dict = {}
        downlink = options.get(CONF_OPTION_MQTT_PROXY, {}).get(
            CONF_OPTION_MQTT_PROXY_DOWNLINK, CONF_OPTION_MQTT_PROXY_DOWNLINK_DEFAULT
        )
        uplink_relay = options.get(CONF_OPTION_MQTT_PROXY, {}).get(
            CONF_OPTION_MQTT_PROXY_UPLINK_RELAY, CONF_OPTION_MQTT_PROXY_UPLINK_RELAY_DEFAULT
        )
        assert downlink is False
        assert uplink_relay is False

    def test_options_explicitly_enabled(self):
        """When options are explicitly set to True, they should read as True."""
        options = {
            CONF_OPTION_MQTT_PROXY: {
                CONF_OPTION_MQTT_PROXY_DOWNLINK: True,
                CONF_OPTION_MQTT_PROXY_UPLINK_RELAY: True,
            }
        }
        downlink = options.get(CONF_OPTION_MQTT_PROXY, {}).get(
            CONF_OPTION_MQTT_PROXY_DOWNLINK, CONF_OPTION_MQTT_PROXY_DOWNLINK_DEFAULT
        )
        uplink_relay = options.get(CONF_OPTION_MQTT_PROXY, {}).get(
            CONF_OPTION_MQTT_PROXY_UPLINK_RELAY, CONF_OPTION_MQTT_PROXY_UPLINK_RELAY_DEFAULT
        )
        assert downlink is True
        assert uplink_relay is True

    def test_options_mixed(self):
        """Downlink and uplink relay can be configured independently."""
        options = {
            CONF_OPTION_MQTT_PROXY: {
                CONF_OPTION_MQTT_PROXY_DOWNLINK: True,
                CONF_OPTION_MQTT_PROXY_UPLINK_RELAY: False,
            }
        }
        downlink = options[CONF_OPTION_MQTT_PROXY][CONF_OPTION_MQTT_PROXY_DOWNLINK]
        uplink_relay = options[CONF_OPTION_MQTT_PROXY][CONF_OPTION_MQTT_PROXY_UPLINK_RELAY]
        assert downlink is True
        assert uplink_relay is False
